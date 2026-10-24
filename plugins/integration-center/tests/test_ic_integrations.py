"""ic_integrations against a mocked SF: the allowlist holds on every tier,
config comes back as plaintext, and nothing but one IC GET is sent.
All records are synthetic."""

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import sf_integration_center as ic
from successfactors_toolkit import mcp_server
from successfactors_toolkit.config import get_settings
from successfactors_toolkit.services.odata_client import ODataClient
from tests.systems import write_system

# Every credential-like or non-allowlisted value carries this marker.
SECRET = "SECRETX"
_DAY_MS = 86_400_000


def _ms(days_ago: float) -> int:
    return int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp() * 1000)


def _sftp(days_ago=2):
    return {
        "__metadata": {"type": "SFOData.IntegrationProcessDefinition", "uri": f"{SECRET}-uri"},
        "externalCode": "IC1",
        "externalName": "Synthetic SFTP export",
        "isActive": True,
        "createdBy": f"{SECRET}-person",
        "futureField": f"{SECRET}-sap-added-later",
        "keypairs": {"results": [{"privateKey": f"{SECRET}-private", "passphrase": SECRET}]},
        "latestVersionNav": {
            "triggerType": "Scheduled",
            "lastAttemptedRunTime": f"/Date({_ms(days_ago)}+0000)/",
            "lastSuccessfulRunTime": f"/Date({_ms(days_ago) - _DAY_MS}+0000)/",
            "nextDeltaToken": f"{SECRET}-delta",
            "lastModifiedBy": f"{SECRET}-person",
            "destinationOperation": {
                "results": [
                    {
                        "type": "SFTP",
                        "method": None,
                        "directory": "/outbound",
                        "fileName": "export_{date}.csv",
                        "encryptionKey": f"{SECRET}-pgp",
                        "signingVerifyingKey": f"{SECRET}-sign",
                        "oAuthCustomHeaderParameters": {"results": [{"value": SECRET}]},
                        "connection": {
                            "type": "SFTP",
                            "uri": "sftp.example.invalid",
                            "port": 22,
                            "directory": "/upload",
                            "externalName": "Example SFTP",
                            "dependencyName": None,
                            "token": f"{SECRET}-token",
                            "username": f"{SECRET}-user",
                            "authenticationKey": f"{SECRET}-key",
                            "tokenIdentifier": f"{SECRET}-tokenid",
                        },
                    }
                ]
            },
        },
    }


def _rest(days_ago=30):
    # destinationOperation as a single object, the other shape SF returns.
    return {
        "externalCode": "IC2",
        "externalName": "Synthetic REST push",
        "isActive": False,
        "latestVersionNav": {
            "triggerType": "Manual",
            "lastAttemptedRunTime": f"/Date({_ms(days_ago)})/",
            "lastSuccessfulRunTime": None,
            "destinationOperation": {
                "type": "REST",
                "method": "POST",
                "connection": {
                    "type": "REST",
                    "uri": f"https://{SECRET}u:{SECRET}p@rest.example.invalid:8443/api/v1/push"
                    f"?apikey={SECRET}#{SECRET}",
                    "port": 22,
                    "externalName": "Example middleware",
                    "username": f"{SECRET}-user",
                },
            },
        },
    }


def _never_run():
    # A shape change: uri is no longer a string; nothing in it may pass.
    return {
        "externalCode": "IC3",
        "externalName": "Synthetic never run",
        "isActive": True,
        "latestVersionNav": {
            "lastAttemptedRunTime": None,
            "destinationOperation": [{"connection": {"uri": {"secret": SECRET}, "port": SECRET}}],
        },
    }


