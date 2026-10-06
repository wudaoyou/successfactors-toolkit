"""Response headers, login errors, token lifetime and locking in the SF HTTP clients."""

import asyncio
import gc
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from successfactors_toolkit.config import Settings
from successfactors_toolkit.models.common import ODataConnectionConfig, SFAPIConnectionConfig
from successfactors_toolkit.services import saml_bearer
from successfactors_toolkit.services.http_limits import passthrough_headers
from successfactors_toolkit.services.odata_client import ODataClient
from successfactors_toolkit.services.sfapi_client import SFAPIClient
from tests.systems import write_system


def _settings():
    return Settings(_env_file=None)  # SYSTEMS_DIR holds conftest's example-a


def _add_system(name):
    directory = write_system(_settings().systems_dir, name)
    (directory / "private-key.pem").write_bytes(b"synthetic-key")


def _odata(handler, monkeypatch):
    client = ODataClient(_settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(client, "_get_token", AsyncMock(return_value="synthetic-token"))
    return client


def _sfapi(handler):
    client = SFAPIClient(_settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client._sessions[client._session_key(client._resolve(None))] = "SESSION"
    return client


# ── response headers (#86) ────────────────────────────────────────────────

_SF_HEADERS = [
    ("Content-Type", "application/json"),
    ("ETag", 'W/"abc"'),
    ("Location", "https://api.example.invalid/odata/v2/User('x')"),
    ("Set-Cookie", "JSESSIONID=secret; Path=/; Secure"),
    ("Set-Cookie", "route=other; Path=/"),
    ("X-CSRF-Token", "csrf-secret"),
    ("Server", "internal-build-1.2"),
]
_SAFE = {
    "content-type": "application/json",
    "etag": 'W/"abc"',
    "location": "https://api.example.invalid/odata/v2/User('x')",
}


def test_helper_keeps_only_allowlisted_headers_lowercased():
    assert passthrough_headers(httpx.Headers(_SF_HEADERS)) == _SAFE


def test_odata_response_headers_are_allowlisted(monkeypatch):
    client = _odata(lambda r: httpx.Response(200, headers=_SF_HEADERS, text="{}"), monkeypatch)
    assert asyncio.run(client.request("GET", "User"))["headers"] == _SAFE


def test_odata_extract_last_headers_are_allowlisted(monkeypatch):
    client = _odata(
        lambda r: httpx.Response(200, headers=_SF_HEADERS, json={"d": {"results": []}}),
        monkeypatch,
    )
    assert asyncio.run(client.extract_all("User"))["last_headers"] == _SAFE


def test_sfapi_response_headers_are_allowlisted():
    client = _sfapi(lambda r: httpx.Response(200, headers=_SF_HEADERS, text="<r/>"))
    result = asyncio.run(client.query("SELECT person FROM CompoundEmployee"))
    assert result["headers"] == _SAFE


def test_odata_retries_on_retry_after_though_responses_are_filtered(monkeypatch):
    """The allowlist applies to what is returned, not to what the client reads."""
    responses = iter(
        [httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(200, text="{}")]
    )
    client = _odata(lambda r: next(responses), monkeypatch)
    sleep = AsyncMock()
    monkeypatch.setattr("successfactors_toolkit.services.odata_client.asyncio.sleep", sleep)
    assert asyncio.run(client.request("GET", "User"))["status_code"] == 200
    assert 7 <= sleep.await_args.args[0] <= 7.5


# ── login errors (#92) ────────────────────────────────────────────────────


def test_failed_sfapi_login_keeps_the_status_but_not_the_response_body(monkeypatch):
    monkeypatch.setattr(saml_bearer, "fetch_token", AsyncMock(return_value="tok"))
    client = SFAPIClient(
        _settings(),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(401, text="<fault>jane.doe@example.invalid denied</fault>")
            )
        ),
    )
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(client.query("SELECT person FROM CompoundEmployee"))
    assert "HTTP 401" in str(raised.value)
    assert "jane.doe" not in str(raised.value)
    assert "fault" not in str(raised.value)


# ── token lifetime (#92) ──────────────────────────────────────────────────


def _pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def test_fetch_token_carries_expires_in():
    token_response = {"access_token": "tok", "expires_in": 3600}
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=token_response))
    )
    token = asyncio.run(
        saml_bearer.fetch_token(
            http_client=http,
            client_key="demo-key",
            user_id="EXAMPLE001",
            company_id="demo",
            token_url="https://api.example.invalid/oauth/token",
            private_key_pem=_pem(),
            settings=_settings(),
        )
    )
    assert token == "tok"
    assert token.expires_in == 3600


@pytest.mark.parametrize(
    ("value", "expected"),
    [(3600, 3600), ("7200", 7200), (1.5, 1.5)]
    + [(v, None) for v in (None, 0, -5, "soon", "nan", "inf", float("inf"), True, [], {})],
)
def test_expires_in_must_be_a_positive_finite_number(value, expected):
    assert saml_bearer._lifetime(value) == expected


