import asyncio
import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from successfactors_toolkit.config import Settings
from successfactors_toolkit.models.common import ODataConnectionConfig
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError
from successfactors_toolkit.services.odata_client import (
    _MAX_FILTER_ENCODED_LEN,
    _MAX_RETRY_AFTER_SECONDS,
    ODataClient,
    _encoded_len,
    _parse_retry_after,
)


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


def test_path_query_options_merge_with_explicit_params(monkeypatch):
    # "EmpJob?$filter=...&$top=..." was previously silently dropped: httpx
    # replaces rather than merges a URL's own query when `params=` is also
    # passed to Client.request(). Explicit params must still win on conflict.
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"d": {"results": []}})

    client = client_for(handler, monkeypatch)
    asyncio.run(
        client.request(
            "GET",
            "EmpJob?$filter=jobCode eq '1'&$top=5",
            params={"$select": "userId", "$top": "10"},
        )
    )

    sent = requests[0].url.params
    assert sent["$filter"] == "jobCode eq '1'"
    assert sent["$select"] == "userId"
    assert sent["$top"] == "10", "explicit params must win over the path's own value"


def test_resolve_rejects_a_host_outside_the_connection_policy(monkeypatch):
    client = client_for(lambda request: httpx.Response(200), monkeypatch)
    with pytest.raises(ConnectionPolicyError):
        client._resolve(ODataConnectionConfig(host="evil.invalid"))
    # A SAP datacenter host is still accepted without extra configuration.
    assert client._resolve(ODataConnectionConfig(host="api4preview.sapsf.com"))["host"] == (
        "api4preview.sapsf.com"
    )


def test_parse_retry_after_caps_at_300_seconds():
    assert _parse_retry_after("100000") == _MAX_RETRY_AFTER_SECONDS
    assert _parse_retry_after("120") == 120.0
    assert _parse_retry_after(None) == 1.0


def test_extract_by_filter_in_builds_bare_in_clause_and_escapes_quotes(monkeypatch):
    # No parentheses around the value list — SF 400s on `in (...)`. Values
    # are deduplicated, and an embedded single quote is doubled per OData
    # literal escaping.
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"d": {"results": []}})

    client = client_for(handler, monkeypatch)
    asyncio.run(
        client.extract_by_filter_in(
            "FOCompany",
            "externalCode",
            ["A", "B", "A", "O'Brien"],
            params={"$filter": "status eq 'A'"},
        )
    )

    assert len(requests) == 1
    sent_filter = requests[0].url.params["$filter"]
    assert sent_filter == "(status eq 'A') and (externalCode in 'A','B','O''Brien')"


def test_extract_by_filter_in_chunks_by_count(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"d": {"results": []}})

    client = client_for(handler, monkeypatch)
    values = [f"v{i}" for i in range(5)]
    result = asyncio.run(
        client.extract_by_filter_in("FOCompany", "externalCode", values, chunk_size=2)
    )

    assert len(requests) == 3
    assert result["chunks_processed"] == 3
    assert [d["chunk_size"] for d in result["chunk_diagnostics"]] == [2, 2, 1]


def test_extract_by_filter_in_chunks_by_encoded_url_length(monkeypatch):
    # None of these values are anywhere near the 1000-count cap, but a long
    # enough value list must still split before the encoded $filter gets too
    # large for a GET URL.
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"d": {"results": []}})

    client = client_for(handler, monkeypatch)
    values = [f"value-{i:04d}" for i in range(500)]  # count well under 1000
    result = asyncio.run(client.extract_by_filter_in("FOCompany", "externalCode", values))

    assert len(requests) > 1, "a long enough value list must be split on length, not just count"
    for request in requests:
        assert _encoded_len(request.url.params["$filter"]) <= _MAX_FILTER_ENCODED_LEN
    assert result["total_records"] == 0
    assert sum(d["chunk_size"] for d in result["chunk_diagnostics"]) == len(values)
