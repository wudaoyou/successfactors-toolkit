"""{company_id}/{company_id}.json: one server, per-tenant connection and PII settings."""

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from successfactors_toolkit import mcp_server
from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.services import saml_bearer
from successfactors_toolkit.services.odata_client import ODataClient
from successfactors_toolkit.services.pii_filter import (
    PiiVaultError,
    TenantConfigInvalid,
    TenantEnvironmentUnset,
    for_tenant,
)
from successfactors_toolkit.services.sfapi_client import SFAPIClient
from successfactors_toolkit.services.tenant_store import TenantConfigError, TenantStore
from tests.systems import write_system

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
    get_settings.cache_clear()
    for name in ("example-a", "example-b"):
        directory = write_system(get_settings().systems_dir, name, client_key=f"key-{name[-1]}")
        (directory / "private-key.pem").write_bytes(b"synthetic-key")
    _write("example-a", production=False)
    _write("example-b", production=False, pii_filter_tier=2)

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

    a, b = query("example-a"), query("example-b")
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
