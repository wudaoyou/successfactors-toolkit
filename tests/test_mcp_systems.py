"""MCP tools and the plugin API on SYSTEMS_DIR."""

import asyncio
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from successfactors_toolkit import mcp_server, plugin_api
from successfactors_toolkit.config import get_settings
from successfactors_toolkit.services import pii_filter, saml_bearer, system_store
from successfactors_toolkit.services.odata_client import ODataClient
from successfactors_toolkit.services.sfapi_client import SFAPIClient
from tests.systems import Widget, write_system


def _dir():
    return get_settings().systems_dir


def _pem_pair(key=None, not_before=timedelta(minutes=-1), valid_for=timedelta(days=7)):
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-test-user")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + not_before)
        .not_valid_after(now + valid_for)
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
        cert.public_bytes(serialization.Encoding.PEM),
    )


@pytest.fixture
def keypair():
    return _pem_pair()


def _system_with_keypair(name, keypair, **fields):
    directory = write_system(_dir(), name, **fields)
    (directory / "private-key.pem").write_bytes(keypair[0])
    (directory / "signing-cert.crt").write_bytes(keypair[1])
    return directory


def test_list_systems_lists_each_file_without_the_client_key(widget_type):
    write_system(_dir(), "w", {"type": "widget", "production": True})
    write_system(_dir(), "broken", {"production": False})
    out = mcp_server.list_systems()
    rows = {row["name"]: row for row in out["systems"]}
    assert rows["example-a"]["type"] == "successfactors"
    assert rows["example-a"]["company_id"] == "example-a"
    assert rows["example-a"]["pii_filter_tier"] == 1
    assert all("client_key" not in row for row in out["systems"])
    assert rows["w"] == {
        "name": "w",
        "type": "widget",
        "production": True,
        "pii_filter_tier": 3,
        "colour": "blue",
    }
    assert rows["broken"]["error"] == "system_invalid"
    assert any(w.startswith("broken:") for w in out["warnings"])


def test_odata_query_with_two_systems_needs_a_name():
    write_system(_dir(), "example-b")
    out = asyncio.run(mcp_server.odata_query("User"))
    assert out["error"] == "system_required" and "example-b" in out["detail"]


def test_odata_query_refuses_a_system_of_another_type(widget_type):
    write_system(_dir(), "w", {"type": "widget", "production": False})
    out = asyncio.run(mcp_server.odata_query("User", system="w"))
    assert out["error"] == "system_unknown" and out["system"] == "w"


def test_tier_follows_each_systems_own_file():
    write_system(_dir(), "prod", production=True)
    write_system(_dir(), "two", pii_filter_tier=2)
    write_system(_dir(), "off", pii_filter_tier=0)
    settings = get_settings()
    assert pii_filter.for_system(settings, "prod").tier == 3
    assert pii_filter.for_system(settings, "two").tier == 2
    assert pii_filter.for_system(settings, "example-a").tier == 1
    assert pii_filter.for_system(settings, "off") is None


def test_tokens_stay_with_the_system_that_issued_them_even_for_one_company():
    write_system(_dir(), "a", company_id="acme")
    write_system(_dir(), "b", company_id="acme")
    settings = get_settings()
    a, b = pii_filter.for_system(settings, "a"), pii_filter.for_system(settings, "b")
    hex_a = a.vault.digest("Jane")
    assert hex_a != b.vault.digest("Jane")
    a.vault.save({hex_a: "Jane"})
    assert b.vault.load([hex_a]) == {}


def test_extra_fields_come_from_each_systems_own_file():
    write_system(_dir(), "a", pii_extra_fields={"PerPersonal": {"customString6": 3}})
    write_system(_dir(), "b")
    settings = get_settings()
    assert pii_filter.for_system(settings, "a")._map["PerPersonal"]["customString6"] == 3
    assert "customString6" not in pii_filter.for_system(settings, "b")._map.get("PerPersonal", {})


def test_an_invalid_system_refuses_the_pii_path_as_a_vault_error():
    write_system(_dir(), "bad", {"type": "successfactors", "production": False})
    with pytest.raises(pii_filter.PiiVaultError) as info:
        pii_filter.for_system(get_settings(), "bad")
    assert mcp_server._pii_error(info.value) == {
        "error": "system_invalid",
        "system": "bad",
        "detail": info.value.detail,
    }


