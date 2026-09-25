"""{company_id}/{company_id}.json: one server, per-tenant connection and PII settings."""

import asyncio
import base64
import json
import re
from pathlib import Path

import httpx
import pytest

from successfactors_toolkit import mcp_server
from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.models.common import ODataConnectionConfig, SFAPIConnectionConfig
from successfactors_toolkit.services import saml_bearer
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError
from successfactors_toolkit.services.odata_client import ODataClient
from successfactors_toolkit.services.pii_filter import (
    PiiVaultError,
    TenantConfigInvalid,
    TenantEnvironmentUnset,
    for_tenant,
)
from successfactors_toolkit.services.sfapi_client import SFAPIClient
from successfactors_toolkit.services.tenant_store import TenantConfigError, TenantStore

_KEY_PEM = base64.b64encode(b"synthetic-key").decode()
_FILE_HOST = "api4preview.sapsf.com"
_FILE_TOKEN_URL = f"https://{_FILE_HOST}/oauth/token"


def _write(company_id, **content):
    directory = Path(get_settings().tenant_keys_dir) / company_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{company_id}.json"
    path.write_text(json.dumps(content))
    return path


def _store():
    return TenantStore(get_settings().tenant_keys_dir)


# ── the file ──────────────────────────────────────────────────────────────


def test_old_tenant_json_alone_is_unset_with_a_rename_hint():
    directory = Path(get_settings().tenant_keys_dir) / "example-a"
    directory.mkdir(parents=True)
    (directory / "tenant.json").write_text('{"production": false}')
    assert _store().production("example-a") is None and _store().config("example-a") is None
    with pytest.raises(TenantEnvironmentUnset) as caught:
        for_tenant(get_settings(), "example-a")
    assert "example-a/example-a.json" in str(caught.value) and "rename" in str(caught.value)


def test_no_file_says_nothing_about_renaming():
    with pytest.raises(TenantEnvironmentUnset) as caught:
        for_tenant(get_settings(), "example-a")
    assert "rename" not in str(caught.value)


def test_unknown_key_is_invalid():
    _write("example-a", production=False, pii_tier=2)
    with pytest.raises(TenantConfigError) as caught:
        _store().config("example-a")
    assert "pii_tier" in caught.value.detail


@pytest.mark.parametrize(
    "bad",
    [
        {"pii_filter_tier": "2"},
        {"pii_filter_tier": 4},
        {"pii_filter_tier": True},
        {"pii_extra_fields": {"PerPersonal": {"customString6": 4}}},
        {"pii_extra_fields": ["PerPersonal"]},
        {"host": 1, "token_url": _FILE_TOKEN_URL},
        {"client_key": 123},
        {"user_id": ["APIUSER"]},
        {"odata_version": "v3"},
    ],
)
def test_each_bad_type_is_invalid(bad):
    _write("example-a", production=False, **bad)
    with pytest.raises(TenantConfigError) as caught:
        _store().config("example-a")
    assert next(iter(bad)) in caught.value.detail


def test_production_with_a_lower_tier_is_invalid():
    _write("example-a", production=True, pii_filter_tier=2)
    with pytest.raises(TenantConfigError) as caught:
        _store().config("example-a")
    assert "pii_filter_tier" in caught.value.detail
    _write("example-a", production=True, pii_filter_tier=3)
    assert _store().config("example-a").production is True


def test_host_without_token_url_is_invalid():
    _write("example-a", production=False, host=_FILE_HOST)
    with pytest.raises(TenantConfigError) as caught:
        _store().config("example-a")
    assert "token_url" in caught.value.detail


def test_invalid_file_refuses_the_pii_path_as_a_vault_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "vault"))
    _write("example-a", production=False, pii_tier=2)
    with pytest.raises(PiiVaultError) as caught:
        for_tenant(Settings(), "example-a")
    assert isinstance(caught.value, TenantConfigInvalid)
    assert mcp_server._pii_error(caught.value) == {
        "error": "tenant_config_invalid",
        "company_id": "example-a",
        "detail": caught.value.detail,
    }
    assert "pii_tier" in caught.value.detail
    assert not (tmp_path / "vault").exists()