def _cache_seconds(expires_in, monkeypatch):
    """Seconds the OData client caches a token whose response said `expires_in`."""
    client = ODataClient(_settings(), httpx.AsyncClient())
    token = saml_bearer.AccessToken("tok")
    token.expires_in = expires_in
    monkeypatch.setattr(saml_bearer, "fetch_token", AsyncMock(return_value=token))
    _, exp = asyncio.run(client._fetch_new_token(client._resolve(None)))
    return exp - time.monotonic()


@pytest.mark.parametrize(
    ("expires_in", "cached"),
    [
        (86400, 82800),  # SAP default 24h: the 23h used before expires_in was read
        (3600, 3240),  # a tenth held back
        (600, 540),
        (None, 82800),  # absent or invalid: the default
    ],
)
def test_token_is_cached_for_its_own_lifetime_less_a_margin(expires_in, cached, monkeypatch):
    assert _cache_seconds(expires_in, monkeypatch) == pytest.approx(cached, abs=5)


def test_plain_string_token_gets_the_default_lifetime(monkeypatch):
    client = ODataClient(_settings(), httpx.AsyncClient())
    monkeypatch.setattr(saml_bearer, "fetch_token", AsyncMock(return_value="tok"))
    _, exp = asyncio.run(client._fetch_new_token(client._resolve(None)))
    assert exp - time.monotonic() == pytest.approx(23 * 3600, abs=5)


def test_a_short_lived_token_is_minted_again_instead_of_reused(monkeypatch):
    client = ODataClient(_settings(), httpx.AsyncClient())
    token = saml_bearer.AccessToken("tok")
    token.expires_in = 30  # inside the 60 s expiry slack from the start
    mint = AsyncMock(return_value=token)
    monkeypatch.setattr(saml_bearer, "fetch_token", mint)
    r = client._resolve(None)

    async def two():
        await client._get_token(r)
        await client._get_token(r)

    asyncio.run(two())
    assert mint.await_count == 2


# ── per-tenant locks (#92) ────────────────────────────────────────────────


def _two_tenant_fetch(release: asyncio.Event, started: list[str]):
    """A token/login stub where tenant "example-a" blocks until `release` is set."""

    async def fetch(**kwargs):
        company = kwargs["company_id"]
        started.append(company)
        if company == "example-a":
            await release.wait()
        return f"tok-{company}"

    return fetch


def test_a_slow_token_endpoint_does_not_block_another_tenant(monkeypatch):
    _add_system("example-b")
    client = ODataClient(_settings(), httpx.AsyncClient())
    a = client._resolve(ODataConnectionConfig(system="example-a"))
    b = client._resolve(ODataConnectionConfig(system="example-b"))

    async def scenario():
        release, started = asyncio.Event(), []
        monkeypatch.setattr(saml_bearer, "fetch_token", _two_tenant_fetch(release, started))
        slow = asyncio.create_task(client._get_token(a))
        await asyncio.sleep(0)
        assert await asyncio.wait_for(client._get_token(b), 1) == "tok-example-b"
        assert not slow.done()  # A is still waiting on its token endpoint
        release.set()
        assert await slow == "tok-example-a"

    asyncio.run(scenario())


def test_concurrent_token_requests_for_one_tenant_mint_once(monkeypatch):
    client = ODataClient(_settings(), httpx.AsyncClient())
    a = client._resolve(ODataConnectionConfig(system="example-a"))

    async def scenario():
        release, started = asyncio.Event(), []
        monkeypatch.setattr(saml_bearer, "fetch_token", _two_tenant_fetch(release, started))
        calls = [asyncio.create_task(client._get_token(a)) for _ in range(3)]
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(*calls) == ["tok-example-a"] * 3
        assert started == ["example-a"]

    asyncio.run(scenario())


def test_token_locks_do_not_pile_up_once_idle(monkeypatch):
    client = ODataClient(_settings(), httpx.AsyncClient())
    monkeypatch.setattr(saml_bearer, "fetch_token", AsyncMock(return_value="tok"))
    asyncio.run(client._get_token(client._resolve(None)))
    gc.collect()
    assert len(client._token_locks) == 0


def test_a_slow_sfapi_login_does_not_block_another_tenant(monkeypatch):
    _add_system("example-b")
    client = SFAPIClient(_settings(), httpx.AsyncClient())
    a = client._resolve(SFAPIConnectionConfig(system="example-a"))
    b = client._resolve(SFAPIConnectionConfig(system="example-b"))

    async def scenario():
        release, started = asyncio.Event(), []
        fetch = _two_tenant_fetch(release, started)
        monkeypatch.setattr(client, "_login", lambda r: fetch(company_id=r["company_id"]))
        slow = asyncio.create_task(client._ensure_session(a))
        await asyncio.sleep(0)
        assert await asyncio.wait_for(client._ensure_session(b), 1) == "tok-example-b"
        assert not slow.done()
        release.set()
        assert await slow == "tok-example-a"
        # a second caller for a cached tenant never logs in again
        assert await client._ensure_session(a) == "tok-example-a"
        assert started == ["example-a", "example-b"]

    asyncio.run(scenario())