@pytest.fixture
def sf(monkeypatch, tmp_path):
    """Install a mock SF answering `responses` (one JSON body or (status, text)
    per request) on system example-a with `system_fields`; returns the sent requests."""
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()
    sent: list[httpx.Request] = []

    def install(response, **system_fields):
        write_system(get_settings().systems_dir, "example-a", **system_fields)

        def handler(request):
            sent.append(request)
            if isinstance(response, tuple):
                return httpx.Response(response[0], text=response[1])
            return httpx.Response(200, json={"d": {"results": response}})

        client = ODataClient(
            get_settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        monkeypatch.setattr(client, "_get_token", AsyncMock(return_value="synthetic-token"))
        monkeypatch.setattr(mcp_server, "_clients", lambda: (client, None))
        return sent

    return install


def _run(**kwargs):
    result = asyncio.run(ic.ic_integrations(system="example-a", **kwargs))
    saved = Path(result["file"]).read_text(encoding="utf-8") if "file" in result else ""
    return result, saved


def test_production_returns_config_as_plaintext_from_one_ic_get(sf):
    sent = sf([_sftp(), _rest()], production=True)
    result, saved = _run(preview=2)

    sftp, rest = result["preview"]
    assert json.loads(saved) == result["preview"]
    assert sftp["name"] == "Synthetic SFTP export" and sftp["active"] is True
    assert sftp["trigger_type"] == "Scheduled"
    assert datetime.fromisoformat(sftp["last_attempted_run"]) > datetime.now(UTC) - timedelta(3)
    assert sftp["last_successful_run"] < sftp["last_attempted_run"]
    assert sftp["downstream"] == [
        {
            "operation_type": "SFTP",
            "method": None,
            "operation_directory": "/outbound",
            "file_name": "export_{date}.csv",
            "connection_type": "SFTP",
            "connection_name": "Example SFTP",
            "dependency_name": None,
            "endpoint": "sftp.example.invalid",
            "port": 22,
            "connection_directory": "/upload",
        }
    ]
    assert rest["downstream"][0]["method"] == "POST"
    assert rest["downstream"][0]["endpoint"] == "https://rest.example.invalid:8443/api/v1/push"
    assert rest["last_successful_run"] is None

    (request,) = sent
    assert request.method == "GET"
    assert request.url.path == f"/odata/v2/{ic.SERVICE}/IntegrationProcessDefinition"
    params = dict(request.url.params)
    assert params["$filter"] == "definitionType eq 'SAVEDCONTENT'"
    assert params["$expand"] == ic.EXPAND


def test_only_allowlisted_properties_are_requested():
    requested = {path.rsplit("/", 1)[-1] for path in ic.SELECT.split(",")}
    assert requested == {
        "externalCode",
        "externalName",
        "isActive",
        "triggerType",
        "lastAttemptedRunTime",
        "lastSuccessfulRunTime",
        "type",
        "method",
        "directory",
        "fileName",
        "uri",
        "port",
        "dependencyName",
    }
    assert ic.EXPAND.split(",") == [
        "latestVersionNav",
        "latestVersionNav/destinationOperation",
        "latestVersionNav/destinationOperation/connection",
    ]


@pytest.mark.parametrize(
    "system_fields",
    [{"pii_filter_tier": 0}, {"pii_filter_tier": 1}, {"pii_filter_tier": 2}, {"production": True}],
    ids=["tier0", "tier1", "tier2", "tier3"],
)
def test_credentials_and_unlisted_fields_never_come_back(sf, system_fields):
    sf([_sftp(), _rest(), _never_run()], **system_fields)
    result, saved = _run(preview=3)
    assert len(result["preview"]) == 3
    assert SECRET not in json.dumps(result) and SECRET not in saved
    never = result["preview"][2]["downstream"][0]
    assert never["endpoint"] is None and never["port"] is None


@pytest.mark.parametrize("status", [400, 403, 500])
def test_an_error_returns_the_status_only(sf, status):
    sf((status, f'{{"error": {{"message": "token={SECRET} uri=https://x.invalid"}}}}'))
    result, saved = _run()
    assert result == {"error": "http_error", "status_code": status}
    assert not saved


def test_ran_within_days_counts_the_last_attempt_whether_or_not_it_succeeded(sf):
    failed_recently = _sftp(days_ago=2)
    failed_recently["latestVersionNav"]["lastSuccessfulRunTime"] = f"/Date({_ms(40)})/"
    sf([failed_recently, _rest(days_ago=30), _never_run()], production=True)

    week, _ = _run(ran_within_days=7, preview=5)
    assert [r["id"] for r in week["preview"]] == ["IC1"]
    quarter, _ = _run(ran_within_days=90, preview=5)
    assert [r["id"] for r in quarter["preview"]] == ["IC1", "IC2"]
    everything, _ = _run(preview=5)
    assert everything["total_integrations"] == 3


def test_ran_within_includes_the_cutoff_boundary():
    now = datetime(2026, 1, 31, tzinfo=UTC)
    rows = [
        {"id": "edge", "last_attempted_run": "2026-01-24T00:00:00+00:00"},
        {"id": "old", "last_attempted_run": "2026-01-23T23:59:59+00:00"},
        {"id": "none", "last_attempted_run": None},
    ]
    assert [r["id"] for r in ic.ran_within(rows, 7, now)] == ["edge"]


@pytest.mark.parametrize(
    "uri, endpoint",
    [
        ("https://u:p@h.invalid/a?k=v#f", "https://h.invalid/a"),
        ("sftp.example.invalid", "sftp.example.invalid"),
        ("user@sftp.example.invalid:2222/in", "sftp.example.invalid:2222/in"),
        ("https://h.invalid:notaport/x", None),
        (None, None),
        # A password holding "/", "?", "#", "@" or "://" hides the userinfo from a URL parser.
        ("https://admin:1234/abc@host.invalid/path", "https://host.invalid/path"),
        ("admin:9876?x@host.invalid", "host.invalid"),
        ("https://admin:1234#x@h.invalid", "https://h.invalid"),
        ("https://admin/S3cret@host.invalid/p", "https://host.invalid/p"),
        ("https://a:p@ss://w@host.invalid/p?u=http://x", "https://host.invalid/p"),
        ("x@y://host.invalid", None),
        ("https://", None),
        ("https://[::1]:8443/p", "https://[::1]:8443/p"),
    ],
)
def test_endpoint_drops_userinfo_query_and_fragment(uri, endpoint):
    assert ic._endpoint(uri) == endpoint


def test_the_tool_takes_no_path_query_or_expand_from_the_caller():
    assert list(inspect.signature(ic.ic_integrations).parameters) == [
        "system",
        "ran_within_days",
        "preview",
    ]


def test_port_takes_int64_as_a_number_or_a_digit_string_only():
    assert [ic._port({"port": v}) for v in (22, "22", "2 2", "²", True, 2.0)] == [
        22,
        22,
        None,
        None,
        None,
        None,
    ]


@pytest.mark.parametrize(
    "value", ["/Date(253402300800000)/", "/Date(" + "9" * 22 + ")/", "x", None]
)
def test_time_is_none_for_anything_but_an_in_range_date(value):
    assert ic._time(value) is None


def test_unexpanded_or_empty_navigations_give_empty_fields():
    deferred = {"externalCode": "IC4", "latestVersionNav": {"__deferred": {"uri": SECRET}}}
    row = ic.integration_row(deferred)
    assert row["last_attempted_run"] is None and row["downstream"] == []
    no_connection = {"latestVersionNav": {"destinationOperation": {"connection": {"results": []}}}}
    (downstream,) = ic.integration_row(no_connection)["downstream"]
    assert downstream["endpoint"] is None and SECRET not in json.dumps(row)