# ── PII ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env", "file", "expected"),
    [
        ("1", {"pii_filter_tier": 2}, 2),
        ("3", {}, 3),
        (None, {}, 1),
        ("1", {"pii_filter_tier": 0}, 0),
    ],
)
def test_test_tenant_tier_comes_from_file_then_env_then_one(
    monkeypatch, tmp_path, env, file, expected
):
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "vault"))
    if env is not None:
        monkeypatch.setenv("PII_FILTER_TIER", env)
    _write("example-a", production=False, **file)
    pii = for_tenant(Settings(), "example-a")
    assert (pii.tier if pii else 0) == expected


def test_production_tenant_is_tier_three(monkeypatch, tmp_path):
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("PII_FILTER_TIER", "1")
    _write("example-a", production=True, pii_filter_tier=3)
    assert for_tenant(Settings(), "example-a").tier == 3


def test_extra_fields_merge_with_the_file_winning_per_field(monkeypatch, tmp_path):
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv(
        "PII_EXTRA_FIELDS", '{"PerPersonal": {"customString6": 2, "customString7": 2}}'
    )
    _write(
        "example-a",
        production=False,
        pii_extra_fields={"PerPersonal": {"customString6": 3}, "PerEmail": {"customString1": 1}},
    )
    pii = for_tenant(Settings(), "example-a")
    assert pii._map["PerPersonal"]["customString6"] == 3
    assert pii._map["PerPersonal"]["customString7"] == 2
    assert pii._map["PerEmail"]["customString1"] == 1
    assert Settings().pii_extra_fields["PerPersonal"]["customString6"] == 2


# ── connection ────────────────────────────────────────────────────────────


