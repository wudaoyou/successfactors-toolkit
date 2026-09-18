"""Per-request connection overrides may not point the server anywhere it likes."""

import asyncio
import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.main import app
from successfactors_toolkit.models.common import ODataConnectionConfig
from successfactors_toolkit.services import saml_bearer
from successfactors_toolkit.services.connection_policy import (
    ConnectionPolicyError,
    check_host,
    check_key_path,
    check_token_url,
)
from successfactors_toolkit.services.odata_client import ODataClient


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        sf_host="api.example.invalid",
        sf_allowed_hosts=["partner.example.invalid"],
        sf_company_id="demo",
        sf_private_key_pem=base64.b64encode(b"synthetic-key").decode(),
        tenant_keys_dir=str(tmp_path / "tenants"),
    )


@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "host",
    [
        "api.example.invalid",  # the configured SF_HOST
        "partner.example.invalid",  # SF_ALLOWED_HOSTS entry
        "api4preview.sapsf.com",  # SAP datacenter suffix
        "API.SUCCESSFACTORS.EU",  # suffix match is case-insensitive
    ],
)
def test_allowed_hosts_pass(host, settings):
    assert check_host(host, settings) == host


@pytest.mark.parametrize(
    "host",
    [
        "evil.invalid",
        "https://api.example.invalid",
        "api.example.invalid/path",
        "user@api.example.invalid",
        "api.example.invalid:8443",
        "api.example.invalid evil.invalid",
        "",
        "sapsf.com.evil.invalid",  # suffix must terminate the hostname
    ],
)
def test_foreign_or_malformed_hosts_are_rejected(host, settings):
    with pytest.raises(ConnectionPolicyError):
        check_host(host, settings)


def test_token_url_must_be_https_on_an_allowed_host(settings):
    url = "https://api.example.invalid/oauth/token"
    assert check_token_url(url, settings) == url
    for bad in (
        "http://api.example.invalid/oauth/token",
        "https://evil.invalid/oauth/token",
        "https://api.example.invalid:8443/oauth/token",
        "https://user:pw@api.example.invalid/oauth/token",
        "api.example.invalid/oauth/token",
    ):
        with pytest.raises(ConnectionPolicyError):
            check_token_url(bad, settings)


def test_key_path_must_stay_inside_the_tenant_store(settings, tmp_path):
    keys_dir = tmp_path / "tenants"
    (keys_dir / "demo").mkdir(parents=True)
    inside = keys_dir / "demo" / "sf_private_key_demo.pem"
    inside.write_bytes(b"synthetic-key")
    assert check_key_path(str(inside), settings) == inside.resolve()

    outside = tmp_path / "elsewhere.pem"
    outside.write_bytes(b"synthetic-key")
    with pytest.raises(ConnectionPolicyError):
        check_key_path(str(outside), settings)
    with pytest.raises(ConnectionPolicyError):
        check_key_path(str(keys_dir / ".." / "elsewhere.pem"), settings)


def test_symlink_out_of_the_tenant_store_is_rejected(settings, tmp_path):
    keys_dir = tmp_path / "tenants"
    keys_dir.mkdir()
    outside = tmp_path / "elsewhere.pem"
    outside.write_bytes(b"synthetic-key")
    link = keys_dir / "escape.pem"
    link.symlink_to(outside)
    with pytest.raises(ConnectionPolicyError):
        check_key_path(str(link), settings)


def test_rest_rejects_a_foreign_host_with_400(api_client):
    response = api_client.post(
        "/api/odata/execute",
        headers={"X-API-Key": "test-api-key"},
        json={"path": "User", "connection": {"host": "evil.invalid"}},
    )
    assert response.status_code == 400
    assert "not allowed" in response.json()["detail"]


def test_rest_rejects_a_private_key_path_outside_the_store_with_400(api_client, tmp_path):
    outside = tmp_path / "id_rsa"
    outside.write_bytes(b"synthetic-key")
    response = api_client.post(
        "/api/odata/execute",
        headers={"X-API-Key": "test-api-key"},
        json={
            "path": "User",
            "connection": {"company_id": "demo", "private_key_path": str(outside)},
        },
    )
    assert response.status_code == 400
    assert "tenant key store" in response.json()["detail"]


# ── Adversarial review follow-ups ─────────────────────────────────────────────


def test_configured_settings_are_trusted_even_when_unusual():
    """SF_HOST/SF_TOKEN_URL are operator input, not caller input.

    An internal hostname with an underscore, or a token endpoint on a
    non-default port, is a legal deployment; running the caller allowlist over
    the server's own configuration rejects every request instead.
    """
    settings = Settings(
        _env_file=None,
        sf_host="sf_internal.corp.invalid",
        sf_token_url="https://sf_internal.corp.invalid:8443/oauth/token",
    )
    assert check_host(settings.sf_host, settings) == settings.sf_host
    assert check_token_url(settings.sf_token_url, settings) == settings.sf_token_url
    # A per-request override still has to earn it.
    with pytest.raises(ConnectionPolicyError):
        check_host("other_internal.corp.invalid", settings)


