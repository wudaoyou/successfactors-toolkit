"""Response headers, login errors, token lifetime and locking in the SF HTTP clients."""

import asyncio
import base64
from unittest.mock import AsyncMock

import httpx

from successfactors_toolkit.config import Settings
from successfactors_toolkit.services.http_limits import passthrough_headers
from successfactors_toolkit.services.odata_client import ODataClient
from successfactors_toolkit.services.sfapi_client import SFAPIClient


def _settings(**overrides):
    return Settings(
        _env_file=None,
        sf_host="api.example.invalid",
        sf_company_id="example-a",
        sf_private_key_pem=base64.b64encode(b"synthetic-key").decode(),
        **overrides,
    )


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
