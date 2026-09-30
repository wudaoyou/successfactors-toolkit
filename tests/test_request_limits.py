"""Response size, request duration, retry waits and extract size are bounded."""

import asyncio
import base64
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from successfactors_toolkit.config import Settings
from successfactors_toolkit.models.odata import ODataExtractRequest
from successfactors_toolkit.services import http_limits
from successfactors_toolkit.services.http_limits import ExtractBudget, LimitExceededError
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


def odata_client(handler, monkeypatch, **overrides):
    client = ODataClient(
        _settings(**overrides), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(client, "_get_token", AsyncMock(return_value="synthetic-token"))
    return client


def test_response_over_the_byte_cap_is_an_error(monkeypatch):
    client = odata_client(
        lambda request: httpx.Response(200, content=b"x" * 500), monkeypatch, max_response_bytes=100
    )
    with pytest.raises(LimitExceededError, match="exceeded 100 bytes") as raised:
        asyncio.run(client.request("GET", "User"))
    assert raised.value.status_code == 413


def test_response_at_the_byte_cap_is_returned_whole(monkeypatch):
    client = odata_client(
        lambda request: httpx.Response(200, content=b"x" * 100), monkeypatch, max_response_bytes=100
    )
    assert asyncio.run(client.request("GET", "User"))["body"] == "x" * 100


def test_compressed_response_is_capped_after_decompression(monkeypatch):
    import gzip

    payload = gzip.compress(b"x" * 10_000)  # tiny on the wire, large once decoded
    client = odata_client(
        lambda request: httpx.Response(200, content=payload, headers={"content-encoding": "gzip"}),
        monkeypatch,
        max_response_bytes=1000,
    )
    with pytest.raises(LimitExceededError):
        asyncio.run(client.request("GET", "User"))


def test_sfapi_response_over_the_byte_cap_is_an_error():
    client = SFAPIClient(
        _settings(max_response_bytes=100),
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="x" * 500))
        ),
    )
    client._sessions[client._session_key(client._resolve(None))] = "SESSION"
    with pytest.raises(LimitExceededError):
        asyncio.run(client.query("SELECT person FROM CompoundEmployee"))


def test_a_slow_response_trips_the_overall_timeout(monkeypatch):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, text="{}")

    monkeypatch.setattr(http_limits, "MAX_REQUEST_SECONDS", 0.05)
    client = odata_client(slow, monkeypatch)
    with pytest.raises(LimitExceededError, match="including retries") as raised:
        asyncio.run(client.request("GET", "User"))
    assert raised.value.status_code == 504


def test_a_slow_sfapi_response_trips_the_overall_timeout(monkeypatch):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, text="<r/>")

    monkeypatch.setattr(http_limits, "MAX_REQUEST_SECONDS", 0.05)
    client = SFAPIClient(_settings(), httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    client._sessions[client._session_key(client._resolve(None))] = "SESSION"
    with pytest.raises(LimitExceededError):
        asyncio.run(client.query("SELECT person FROM CompoundEmployee"))


def test_rate_limit_waits_stop_at_the_total_sleep_cap(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"retry-after": "200"}, text="slow down")

    client = odata_client(handler, monkeypatch)
    sleep = AsyncMock()
    monkeypatch.setattr("successfactors_toolkit.services.odata_client.asyncio.sleep", sleep)

    result = asyncio.run(client.request("GET", "User"))

    # One 200s wait fits under the 300s cap; a second would not, so the 429 is returned.
    assert result["status_code"] == 429
    assert len(requests) == 2
    assert sleep.await_count == 1


def test_max_pages_above_the_ceiling_is_rejected():
    ODataExtractRequest(path="User", max_pages=1000)
    with pytest.raises(ValidationError):
        ODataExtractRequest(path="User", max_pages=1001)


def test_too_many_filter_values_are_rejected_before_any_request(monkeypatch):
    def handler(request):
        pytest.fail("no request expected")

    client = odata_client(handler, monkeypatch, max_filter_values=3)
    with pytest.raises(LimitExceededError, match="4 distinct values") as raised:
        asyncio.run(client.extract_by_filter_in("FOCompany", "externalCode", ["a", "b", "c", "d"]))
    assert raised.value.status_code == 413


def test_repeated_filter_values_count_once(monkeypatch):
    client = odata_client(
        lambda request: httpx.Response(200, json={"d": {"results": []}}),
        monkeypatch,
        max_filter_values=3,
    )
    result = asyncio.run(
        client.extract_by_filter_in("FOCompany", "externalCode", ["a", "b", "c"] * 10)
    )
    assert result["chunks_processed"] == 1


def test_extract_stops_with_an_error_at_the_total_byte_cap(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "d": {
                    "results": [{"id": "x" * 40}],
                    "__next": "https://api.example.invalid/odata/v2/User?$skiptoken=n",
                }
            },
        )

    client = odata_client(handler, monkeypatch, max_extract_bytes=150)
    with pytest.raises(LimitExceededError, match="exceeded 150 bytes of response data"):
        asyncio.run(client.extract_all("User", max_pages=50))


def test_extract_budget_time_limit(monkeypatch):
    budget = ExtractBudget(_settings(max_extract_seconds=10))
    budget.check_time()
    now = http_limits.time.monotonic()
    monkeypatch.setattr(http_limits.time, "monotonic", lambda: now + 11)
    with pytest.raises(LimitExceededError, match="exceeded 10s") as raised:
        budget.check_time()
    assert raised.value.status_code == 504