def test_plugin_api_selects_and_reads_a_registered_type(monkeypatch):
    monkeypatch.setattr(system_store, "_TYPES", dict(system_store._TYPES))
    plugin_api.register_system_type("widget", Widget)
    write_system(_dir(), "w", {"type": "widget", "production": False, "colour": "red"})
    assert plugin_api.select_system("widget") == "w"
    assert plugin_api.system_config("w").colour == "red"
    assert plugin_api.system_dir("w") == _dir() / "w"
    with pytest.raises(plugin_api.SystemUnavailable) as info:
        plugin_api.select_system("widget", "example-a")
    assert plugin_api.system_error(info.value)["error"] == "system_unknown"


def test_a_plugin_that_fails_after_registering_a_type_leaves_none(monkeypatch):
    class _EntryPoint:
        name = "broken"

        def load(self):
            def register(mcp):
                plugin_api.register_system_type("widget", Widget)
                raise RuntimeError("boom")

            return register

    monkeypatch.setattr(system_store, "_TYPES", dict(system_store._TYPES))
    monkeypatch.setattr(plugin_api, "entry_points", lambda group: [_EntryPoint()])
    monkeypatch.setattr(plugin_api, "_plugins", {})
    monkeypatch.setattr(plugin_api, "_status_fns", {})
    plugin_api._load_plugins(mcp_server.mcp)
    assert "widget" not in system_store._TYPES
    assert plugin_api._plugins["broken"]["loaded"] is False


def _soap(inner):
    return (
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<SOAP-ENV:Body>{inner}</SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )


def test_one_server_serves_two_systems_with_their_own_key_and_tier(monkeypatch, tmp_path):
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "vault"))
    get_settings.cache_clear()
    write_system(_dir(), "example-a", client_key="key-a")
    directory = write_system(_dir(), "example-b", client_key="key-b", pii_filter_tier=2)
    (directory / "private-key.pem").write_bytes(b"synthetic-key")

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

    def query(system):
        return asyncio.run(
            mcp_server.odata_query("PerEmail", system=system, max_pages=1, preview=1)
        )

    a, b = query("example-a"), query("example-b")
    assert a["preview"][0]["emailAddress"] == "a@example.invalid" and a["pii_filter_tier"] == 1
    assert re.fullmatch(r"\[PII-T2-[0-9a-f]{16}\]", b["preview"][0]["emailAddress"])
    assert b["pii_filter_tier"] == 2
    query("example-a")  # cached: no third token
    assert minted == [("example-a", "key-a"), ("example-b", "key-b")]
    assert bearers == ["Bearer tok-key-a", "Bearer tok-key-b", "Bearer tok-key-a"]

    minted.clear()
    bearers.clear()
    for system in ("example-a", "example-b"):
        assert "error" not in asyncio.run(mcp_server.ce_query(system=system))
    assert minted == [("example-a", "key-a"), ("example-b", "key-b")]
    assert bearers == ["Bearer tok-key-a", "Bearer tok-key-b"]


def test_list_systems_reports_environment_tier_and_errors(keypair):
    _system_with_keypair("example-a", keypair, production=True)
    (_system_with_keypair("example-b", keypair) / "example-b.json").write_text(
        '{"type": "successfactors"}'
    )
    _system_with_keypair("example-c", keypair, pii_filter_tier=2)
    result = mcp_server.list_systems()
    a, b, c = result["systems"]
    assert (a["production"], a["pii_filter_tier"]) == (True, 3)
    assert (b["production"], b["error"]) == (None, "system_invalid")
    assert (c["production"], c["pii_filter_tier"]) == (False, 2)
    [warning] = result["warnings"]
    assert warning.startswith("example-b: system_invalid:") and "production" in warning

    write_system(_dir(), "example-b")
    result = mcp_server.list_systems()
    assert result["systems"][1]["pii_filter_tier"] == 1
    assert "warnings" not in result


def test_list_systems_reports_each_files_settings_and_cert(keypair):
    _system_with_keypair(
        "example-a",
        keypair,
        pii_filter_tier=2,
        host="api4preview.sapsf.com",
        token_url="https://api4preview.sapsf.com/oauth/token",
        user_id="FILEUSER",
        odata_version="v4",
    )
    write_system(_dir(), "example-b", host=1)
    result = mcp_server.list_systems()
    a, b = result["systems"]
    assert (a["host"], a["user_id"], a["odata_version"], a["pii_filter_tier"]) == (
        "api4preview.sapsf.com",
        "FILEUSER",
        "v4",
        2,
    )
    assert a["cert_days_left"] > 0 and a["cert_expires"]
    assert "client_key" not in a and "synthetic-client-key" not in str(result)
    assert (b["error"], b["type"]) == ("system_invalid", "successfactors")
    assert "cert_expires" not in b
    assert any(w.startswith("example-b:") and "host" in w for w in result["warnings"])
