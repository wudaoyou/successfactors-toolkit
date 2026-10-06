"""Security-relevant events leave an audit line; no line ever holds a secret."""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.main import app
from successfactors_toolkit.models.common import ODataConnectionConfig, SFAPIConnectionConfig
from successfactors_toolkit.services import saml_bearer
from successfactors_toolkit.services.audit import audit
from successfactors_toolkit.services.connection_policy import (
    ConnectionPolicyError,
    check_token_url,
)
from successfactors_toolkit.services.odata_client import ODataClient
from successfactors_toolkit.services.sfapi_client import SFAPIClient
from tests.systems import write_system
from tests.test_tenants import _pem_pair

LOGGER = "successfactors_toolkit.audit"


@pytest.fixture
def lines(caplog):
    """Audit lines logged so far. The audit logger does not propagate, so the
    capture handler is attached to it directly."""
    logger = logging.getLogger(LOGGER)
    logger.addHandler(caplog.handler)
    yield lambda: [r.getMessage() for r in caplog.records if r.name == LOGGER]
    logger.removeHandler(caplog.handler)


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    get_settings.cache_clear()
    return {"X-API-Key": "test-api-key", "X-Admin-Key": "test-admin-key"}


def _upload(pair):
    return {"private_key": ("k.pem", pair[0]), "certificate": ("c.pem", pair[1])}


def test_lines_are_key_value_with_booleans_and_without_nones(lines):
    audit("key_install", "ok", company_id="demo", force=False, code=None)
    assert lines() == ["event=key_install outcome=ok company_id=demo force=false"]


def test_a_value_cannot_forge_a_second_line(lines):
    audit("connection_override", "denied", company_id="demo\nevent=key_delete outcome=ok x=y")
    (line,) = lines()
    assert "\n" not in line
    assert line.startswith('event=connection_override outcome=denied company_id="demo\\nevent=')


def test_only_ok_outcomes_are_info(caplog, lines):
    audit("key_delete", "ok", company_id="demo")
    audit("auth", "denied", scope="api", reason="missing")
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [
        logging.INFO,
        logging.WARNING,
    ]


def test_api_key_failures_are_logged_without_the_key(monkeypatch, lines):
    monkeypatch.setenv("API_KEY", "test-api-key")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get("/api/tenants").status_code == 401
        assert client.get("/api/tenants", headers={"X-API-Key": "guess-123"}).status_code == 401
    assert lines() == [
        "event=auth outcome=denied scope=api reason=missing",
        "event=auth outcome=denied scope=api reason=invalid",
    ]


def test_disabled_rest_api_is_logged(lines):
    with TestClient(app) as client:
        assert client.get("/api/tenants", headers={"X-API-Key": "x"}).status_code == 503
    assert lines() == ["event=auth outcome=denied scope=api reason=disabled"]


def test_admin_key_failures_are_logged_without_the_key(admin, lines):
    api_only = {"X-API-Key": admin["X-API-Key"]}
    with TestClient(app) as client:
        assert client.get("/api/tenants", headers=api_only).status_code == 401
        wrong = {**api_only, "X-Admin-Key": "guess-456"}
        assert client.get("/api/tenants", headers=wrong).status_code == 401
        assert client.get("/api/tenants", headers=admin).status_code == 200
    assert lines() == [
        "event=auth outcome=denied scope=admin reason=missing",
        "event=auth outcome=denied scope=admin reason=invalid",
    ]


def test_disabled_admin_api_is_logged(monkeypatch, lines):
    monkeypatch.setenv("API_KEY", "test-api-key")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get("/api/tenants", headers={"X-API-Key": "test-api-key"}).status_code == 503
    assert lines() == ["event=auth outcome=denied scope=admin reason=disabled"]


def test_key_install_and_delete_are_logged_without_key_material(admin, lines):
    pair = _pem_pair()
    with TestClient(app) as client:
        url = "/api/tenants/example-a"
        assert client.post(f"{url}/keypair", headers=admin, files=_upload(pair)).status_code == 201
        assert client.post(f"{url}/keypair", headers=admin, files=_upload(pair)).status_code == 409
        forced = client.post(f"{url}/keypair?force=true", headers=admin, files=_upload(pair))
        assert forced.status_code == 201
        bad = client.post(f"{url}/keypair?force=true", headers=admin, files=_upload((b"x", b"y")))
        assert bad.status_code == 400
        assert client.delete(url, headers=admin).status_code == 204
        assert client.delete(url, headers=admin).status_code == 404
    assert lines() == [
        "event=key_install outcome=ok company_id=example-a force=false",
        "event=key_install outcome=failed company_id=example-a code=tenant_already_exists",
        "event=key_install outcome=ok company_id=example-a force=true",
        "event=key_install outcome=failed company_id=example-a code=invalid_private_key",
        "event=key_delete outcome=ok company_id=example-a",
        "event=key_delete outcome=failed company_id=example-a code=tenant_not_found",
    ]
    assert not any("PRIVATE KEY" in line or "CERTIFICATE" in line for line in lines())


