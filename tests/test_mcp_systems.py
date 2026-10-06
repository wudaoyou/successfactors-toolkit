"""MCP tools and the plugin API on SYSTEMS_DIR."""

import asyncio
import re

import httpx
import pytest

from successfactors_toolkit import mcp_server, plugin_api
from successfactors_toolkit.config import get_settings
from successfactors_toolkit.services import pii_filter, saml_bearer, system_store
from successfactors_toolkit.services.odata_client import ODataClient
from successfactors_toolkit.services.sfapi_client import SFAPIClient
from tests.systems import Widget, write_system


def _dir():
    return get_settings().systems_dir


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