def test_unparseable_overrides_are_policy_errors_not_crashes(settings):
    """A rejected override must reach the 400 handler, not escape as a 500."""
    with pytest.raises(ConnectionPolicyError):
        check_token_url("https://[::1", settings)  # urlsplit raises ValueError
    with pytest.raises(ConnectionPolicyError):
        check_key_path("\x00", settings)  # Path.resolve raises ValueError


def test_rest_rejects_an_unparseable_private_key_path_with_400(api_client):
    response = api_client.post(
        "/api/odata/execute",
        headers={"X-API-Key": "test-api-key"},
        json={
            "path": "User",
            "connection": {"company_id": "demo", "private_key_path": "\x00"},
        },
    )
    assert response.status_code == 400


def test_token_url_is_checked_against_the_clients_own_settings(settings, monkeypatch):
    """The signed assertion is a bearer credential: the allowlist that gates
    where it is POSTed must be the one the client was built with, not whatever
    the process environment happens to say."""
    monkeypatch.setenv("SF_ALLOWED_HOSTS", '["legacy.example.invalid"]')
    get_settings.cache_clear()
    assert "legacy.example.invalid" in get_settings().sf_allowed_hosts
    assert "legacy.example.invalid" not in settings.sf_allowed_hosts

    class _NoPost:
        async def post(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("assertion POSTed to a host the caller's policy forbids")

    with pytest.raises(ConnectionPolicyError):
        asyncio.run(
            saml_bearer.fetch_token(
                http_client=_NoPost(),
                client_key="demo-key",
                user_id="EXAMPLE001",
                company_id="demo",
                token_url="https://legacy.example.invalid/oauth/token",
                private_key_pem=b"",
                settings=settings,
            )
        )


class _RecordingHTTPClient:
    """Captures what the OData client actually puts on the wire."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.url = ""

    async def request(self, *, method, url, headers, params, json, timeout):
        self.url = url
        self.headers = headers
        return httpx.Response(200, text="", request=httpx.Request(method, url))


def test_caller_headers_cannot_retarget_or_reauthorize_the_request():
    """`headers` on /api/odata/execute is caller input. httpx sends a supplied
    Host verbatim, so letting it through makes the allowlisted hostname not the
    host the request actually addresses."""
    settings = Settings(
        _env_file=None,
        sf_host="api.example.invalid",
        sf_company_id="demo",
        sf_private_key_pem=base64.b64encode(b"synthetic-key").decode(),
        tenant_keys_dir="/nonexistent",
    )
    http = _RecordingHTTPClient()
    client = ODataClient(settings, http)
    client._tokens[client._token_key(client._resolve(None))] = ("tok", float("inf"))

    asyncio.run(
        client.request(
            "GET",
            "User",
            extra_headers={
                "Host": "internal.corp.invalid",
                "authorization": "Bearer attacker-supplied",
                "Accept": "application/json;odata=verbose",
            },
        )
    )

    assert "Host" not in http.headers and "host" not in http.headers
    assert http.headers["Authorization"] == "Bearer tok"
    assert "authorization" not in http.headers
    # Harmless caller headers still get through.
    assert http.headers["Accept"] == "application/json;odata=verbose"


@pytest.mark.parametrize(
    ("path", "version"),
    [
        ("../../sfapi/v1/soap", "v2"),
        ("%2e%2e/%2e%2e/sfapi/v1/soap", "v2"),
        ("..%2f..%2fsfapi/v1/soap", "v2"),
        ("%252e%252e/%252e%252e/sfapi/v1/soap", "v2"),
        ("..\\..\\sfapi\\v1\\soap", "v2"),
        ("%2e%2e%5c%2e%2e%5csfapi", "v2"),
        ("https://evil.invalid/", "v2"),
        ("User", "../sfapi"),
        ("User", "v2/../../sfapi"),
        ("User", "%76%32"),
    ],
)
def test_odata_request_cannot_escape_the_api_root(settings, path, version):
    http = _RecordingHTTPClient()
    client = ODataClient(settings, http)

    with pytest.raises(ConnectionPolicyError):
        asyncio.run(
            client.request(
                "GET",
                path,
                conn=ODataConnectionConfig(odata_version=version),
            )
        )

    assert http.url == ""


@pytest.mark.parametrize(
    "path", ["User", "/User", "$metadata", "EmpJob/$metadata", "User?$top=10", "User('a%2Fb')"]
)
def test_odata_request_preserves_safe_paths(settings, path):
    http = _RecordingHTTPClient()
    client = ODataClient(settings, http)
    resolved = client._resolve(None)
    client._tokens[client._token_key(resolved)] = ("tok", float("inf"))

    asyncio.run(client.request("GET", path))

    assert http.url.startswith("https://api.example.invalid/odata/v2/")


def test_odata_request_preserves_v4(settings):
    http = _RecordingHTTPClient()
    client = ODataClient(settings, http)
    resolved = client._resolve(ODataConnectionConfig(odata_version="v4"))
    client._tokens[client._token_key(resolved)] = ("tok", float("inf"))

    asyncio.run(client.request("GET", "User", conn=ODataConnectionConfig(odata_version="v4")))

    assert http.url == "https://api.example.invalid/odata/v4/User"
