"""OData v4: service-root URLs, value/@odata.nextLink paging, CSDL metadata,
and PII tokenization of full-metadata records. No tenant is contacted."""

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from successfactors_toolkit import mcp_server
from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.services.odata_client import ODataClient, v4_service_root
from successfactors_toolkit.services.pii_filter import PiiFilter, Vault
from tests.systems import write_system

_ROOT = "talent/cdp/Learning.svc/v1"
_BASE = f"https://api.example.invalid/odatav4/{_ROOT}"
_SSN = "123-45-6789"
_TOKEN = re.compile(r"\[PII-T(\d)-[0-9a-f]{16}\]")


def _v4_client(handler, monkeypatch):
    settings = Settings(_env_file=None)
    write_system(settings.systems_dir, "example-a", odata_version="v4")
    client = ODataClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(client, "_get_token", AsyncMock(return_value="synthetic-token"))
    return client


def _page(records, next_link=None):
    body = {"@odata.context": f"{_BASE}/$metadata#Items", "value": records}
    if next_link:
        body["@odata.nextLink"] = next_link
    return httpx.Response(200, json=body)


def test_v4_request_asks_for_full_metadata_json_without_format(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return _page([])

    client = _v4_client(handler, monkeypatch)
    asyncio.run(client.request("GET", f"{_ROOT}/Items", params={"$top": 1}))
    asyncio.run(client.request("GET", f"{_ROOT}/$metadata"))

    data, metadata = requests
    assert "$format" not in data.url.params
    assert data.headers["Accept"] == "application/json;odata.metadata=full"
    assert data.headers["OData-MaxVersion"] == "4.0"
    assert "$format" not in metadata.url.params
    assert metadata.headers["Accept"] == "application/xml"


def test_v4_extract_follows_skiptoken_links_but_never_their_host(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        token = request.url.params.get("$skiptoken")
        if token is None:
            # A link naming another host: only its $skiptoken may be used.
            return _page(
                [{"itemId": "1"}],
                "https://evil.invalid/elsewhere/Items?$skiptoken=abc&$filter=x",
            )
        return _page([{"itemId": "2"}], "Items?$skiptoken=def" if token == "abc" else None)

    client = _v4_client(handler, monkeypatch)
    result = asyncio.run(
        client.extract_all(f"{_ROOT}/Items?$filter=active eq true", params={"$skip": 5})
    )

    assert [r["itemId"] for r in result["results"]] == ["1", "2", "2"]
    assert result["stopped_reason"] == "exhausted"
    assert result["pages_fetched"] == 3
    assert all(str(r.url).startswith(f"{_BASE}/Items?") for r in requests)
    second = requests[1].url.params
    assert second["$skiptoken"] == "abc"
    assert second["$filter"] == "active eq true", "our own options, not the link's"
    assert "$skip" not in second, "the skiptoken carries the position now"


def test_v4_extract_follows_skip_links_and_reports_the_cap(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        skip = int(request.url.params.get("$skip", 0))
        return _page(
            [{"itemId": str(skip)}, {"itemId": str(skip + 1)}],
            f"{_BASE}/Items?$top=2&$skip={skip + 2}",
        )

    client = _v4_client(handler, monkeypatch)
    result = asyncio.run(client.extract_all(f"{_ROOT}/Items", max_pages=2))

    assert result["total_records"] == 4
    assert result["stopped_reason"] == "max_pages"
    assert result["next_skiptoken"] is None, "a $skip link has no skiptoken to resume with"
    assert requests[1].url.params["$skip"] == "2"
    assert requests[1].url.params["$top"] == "2"


def test_v4_extract_single_entity_and_cap_on_a_skiptoken(monkeypatch):
    client = _v4_client(
        lambda request: _page([{"itemId": "1"}], f"{_BASE}/Items?$skiptoken=next"), monkeypatch
    )
    result = asyncio.run(client.extract_all(f"{_ROOT}/Items", max_pages=1))
    assert result["stopped_reason"] == "max_pages"
    assert result["next_skiptoken"] == "next"


def test_v4_extract_by_filter_in_uses_the_parenthesised_in(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return _page([])

    client = _v4_client(handler, monkeypatch)
    asyncio.run(client.extract_by_filter_in(f"{_ROOT}/Items", "itemId", ["a", "b'c"]))
    assert requests[0].url.params["$filter"] == "itemId in ('a','b''c')"


# --- PII -------------------------------------------------------------------


def _filter(tmp_path, tier=1):
    return PiiFilter(tier, {}, Vault(tmp_path / "vault", "example-a"))


def _v4_national_id(**extra):
    key = f"PerNationalId(cardType='SSN',country='USA',personIdExternal='p1',nationalId='{_SSN}')"
    return {
        "@odata.type": "#SFOData.PerNationalId",
        "@odata.id": key,
        "@odata.editLink": key,
        "@odata.etag": 'W/"1"',
        "personIdExternal": "p1",
        "country": "USA",
        "nationalId": _SSN,
        "personNav@odata.navigationLink": f"{key}/personNav",
        "personNav@odata.associationLink": f"{key}/personNav/$ref",
        **extra,
    }


def test_v4_record_gets_the_same_tokens_as_its_v2_equivalent(tmp_path):
    pii = _filter(tmp_path)
    v2 = {"__metadata": {"type": "SFOData.PerNationalId"}, "nationalId": _SSN, "country": "USA"}
    [v2_out], v2_count = pii.tokenize_records([v2])
    [v4_out], v4_count = pii.tokenize_records([_v4_national_id()], v4=True)

    assert v2_count == v4_count == 1
    assert v4_out["nationalId"] == v2_out["nationalId"]
    assert v4_out["personIdExternal"] == "p1" and v4_out["@odata.type"]
    assert _SSN not in json.dumps(v4_out)
    assert not any("Link" in key or key.endswith("@odata.id") for key in v4_out)


def test_v4_expanded_collections_and_their_links_are_covered(tmp_path):
    key = "EmpWorkPermit(documentNumber='A1234567',userId='u1')"
    record = {
        "@odata.type": "#SFOData.PerPerson",
        "@odata.context": "$metadata#PerPerson('p1')",
        "personIdExternal": "p1",
        "nationalIdNav": [_v4_national_id()],
        "nationalIdNav@odata.nextLink": f"PerPerson('p1')/nationalIdNav?$skiptoken={_SSN}",
        "nationalIdNav@odata.count": 2,
        "workPermitNav": {
            "@odata.type": "#SFOData.EmpWorkPermit",
            "@odata.readLink": key,
            "@odata.mediaReadLink": f"{key}/$value",
            "@odata.mediaEditLink": f"{key}/$value",
            "attachment@odata.mediaReadLink": f"{key}/attachment",
            "documentNumber": "A1234567",
        },
        "photoNav": {"@id": "Photo(userId='u1')", "@type": "#SFOData.Photo"},
    }
    [out], count = _filter(tmp_path).tokenize_records([record], v4=True)
    text = json.dumps(out)
    assert _SSN not in text and "A1234567" not in text
    assert count == 2
    assert _TOKEN.fullmatch(out["workPermitNav"]["documentNumber"])
    assert set(out["workPermitNav"]) == {"@odata.type", "documentNumber"}
    assert out["nationalIdNav@odata.count"] == 2
    assert "@odata.context" not in out and "nationalIdNav@odata.nextLink" not in out
    # 4.01's bare annotation names count too.
    assert out["photoNav"] == {"@type": "#SFOData.Photo"}


def test_untyped_v4_records_get_every_entitys_rules(tmp_path):
    # Without @odata.type (e.g. a caller's own $format=json) the entity is
    # unknown: every entity's rules apply at their most sensitive tier.
    untyped = {"userId": "u1", "documentNumber": "A1234567", "city": "Home Town"}
    [v4], v4_count = _filter(tmp_path, tier=2).tokenize_records([untyped], v4=True)
    assert v4_count == 2
    assert _TOKEN.fullmatch(v4["documentNumber"]).group(1) == "1"
    assert _TOKEN.fullmatch(v4["city"]).group(1) == "2"
    assert v4["userId"] == "u1"
    # v2 is unchanged: an untyped record gets the "*" rules only.
    [v2], v2_count = _filter(tmp_path, tier=2).tokenize_records([untyped])
    assert v2_count == 0 and v2 == untyped


def test_typed_v4_records_keep_entity_scoping(tmp_path):
    record = {"@odata.type": "#SFOData.PerEmail", "city": "Plant City"}
    [out], count = _filter(tmp_path, tier=2).tokenize_records([record], v4=True)
    assert count == 0 and out["city"] == "Plant City"


def test_unknown_v4_types_get_every_entitys_rules(tmp_path):
    # v4 services may use their own type names; one the map doesn't know is
    # treated like an untyped record, never as "*"-only.
    record = {"@odata.type": "#com.sap.sf.ec.WorkPermit", "documentNumber": "A1234567"}
    [out], count = _filter(tmp_path, tier=1).tokenize_records([record], v4=True)
    assert count == 1 and _TOKEN.fullmatch(out["documentNumber"])


@pytest.mark.parametrize(
    "body",
    [
        {"@odata.context": "$metadata#PerNationalId", "value": [{"nationalId": _SSN}]},
        {"@odata.context": "$metadata#PerNationalId/$entity", "nationalId": _SSN},
    ],
)
def test_error_path_tokenizes_v4_collections_and_single_entities(tmp_path, body):
    text = mcp_server._pii_tokenize_json_records(json.dumps(body), _filter(tmp_path))
    assert text is not None and _SSN not in text and "@odata.context" not in text
    assert _TOKEN.search(text)


# --- MCP tools on a v4 system ------------------------------------------------

_CSDL = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">
  <edmx:DataServices>
    <Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="com.sap.sf.learning">
      <EntityType Name="Item">
        <Key><PropertyRef Name="itemId"/><PropertyRef Name="itemType"/></Key>
        <Property Name="itemId" Type="Edm.String" Nullable="false" MaxLength="90"/>
        <Property Name="itemType" Type="Edm.String" Nullable="false"/>
        <Property Name="title" Type="Edm.String"/>
        <NavigationProperty Name="owner" Type="com.sap.sf.learning.Owner"/>
        <NavigationProperty Name="sessions" Type="Collection(com.sap.sf.learning.Session)"
                            Partner="item"/>
      </EntityType>
      <EntityType Name="Session">
        <Key><PropertyRef Name="sessionId"/></Key>
        <Property Name="sessionId" Type="Edm.Int64" Nullable="false"/>
      </EntityType>
      <EntityType Name="Owner">
        <Key><PropertyRef Name="userId"/></Key>
        <Property Name="userId" Type="Edm.String" Nullable="false"/>
      </EntityType>
      <EntityContainer Name="EntityContainer">
        <EntitySet Name="Items" EntityType="com.sap.sf.learning.Item"/>
        <EntitySet Name="Sessions" EntityType="com.sap.sf.learning.Session"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""


class _V4OData:
    def __init__(self, results=None, csdl=_CSDL):
        self.results = results or []
        self.csdl = csdl
        self.paths: list[str] = []
        self.extract_params: list[dict] = []

    async def request(self, method, path, conn=None, params=None, body=None, extra_headers=None):
        self.paths.append(path)
        return {"status_code": 200, "headers": {}, "body": self.csdl}

    async def extract_all(self, path, conn=None, params=None, max_pages=10):
        self.extract_params.append(dict(params or {}))
        return {
            "stopped_reason": "exhausted",
            "total_records": len(self.results),
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": self.results,
            "last_page_size": len(self.results),
        }


@pytest.fixture
def v4_system(monkeypatch, tmp_path):
    mcp_server._key_cache.clear()
    mcp_server._nav_cache.clear()
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "vault"))
    get_settings.cache_clear()
    write_system(get_settings().systems_dir, "example-a", odata_version="v4")

    def install(odata):
        monkeypatch.setattr(mcp_server, "_clients", lambda: (odata, None))
        return odata

    yield install
    mcp_server._key_cache.clear()
    mcp_server._nav_cache.clear()


def test_parse_edmx_navs_reads_v4_navigation_types():
    navs = mcp_server._parse_edmx_navs(_CSDL)
    assert navs["Item"] == [
        {"name": "owner", "target": "Owner", "filterable": "true"},
        {"name": "sessions", "target": "Session", "filterable": "true"},
    ]
    assert mcp_server._parse_edmx_keys(_CSDL)["Item"] == ["itemId", "itemType"]


def test_odata_metadata_reads_the_v4_service_document_for_an_entity_set(v4_system):
    odata = v4_system(_V4OData())
    result = asyncio.run(mcp_server.odata_metadata(entity=f"{_ROOT}/Items"))

    assert odata.paths == [f"{_ROOT}/$metadata"] * 2
    assert list(result["fields"]) == ["Item"]
    assert result["fields"]["Item"]["itemId"] == {
        "Type": "Edm.String",
        "Nullable": "false",
        "MaxLength": "90",
    }
    assert [n["target"] for n in result["navigation"]] == ["Owner", "Session"]

    # Another set of the same service reuses the cached navigation lookup.
    asyncio.run(mcp_server.odata_metadata(entity=f"{_ROOT}/Sessions"))
    assert odata.paths.count(f"{_ROOT}/$metadata") == 3


def test_odata_metadata_v4_service_root_is_the_whole_service(v4_system):
    v4_system(_V4OData())
    result = asyncio.run(mcp_server.odata_metadata(entity=_ROOT))
    assert sorted(result["fields"]) == ["Item", "Owner", "Session"]


def test_compare_metadata_on_v4_services(v4_system):
    class _Drifted(_V4OData):
        async def request(self, method, path, conn=None, **kwargs):
            body = self.csdl
            if conn is not None and conn.system == "drifted":
                body = body.replace(' MaxLength="90"', ' MaxLength="128"')
            return {"status_code": 200, "headers": {}, "body": body}

    v4_system(_Drifted())
    write_system(get_settings().systems_dir, "drifted", odata_version="v4")
    result = asyncio.run(mcp_server.compare_metadata("example-a", "drifted", f"{_ROOT}/Items"))
    assert result["in_sync"] is False
    assert result["differences"]["Item"]["changed"] == {"itemId": {"MaxLength": ["90", "128"]}}


def test_odata_query_on_v4_orders_by_key_without_snapshot_paging(v4_system, monkeypatch):
    odata = v4_system(_V4OData([{"itemId": "1", "itemType": "A"}]))
    # Keys of an unknown entity are tokenized otherwise.
    write_system(get_settings().systems_dir, "example-a", odata_version="v4", pii_filter_tier=0)
    result = asyncio.run(mcp_server.odata_query(f"{_ROOT}/Items?$select=itemId,itemType"))

    assert odata.paths == [f"{_ROOT}/$metadata"]
    assert result["orderby_added"] == "itemId,itemType"
    assert "paging" not in odata.extract_params[0]
    assert "paging_added" not in result


def test_odata_query_on_v4_tokenizes_full_metadata_records(v4_system, monkeypatch):
    write_system(get_settings().systems_dir, "example-a", odata_version="v4", pii_filter_tier=1)
    untyped = {"personIdExternal": "p2", "nationalId": "A1234567"}
    v4_system(_V4OData([_v4_national_id(), untyped]))
    result = asyncio.run(mcp_server.odata_query(f"{_ROOT}/PerNationalId", max_pages=1))

    saved = Path(result["file"]).read_text(encoding="utf-8")
    assert _SSN not in saved and "A1234567" not in saved
    assert result["pii_tokenized"] == 2


@pytest.mark.parametrize(
    ("path", "root", "rest"),
    [
        (
            "talent/calibration/CalSession.svc/v1/CalibrationSession",
            "talent/calibration/CalSession.svc/v1",
            "CalibrationSession",
        ),
        ("onboarding/AdditionalServices.svc/v1/", "onboarding/AdditionalServices.svc/v1", ""),
        (
            "talent/continuousfeedback/v1/feedback('1')",
            "talent/continuousfeedback/v1",
            "feedback('1')",
        ),
        ("talent/continuousfeedback/v1/$metadata", "talent/continuousfeedback/v1", "$metadata"),
    ],
)
def test_v4_service_root_with_or_without_svc(path, root, rest):
    # Service roots as SF publishes them on the API Hub (2026-09-28).
    assert v4_service_root(path) == (root, rest)


def test_v4_feedback_display_names_are_tokenized(tmp_path):
    record = {
        "@odata.type": "#continuousfeedback.feedback",
        "senderDisplayName": "Jane Doe",
        "subjectDisplayName": "John Roe",
        "topic": "Q3 demo",
    }
    [out], count = _filter(tmp_path, tier=3).tokenize_records([record], v4=True)
    assert count == 2 and out["topic"] == "Q3 demo"
    assert _TOKEN.fullmatch(out["senderDisplayName"]).group(1) == "3"