def test_production_flag_changes_are_logged_with_the_previous_value(admin, lines):
    with TestClient(app) as client:
        client.post("/api/tenants/example-a/keypair", headers=admin, files=_upload(_pem_pair()))
        url = "/api/tenants/example-a/environment"
        assert client.put(url, headers=admin, json={"production": True}).status_code == 200
        assert client.put(url, headers=admin, json={"production": False}).status_code == 200
        assert (
            client.put(
                "/api/tenants/nope/environment", headers=admin, json={"production": True}
            ).status_code
            == 404
        )
    assert lines()[1:] == [
        "event=production_flag outcome=ok company_id=example-a previous=unset production=true",
        "event=production_flag outcome=ok company_id=example-a previous=true production=false",
        "event=production_flag outcome=failed company_id=nope code=tenant_not_found",
    ]


@pytest.fixture
def settings():
    settings = Settings(_env_file=None)
    write_system(settings.systems_dir, "example-a", client_key="configured-key")
    return settings


def test_rejected_overrides_are_logged_by_field_not_by_value(settings, tmp_path, lines):
    client = ODataClient(settings, None)
    outside = tmp_path / "secret-name.pem"
    outside.write_bytes(b"synthetic-key")
    for conn in (
        ODataConnectionConfig(host="evil.invalid"),
        ODataConnectionConfig(private_key_path=str(outside)),
        ODataConnectionConfig(client_key="attacker-key"),
    ):
        with pytest.raises(ConnectionPolicyError):
            client._resolve(conn)
    with pytest.raises(ConnectionPolicyError):  # what saml_bearer.fetch_token runs
        check_token_url("https://evil.invalid/oauth/token", settings, "example-a")
    assert lines() == [
        "event=connection_override outcome=denied system=example-a field=host",
        "event=connection_override outcome=denied system=example-a field=private_key_path",
        "event=connection_override outcome=denied system=example-a field=client_key",
        "event=connection_override outcome=denied system=example-a field=token_url",
    ]
    assert not any("evil" in line or "secret-name" in line for line in lines())


def _write_system_file(settings, **fields):
    write_system(settings.systems_dir, "example-a", **fields)


def test_a_bad_configured_value_is_a_config_failure_not_a_refused_override(settings, lines):
    _write_system_file(settings, host="evil.invalid", token_url="https://evil.invalid/oauth/token")
    for client in (ODataClient(settings, None), SFAPIClient(settings, None)):
        with pytest.raises(ConnectionPolicyError):
            client._resolve(None)
    # The same value sent in the request is a refused override.
    with pytest.raises(ConnectionPolicyError):
        ODataClient(settings, None)._resolve(ODataConnectionConfig(host="evil.invalid"))
    assert lines() == [
        "event=connection_config outcome=failed system=example-a field=host",
        "event=connection_config outcome=failed system=example-a field=host",
        "event=connection_override outcome=denied system=example-a field=host",
    ]


def test_token_url_denials_say_whether_the_request_or_the_config_supplied_it(settings, lines):
    _write_system_file(settings, host="api4preview.sapsf.com", token_url="https://evil.invalid/t")

    async def run():
        odata, sfapi = ODataClient(settings, None), SFAPIClient(settings, None)
        for conn in (None, ODataConnectionConfig(token_url="https://other.invalid/t")):
            with pytest.raises(ConnectionPolicyError):
                await odata._fetch_new_token(odata._resolve(conn))
        for conn in (None, SFAPIConnectionConfig(token_url="https://other.invalid/t")):
            with pytest.raises(ConnectionPolicyError):
                await sfapi._login(sfapi._resolve(conn))

    asyncio.run(run())
    config = "event=connection_config outcome=failed system=example-a field=token_url"
    override = "event=connection_override outcome=denied system=example-a field=token_url"
    # A denied token_url is never also logged as an accepted override.
    assert lines() == [config, override, config, override]


