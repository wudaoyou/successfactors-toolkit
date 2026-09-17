import asyncio
import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from successfactors_toolkit.config import Settings
from successfactors_toolkit.models.common import ODataConnectionConfig
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError
from successfactors_toolkit.services.odata_client import ODataClient


def client_for(handler, monkeypatch):
    settings = Settings(
        _env_file=None,
        sf_host="api.example.invalid",
        sf_company_id="example-a",
        sf_private_key_pem=base64.b64encode(b"synthetic-key").decode(),
    )
    client = ODataClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(client, "_get_token", AsyncMock(return_value="synthetic-token"))
    monkeypatch.setattr("successfactors_toolkit.services.odata_client.asyncio.sleep", AsyncMock())
    return client


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_mutations_do_not_replay_after_server_failure(method, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, text="upstream unavailable")

    client = client_for(handler, monkeypatch)
    result = asyncio.run(client.request(method, "User('example')", body={"active": False}))
    assert result["status_code"] == 503
    assert len(requests) == 1
    assert requests[0].method == method
    assert json.loads(requests[0].content) == {"active": False}


def test_reads_retry_and_auth_refresh_is_bounded(monkeypatch):
    statuses = iter([401, 503, 200])
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(next(statuses), json={"d": {"results": []}})

    client = client_for(handler, monkeypatch)
    assert asyncio.run(client.request("GET", "User"))["status_code"] == 200
    assert len(requests) == 3
    assert client._get_token.await_count == 2
    assert client._get_token.call_args.kwargs["force_refresh"] is True


def test_cursor_pagination_keeps_filter_and_reports_cap(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        token = request.url.params.get("$skiptoken")
        return httpx.Response(
            200,
            json={
                "d": {
                    "results": [{"id": token or "first"}],
                    "__next": "https://api.example.invalid/odata/v2/User?$skiptoken=next",
                }
            },
        )

    client = client_for(handler, monkeypatch)
    result = asyncio.run(
        client.extract_all("User", params={"$filter": "active eq true"}, max_pages=2)
    )
    assert result["total_records"] == 2
    assert result["stopped_reason"] == "max_pages"
    assert result["next_skiptoken"] == "next"
    assert requests[1].url.params["$skiptoken"] == "next"
    assert requests[1].url.params["$filter"] == "active eq true"


def test_partial_results_preserved_on_later_http_error(monkeypatch):
    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "d": {
                        "results": [{"id": "first"}],
                        "__next": "https://api.example.invalid/odata/v2/User?$skiptoken=next",
                    }
                },
            ),
            httpx.Response(403, text="denied"),
        ]
    )
    client = client_for(lambda request: next(responses), monkeypatch)
    result = asyncio.run(client.extract_all("User"))
    assert result["results"] == [{"id": "first"}]
    assert result["stopped_reason"] == "http_error"
    assert result["last_status_code"] == 403


def test_token_cache_is_isolated_by_company(monkeypatch):
    client = client_for(lambda request: httpx.Response(200), monkeypatch)
    a = client._resolve(ODataConnectionConfig(company_id="example-a"))
    b = client._resolve(ODataConnectionConfig(company_id="example-b"))
    assert client._token_key(a) != client._token_key(b)


def test_resolve_rejects_a_host_outside_the_connection_policy(monkeypatch):
    client = client_for(lambda request: httpx.Response(200), monkeypatch)
    with pytest.raises(ConnectionPolicyError):
        client._resolve(ODataConnectionConfig(host="evil.invalid"))
    # A SAP datacenter host is still accepted without extra configuration.
    assert client._resolve(ODataConnectionConfig(host="api4preview.sapsf.com"))["host"] == (
        "api4preview.sapsf.com"
    )