def _clients(handler=lambda request: httpx.Response(200)):
    settings = Settings(
        _env_file=None,
        sf_host="api.example.invalid",
        sf_company_id="example-a",
        sf_client_key="env-key",
        sf_user_id="ENVUSER",
        sf_token_url="https://api.example.invalid/oauth/token",
        sf_private_key_pem=_KEY_PEM,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ODataClient(settings, http), SFAPIClient(settings, http)


def _file_connection(company_id, **extra):
    return _write(
        company_id,
        production=False,
        host=_FILE_HOST,
        token_url=_FILE_TOKEN_URL,
        client_key=f"key-{company_id}",
        user_id="FILEUSER",
        **extra,
    )


def test_resolve_picks_the_tenant_file_for_odata_and_sfapi():
    _file_connection("example-b", odata_version="v4")
    odata, sfapi = _clients()
    r = odata._resolve(ODataConnectionConfig(company_id="example-b"))
    assert (r["host"], r["token_url"], r["client_key"], r["user_id"], r["version"]) == (
        _FILE_HOST,
        _FILE_TOKEN_URL,
        "key-example-b",
        "FILEUSER",
        "v4",
    )
    r = sfapi._resolve(SFAPIConnectionConfig(company_id="example-b"))
    assert (r["host"], r["token_url"], r["client_key"], r["user_id"]) == (
        _FILE_HOST,
        _FILE_TOKEN_URL,
        "key-example-b",
        "FILEUSER",
    )


def test_request_override_beats_file_beats_env():
    _write("example-a", production=False, client_key="file-key")
    odata, sfapi = _clients()
    for client, config in ((odata, ODataConnectionConfig), (sfapi, SFAPIConnectionConfig)):
        r = client._resolve(None)  # the default tenant reads SF_COMPANY_ID's file
        assert (r["client_key"], r["user_id"], r["host"]) == (
            "file-key",
            "ENVUSER",
            "api.example.invalid",
        )
        r = client._resolve(config(client_key="request-key"))
        assert r["client_key"] == "request-key"
    assert odata._resolve(None)["version"] == "v2"


def test_a_disallowed_host_in_the_file_raises_the_policy_error():
    _write("example-a", production=False, host="evil.invalid", token_url="https://evil.invalid/t")
    for client in _clients():
        with pytest.raises(ConnectionPolicyError):
            client._resolve(None)


def test_host_without_token_url_refuses_the_connection():
    _write("example-a", host=_FILE_HOST)  # no production flag: still refused
    for client in _clients():
        with pytest.raises(ConnectionPolicyError, match="token_url"):
            client._resolve(None)


@pytest.mark.parametrize("extra", [{"production": True, "pii_filter_tier": 1}, {"pii_tier": 2}, {}])
def test_connection_uses_the_file_even_when_the_rest_is_invalid_or_unset(extra):
    path = _write("example-a", client_key="file-key", **extra)
    if not extra:
        path.write_text('{"client_key": "file-key"}')  # no production flag
    for client in _clients():
        assert client._resolve(None)["client_key"] == "file-key"


def test_rest_override_host_beats_the_file():
    _file_connection("example-a")
    odata, _ = _clients()
    r = odata._resolve(ODataConnectionConfig(host="api.example.invalid", client_key="k"))
    assert (r["host"], r["client_key"], r["user_id"]) == ("api.example.invalid", "k", "FILEUSER")


# ── MCP: one server, two tenants ──────────────────────────────────────────


def _soap(inner):
    return (
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<SOAP-ENV:Body>{inner}</SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )


def test_one_server_serves_two_tenants_with_their_own_key_and_tier(monkeypatch, tmp_path):
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("PII_FILTER_TIER", "1")
    monkeypatch.setenv("SF_COMPANY_ID", "example-a")
    monkeypatch.setenv("SF_CLIENT_KEY", "env-key")
    monkeypatch.setenv("SF_TOKEN_URL", "https://api.example.invalid/oauth/token")
    monkeypatch.setenv("SF_PRIVATE_KEY_PEM", _KEY_PEM)
    get_settings.cache_clear()
    _write("example-a", production=False, client_key="key-a")
    _write("example-b", production=False, client_key="key-b", pii_filter_tier=2)

    minted: list[tuple[str, str]] = []

    async def fetch_token(**kwargs):
        minted.append((kwargs["company_id"], kwargs["client_key"]))
        return f"tok-{kwargs['client_key']}"

    monkeypatch.setattr(saml_bearer, "fetch_token", fetch_token)
    bearers: list[str] = []

    def handler(request):
        if request.url.path.startswith("/sfapi/"):
            if request.headers.get("SOAPAction") == "login":
                bearers.append(request.headers["Authorization"])
                return httpx.Response(200, text=_soap("<sessionId>s</sessionId>"))
            return httpx.Response(
                200,
                text=_soap(
                    "<queryResponse><numResults>0</numResults><hasMore>false</hasMore>"
                    "</queryResponse>"
                ),
            )
        bearers.append(request.headers["Authorization"])
        record = {"__metadata": {"type": "SFOData.PerEmail"}, "emailAddress": "a@example.invalid"}
        return httpx.Response(200, json={"d": {"results": [record]}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    clients = ODataClient(get_settings(), http), SFAPIClient(get_settings(), http)
    monkeypatch.setattr(mcp_server, "_clients", lambda: clients)

    def query(company_id):
        return asyncio.run(
            mcp_server.odata_query("PerEmail", company_id=company_id, max_pages=1, preview=1)
        )

    a, b = query(""), query("example-b")
    assert a["preview"][0]["emailAddress"] == "a@example.invalid" and a["pii_filter_tier"] == 1
    assert re.fullmatch(r"\[PII-T2-[0-9a-f]{16}\]", b["preview"][0]["emailAddress"])
    assert b["pii_filter_tier"] == 2
    query("example-a")  # cached: no third token
    assert minted == [("example-a", "key-a"), ("example-b", "key-b")]
    assert bearers == ["Bearer tok-key-a", "Bearer tok-key-b", "Bearer tok-key-a"]

    minted.clear()
    bearers.clear()
    for company_id in ("example-a", "example-b"):
        assert "error" not in asyncio.run(mcp_server.ce_query(company_id=company_id))
    assert minted == [("example-a", "key-a"), ("example-b", "key-b")]
    assert bearers == ["Bearer tok-key-a", "Bearer tok-key-b"]