def test_denied_token_url_override_is_refused_with_a_warm_token_cache(settings, lines):
    odata, sfapi = ODataClient(settings, None), SFAPIClient(settings, None)
    odata._tokens[odata._token_key(odata._resolve(None))] = ("cached", float("inf"))
    sfapi._sessions[sfapi._session_key(sfapi._resolve(None))] = object()
    with pytest.raises(ConnectionPolicyError):
        odata._resolve(ODataConnectionConfig(token_url="https://evil.invalid/t"))
    with pytest.raises(ConnectionPolicyError):
        sfapi._resolve(SFAPIConnectionConfig(token_url="https://evil.invalid/t"))
    assert (
        lines() == ["event=connection_override outcome=denied system=example-a field=token_url"] * 2
    )


def test_accepted_overrides_are_logged_by_field_name_only(settings, lines):
    odata = ODataClient(settings, None)
    sfapi = SFAPIClient(settings, None)
    # Repeating what is configured is not an override.
    odata._resolve(ODataConnectionConfig(host="api.example.invalid", client_key="configured-key"))
    sfapi._resolve(SFAPIConnectionConfig(system="example-a"))
    assert lines() == []
    odata._resolve(
        ODataConnectionConfig(
            host="api4preview.sapsf.com", token_url="https://api4preview.sapsf.com/oauth/token"
        )
    )
    sfapi._resolve(SFAPIConnectionConfig(host="api4preview.sapsf.com"))
    assert lines() == [
        "event=connection_override outcome=ok system=example-a fields=host,token_url",
        "event=connection_override outcome=ok system=example-a fields=host",
    ]


def test_a_rejected_rest_override_is_logged(monkeypatch, lines):
    monkeypatch.setenv("API_KEY", "test-api-key")
    get_settings.cache_clear()
    with TestClient(app) as client:
        response = client.post(
            "/api/odata/execute",
            headers={"X-API-Key": "test-api-key"},
            json={
                "path": "User",
                "connection": {"system": "example-a", "host": "evil.invalid"},
            },
        )
    assert response.status_code == 400
    assert lines() == ["event=connection_override outcome=denied system=example-a field=host"]


def test_a_refused_sf_token_request_is_logged_without_the_assertion(settings, lines):
    async def refuse(request):
        return httpx.Response(401, json={"error": "invalid_client"}, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
            with pytest.raises(httpx.HTTPStatusError):
                await saml_bearer.fetch_token(
                    http_client=http,
                    client_key="configured-key",
                    user_id="EXAMPLE001",
                    company_id="demo",
                    token_url="https://api.example.invalid/oauth/token",
                    private_key_pem=_pem_pair()[0],
                    settings=settings,
                )

    asyncio.run(run())
    assert lines() == ["event=sf_token outcome=failed company_id=demo status=401"]


def test_audit_lines_reach_stderr_and_never_stdout():
    root = Path(__file__).resolve().parents[1]
    script = "from successfactors_toolkit.services.audit import audit; audit('auth', 'denied', scope='api')"
    run = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(root)},
    )
    assert run.stdout == ""
    assert "WARNING audit event=auth outcome=denied scope=api" in run.stderr


def test_mcp_stdio_keeps_audit_lines_off_the_protocol_stream(tmp_path):
    """A denied connection in MCP mode is logged on stderr; stdout stays JSON-RPC."""
    root = Path(__file__).resolve().parents[1]
    write_system(
        tmp_path / "systems",
        "demo",
        host="evil.invalid",
        token_url="https://evil.invalid/oauth/token",
    )
    errlog = tmp_path / "stderr.txt"

    async def exercise():
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("SF_") and key not in {"API_KEY", "ADMIN_API_KEY"}
        }
        env.update(
            {
                "PYTHONPATH": str(root),
                "SYSTEMS_DIR": str(tmp_path / "systems"),
            }
        )
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "successfactors_toolkit.mcp_server"],
            cwd=str(tmp_path),
            env=env,
        )
        with errlog.open("w") as err:
            async with stdio_client(server, errlog=err) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=15) as session:
                    await session.initialize()
                    result = await session.call_tool("odata_metadata", {"system": "demo"})
                    assert result.is_error

    asyncio.run(asyncio.wait_for(exercise(), timeout=60))
    # The bad host is in the system file, not in a request: a config failure.
    assert "event=connection_config outcome=failed system=demo field=host" in errlog.read_text()
    assert "event=connection_override" not in errlog.read_text()
