"""MCP tool tests — no tenant is contacted; both clients are faked.

Two things are worth a check here: that Compound Employee payloads land on
disk and never travel back in the tool result (HR data), and that the EDMX
summary the model diffs across instances actually matches the metadata.
"""

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from successfactors_toolkit import mcp_server
from successfactors_toolkit.config import get_settings


@pytest.fixture(autouse=True)
def _clear_entity_key_cache():
    # _entity_key_properties and _nav_properties cache per process (the
    # latter per company_id); tests reuse the same company_id/entity names
    # against different fakes, so a stale cache entry would leak into the
    # next test.
    mcp_server._key_cache.clear()
    mcp_server._nav_cache.clear()
    yield


_EDMX = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">
  <edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm"
            xmlns:sap="http://www.sap.com/Protocols/SAPData" Namespace="SFOData">
      <EntityType Name="EmpJob">
        <Property Name="userId" Type="Edm.String" Nullable="false" sap:label="User"/>
        <Property Name="jobCode" Type="Edm.String" MaxLength="128" sap:required="true"/>
        <NavigationProperty Name="userNav" Relationship="SFOData.userNav_FK"
                             FromRole="FromRole_userNav" ToRole="ToRole_userNav"
                             sap:filterable="true"/>
      </EntityType>
      <EntityType Name="FOCompany">
        <Property Name="externalCode" Type="Edm.String" Nullable="false"/>
      </EntityType>
      <Association Name="userNav_FK">
        <End Type="SFOData.User" Multiplicity="0..1" Role="ToRole_userNav"/>
        <End Type="SFOData.EmpJob" Multiplicity="*" Role="FromRole_userNav"/>
      </Association>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""

# The marker stands in for employee data: it must reach the file and nothing else.
_MARKER = "PERSON-SECRET-4711"


def _ce_page(has_more: str, session: str) -> str:
    return (
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<queryResponse><numResults>2</numResults><hasMore>{has_more}</hasMore>"
        f"<querySessionId>{session}</querySessionId>"
        f"<person><person_id_external>{_MARKER}</person_id_external></person>"
        "</queryResponse></SOAP-ENV:Envelope>"
    )


class _FakeSFAPI:
    def __init__(self):
        self.queries: list[str] = []
        self.sessions: list[str] = []

    async def query(self, query_string, conn=None, params=None):
        self.queries.append(query_string)
        return {"status_code": 200, "headers": {}, "body": _ce_page("true", "SESSION-1")}

    async def query_more(self, query_session_id, conn=None):
        self.sessions.append(query_session_id)
        return {"status_code": 200, "headers": {}, "body": _ce_page("false", "")}


# Second instance: userId lost its label, jobCode became optional, a custom
# field exists only here, and FOCompany dropped a field.
_EDMX_DRIFTED = (
    _EDMX.replace(' sap:label="User"', "")
    .replace('sap:required="true"', 'sap:required="false"')
    .replace(
        '<Property Name="jobCode"',
        '<Property Name="customString42" Type="Edm.String"/>\n        <Property Name="jobCode"',
    )
    .replace('<Property Name="externalCode" Type="Edm.String" Nullable="false"/>', "")
)


class _FakeOData:
    """Serves _EDMX for every instance except "drifted"."""

    async def request(self, method, path, conn=None, params=None, body=None, extra_headers=None):
        drifted = conn is not None and conn.company_id == "drifted"
        return {"status_code": 200, "headers": {}, "body": _EDMX_DRIFTED if drifted else _EDMX}


def _install(monkeypatch, tmp_path):
    """Fake both clients and point RESULTS_DIR at the test's own directory."""
    odata, sfapi = _FakeOData(), _FakeSFAPI()
    monkeypatch.setattr(mcp_server, "_clients", lambda: (odata, sfapi))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()
    return odata, sfapi


def test_ce_query_writes_payload_to_disk_and_keeps_it_out_of_the_result(monkeypatch, tmp_path):
    _, sfapi = _install(monkeypatch, tmp_path)

    result = asyncio.run(mcp_server.ce_query(company_id="example-a", person_id_external="4711"))

    assert result["page_count"] == 2, "hasMore=true must trigger one queryMore"
    assert sfapi.sessions == ["SESSION-1"]
    assert result["total_records"] == 4
    assert result["truncated"] is False
    assert _MARKER not in json.dumps(result), "employee data must not ride back in the tool result"
    assert [p.name for p in sorted((tmp_path / "results" / "mcp").iterdir())]
    assert all(_MARKER in open(f, encoding="utf-8").read() for f in result["files"])
    # HR data on disk is owner-only.
    assert all(stat.S_IMODE(Path(f).stat().st_mode) == 0o600 for f in result["files"])


def test_ce_query_surfaces_soap_faults_without_writing_a_file(monkeypatch, tmp_path):
    _, sfapi = _install(monkeypatch, tmp_path)
    fault = (
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
        "<SOAP-ENV:Body><SOAP-ENV:Fault><faultstring>INVALID_SFQL: deduction_recurring"
        "</faultstring></SOAP-ENV:Fault></SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )

    async def _faulting(query_string, conn=None, params=None):
        return {"status_code": 500, "headers": {}, "body": fault}

    monkeypatch.setattr(sfapi, "query", _faulting)
    result = asyncio.run(mcp_server.ce_query(company_id="example-a"))

    assert result["error"] == "soap_fault"
    assert "INVALID_SFQL" in result["body"], "the model needs to see which segment failed"
    assert not (tmp_path / "results").exists()


def test_odata_metadata_summarises_edmx_fields_and_annotations(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    result = asyncio.run(mcp_server.odata_metadata(company_id="example-a", entity="EmpJob"))

    assert result["entity_count"] == 2
    assert result["field_count"] == 3
    fields = result["fields"]  # small enough to be inlined
    assert sorted(fields["EmpJob"]) == ["jobCode", "userId"]
    assert fields["EmpJob"]["userId"]["label"] == "User", "sap: annotations carry the config"
    assert fields["EmpJob"]["jobCode"]["required"] == "true"
    # Navigation properties: entity-scoped $metadata doesn't carry them, so
    # they're resolved from a full-service fetch and merged in.
    assert result["navigation"] == [{"name": "userNav", "target": "User", "filterable": "true"}]
    assert "navigation_warning" not in result
    on_disk = json.loads(open(result["file"], encoding="utf-8").read())
    assert on_disk == {"fields": fields, "navigation": result["navigation"]}


def test_odata_metadata_whole_service_pull_skips_navigation(monkeypatch, tmp_path):
    # entity="" already returns everything the full EDMX has; resolving navs
    # per-entity on top of that isn't attempted (and isn't needed).
    _install(monkeypatch, tmp_path)

    result = asyncio.run(mcp_server.odata_metadata(company_id="example-a", entity=""))

    assert "navigation" not in result


def test_odata_metadata_caches_the_full_metadata_fetch_per_company(monkeypatch, tmp_path):
    odata, _ = _install(monkeypatch, tmp_path)
    paths: list[str] = []
    original_request = odata.request

    async def _counting(method, path, **kwargs):
        paths.append(path)
        return await original_request(method, path, **kwargs)

    monkeypatch.setattr(odata, "request", _counting)

    asyncio.run(mcp_server.odata_metadata(company_id="example-a", entity="EmpJob"))
    asyncio.run(mcp_server.odata_metadata(company_id="example-a", entity="FOCompany"))

    # One entity-scoped fetch per call, but the full-service $metadata that
    # navigation properties are resolved from is fetched once and reused.
    assert paths.count("EmpJob/$metadata") == 1
    assert paths.count("FOCompany/$metadata") == 1
    assert paths.count("$metadata") == 1


class _NavFailingOData:
    """Entity-scoped $metadata works; the full-service fetch navigation
    properties need fails."""

    async def request(self, method, path, conn=None, params=None, body=None, extra_headers=None):
        if path == "$metadata":
            return {"status_code": 500, "headers": {}, "body": "boom"}
        return {"status_code": 200, "headers": {}, "body": _EDMX}


def test_odata_metadata_navigation_failure_does_not_break_field_output(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_clients", lambda: (_NavFailingOData(), _FakeSFAPI()))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()

    result = asyncio.run(mcp_server.odata_metadata(company_id="example-a", entity="EmpJob"))

    assert result["entity_count"] == 2
    assert result["fields"]["EmpJob"]["userId"]["label"] == "User", "field output is unaffected"
    assert result["navigation"] == []
    assert "navigation_warning" in result


def test_parse_edmx_navs_resolves_target_via_association_and_falls_back_to_role():
    xml = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">
  <edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm"
            xmlns:sap="http://www.sap.com/Protocols/SAPData" Namespace="SFOData">
      <EntityType Name="EmpJob">
        <Property Name="userId" Type="Edm.String"/>
        <NavigationProperty Name="userNav" Relationship="SFOData.userNav_FK"
                             FromRole="FromRole_userNav" ToRole="ToRole_userNav"/>
        <NavigationProperty Name="unresolvedNav" Relationship="SFOData.missing_FK"
                             FromRole="FromRole_x" ToRole="ToRole_x" sap:filterable="false"/>
      </EntityType>
      <Association Name="userNav_FK">
        <End Type="SFOData.User" Multiplicity="0..1" Role="ToRole_userNav"/>
        <End Type="SFOData.EmpJob" Multiplicity="*" Role="FromRole_userNav"/>
      </Association>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""

    navs = mcp_server._parse_edmx_navs(xml)

    assert navs["EmpJob"] == [
        {"name": "userNav", "target": "User", "filterable": "true"},
        # Relationship doesn't match any Association: falls back to ToRole.
        {"name": "unresolvedNav", "target": "ToRole_x", "filterable": "false"},
    ]


def test_compare_metadata_reports_drift_between_two_instances(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    result = asyncio.run(mcp_server.compare_metadata(company_a="example-a", company_b="drifted"))

    assert result["in_sync"] is False
    emp_job = result["differences"]["EmpJob"]
    assert emp_job["fields_only_in_b"] == ["customString42"], "new field in the second instance"
    assert emp_job["fields_only_in_a"] == []
    # [value_in_a, value_in_b] — a dropped label and a relaxed requirement.
    assert emp_job["changed"]["userId"]["label"] == ["User", None]
    assert emp_job["changed"]["jobCode"]["required"] == ["true", "false"]
    # FOCompany lost its only field in b, so it has no properties left.
    assert result["differences"]["FOCompany"]["fields_only_in_a"] == ["externalCode"]
    assert result["summary"]["fields_changed"] == 2
    assert result["summary"]["entities_with_differences"] == 2


def test_compare_metadata_is_in_sync_when_both_instances_match(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    result = asyncio.run(mcp_server.compare_metadata(company_a="example-a", company_b="example-b"))

    assert result["in_sync"] is True
    assert result["differences"] == {}
    assert result["summary"]["entities_compared"] == 2


class _FailingOData:
    """extract_all reports the failure but drops the body; request() has the
    reason SF actually gave."""

    async def extract_all(self, path, conn=None, params=None, max_pages=10):
        return {
            "stopped_reason": "http_error",
            "last_status_code": 403,
            "total_records": 0,
            "results": [],
        }

    async def request(self, method, path, conn=None, params=None, body=None, extra_headers=None):
        return {"status_code": 403, "headers": {}, "body": "No permission for entity EmpJob"}


class _PreviewOData:
    def __init__(self, results=None):
        self.extract_calls = []
        self.results = results if results is not None else [{"id": index} for index in range(21)]

    async def extract_all(self, path, conn=None, params=None, max_pages=10):
        self.extract_calls.append((path, conn, params, max_pages))
        return {
            "stopped_reason": "exhausted",
            "total_records": len(self.results),
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": self.results,
        }


@pytest.mark.parametrize(("preview", "expected_count"), [(0, 0), (20, 20)])
def test_odata_query_accepts_preview_boundaries(monkeypatch, tmp_path, preview, expected_count):
    odata = _PreviewOData()
    monkeypatch.setattr(mcp_server, "_clients", lambda: (odata, _FakeSFAPI()))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()

    result = asyncio.run(mcp_server.odata_query(path="EmpJob", preview=preview))

    assert len(odata.extract_calls) == 1
    assert result.get("preview", []) == [{"id": index} for index in range(expected_count)]


@pytest.mark.parametrize("preview", [-1, 21, 10**100])
def test_odata_query_rejects_out_of_range_preview_before_creating_clients(monkeypatch, preview):
    monkeypatch.setattr(
        mcp_server,
        "_clients",
        lambda: pytest.fail("out-of-range preview must not create clients or query SuccessFactors"),
    )

    with pytest.raises(ValueError, match="preview must be 0-20"):
        asyncio.run(mcp_server.odata_query(path="EmpJob", preview=preview))


def test_odata_query_inlines_preview_at_the_16_kib_boundary(monkeypatch, tmp_path):
    record = {"value": ""}
    record["value"] = "x" * (
        mcp_server._PREVIEW_INLINE_LIMIT
        - len(json.dumps([record], ensure_ascii=False, default=str).encode("utf-8"))
    )
    odata = _PreviewOData([record])
    monkeypatch.setattr(mcp_server, "_clients", lambda: (odata, _FakeSFAPI()))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()

    result = asyncio.run(mcp_server.odata_query(path="EmpJob", preview=1))

    assert result["preview"] == [record]
    assert "preview_error" not in result
    assert (
        len(json.dumps(result["preview"], ensure_ascii=False, default=str).encode("utf-8"))
        == mcp_server._PREVIEW_INLINE_LIMIT
    )


def test_odata_query_omits_oversized_expanded_preview_but_saves_it(monkeypatch, tmp_path):
    record = {"id": "1", "nav": {"results": [{"value": "x" * mcp_server._PREVIEW_INLINE_LIMIT}]}}
    odata = _PreviewOData([record])
    monkeypatch.setattr(mcp_server, "_clients", lambda: (odata, _FakeSFAPI()))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()

    result = asyncio.run(mcp_server.odata_query(path="EmpJob", preview=1))

    assert "preview" not in result
    assert result["preview_error"] == (
        "Requested preview exceeds the 16 KiB inline limit; inspect the saved file locally."
    )
    assert json.loads(Path(result["file"]).read_text()) == [record]


def test_odata_query_failure_surfaces_the_upstream_error_body(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_clients", lambda: (_FailingOData(), _FakeSFAPI()))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()

    result = asyncio.run(mcp_server.odata_query(path="EmpJob", company_id="example-a"))

    assert result["error"] == "http_error"
    assert result["status_code"] == 403
    assert "No permission" in result["body"], "the status code alone never says why"
    assert not (tmp_path / "results").exists()


# EmpJob keyed on (userId, startDate) — enough to exercise auto-$orderby.
_EDMX_WITH_KEY = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">
  <edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm"
            xmlns:sap="http://www.sap.com/Protocols/SAPData" Namespace="SFOData">
      <EntityType Name="EmpJob">
        <Key>
          <PropertyRef Name="userId"/>
          <PropertyRef Name="startDate"/>
        </Key>
        <Property Name="userId" Type="Edm.String"/>
        <Property Name="startDate" Type="Edm.DateTime"/>
        <Property Name="jobCode" Type="Edm.String"/>
      </EntityType>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""


class _KeyedOData:
    """Serves $metadata (with a Key, for auto-$orderby lookups) plus a canned
    extract_all result, so pagination-correctness logic can be tested without
    a real tenant."""

    def __init__(self, extract_result, metadata_xml=_EDMX_WITH_KEY, metadata_status=200):
        self.extract_result = extract_result
        self.metadata_xml = metadata_xml
        self.metadata_status = metadata_status
        self.extract_calls: list[tuple] = []
        self.metadata_calls: list[str] = []

    async def request(self, method, path, conn=None, params=None, body=None, extra_headers=None):
        self.metadata_calls.append(path)
        return {"status_code": self.metadata_status, "headers": {}, "body": self.metadata_xml}

    async def extract_all(self, path, conn=None, params=None, max_pages=10):
        self.extract_calls.append((path, conn, params, max_pages))
        return self.extract_result


def _install_keyed(monkeypatch, tmp_path, extract_result, **kwargs):
    odata = _KeyedOData(extract_result, **kwargs)
    monkeypatch.setattr(mcp_server, "_clients", lambda: (odata, _FakeSFAPI()))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()
    return odata


def test_odata_query_auto_adds_orderby_and_counts_duplicates(monkeypatch, tmp_path):
    # The third row repeats the first row's key — simulates the unstable
    # paging that drops/duplicates rows when no $orderby is given.
    rows = [
        {"userId": "1", "startDate": "2020-01-01", "jobCode": "A"},
        {"userId": "2", "startDate": "2020-01-01", "jobCode": "B"},
        {"userId": "1", "startDate": "2020-01-01", "jobCode": "A"},
    ]
    odata = _install_keyed(
        monkeypatch,
        tmp_path,
        {
            "stopped_reason": "exhausted",
            "total_records": len(rows),
            "pages_fetched": 2,
            "next_skiptoken": None,
            "results": rows,
            "last_page_size": 1,
        },
    )

    result = asyncio.run(mcp_server.odata_query(path="EmpJob"))

    assert result["orderby_added"] == "userId,startDate"
    assert odata.extract_calls[0][2]["$orderby"] == "userId,startDate", (
        "the derived $orderby must actually reach extract_all"
    )
    assert result["duplicate_records"] == 1
    assert any("duplicate" in w for w in result["warnings"])


def test_odata_query_respects_an_explicit_orderby(monkeypatch, tmp_path):
    odata = _install_keyed(
        monkeypatch,
        tmp_path,
        {
            "stopped_reason": "exhausted",
            "total_records": 1,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": [{"userId": "1", "jobCode": "A"}],
            "last_page_size": 1,
        },
    )

    result = asyncio.run(mcp_server.odata_query(path="EmpJob", params={"$orderby": "jobCode"}))

    assert "orderby_added" not in result
    assert odata.extract_calls[0][2]["$orderby"] == "jobCode"


def test_odata_query_warns_when_keys_cannot_be_determined(monkeypatch, tmp_path):
    # $metadata itself fails (permission, unknown entity, ...): auto-$orderby
    # must degrade to a warning, not fail the whole query.
    odata = _install_keyed(
        monkeypatch,
        tmp_path,
        {
            "stopped_reason": "exhausted",
            "total_records": 1,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": [{"id": "1"}],
            "last_page_size": 1,
        },
        metadata_status=403,
    )

    result = asyncio.run(mcp_server.odata_query(path="cust_MDFObject"))

    assert "orderby_added" not in result
    assert odata.extract_calls[0][2].get("$orderby") is None
    assert any("could not be determined" in w for w in result["warnings"])


def test_odata_query_warns_on_suspected_silent_truncation(monkeypatch, tmp_path):
    rows = [{"userId": str(i), "startDate": "d"} for i in range(100)]
    _install_keyed(
        monkeypatch,
        tmp_path,
        {
            "stopped_reason": "exhausted",
            "total_records": 100,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": rows,
            "last_page_size": 100,
        },
    )

    # max_pages=1 so this also proves the truncation check doesn't depend on
    # the auto-$orderby / duplicate-counting path being active.
    result = asyncio.run(mcp_server.odata_query(path="EmpJob", params={"$top": 100}, max_pages=1))

    assert any("exactly $top=100" in w for w in result["warnings"])


def test_odata_query_does_not_warn_when_page_is_smaller_than_top(monkeypatch, tmp_path):
    rows = [{"userId": "1", "startDate": "d"}]
    _install_keyed(
        monkeypatch,
        tmp_path,
        {
            "stopped_reason": "exhausted",
            "total_records": 1,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": rows,
            "last_page_size": 1,
        },
    )

    result = asyncio.run(mcp_server.odata_query(path="EmpJob", params={"$top": 100}, max_pages=1))

    assert "warnings" not in result


def test_odata_query_skips_duplicate_counting_when_select_omits_a_key_field(monkeypatch, tmp_path):
    # $select left out startDate, half of EmpJob's composite key. Two rows
    # that are genuinely distinct (different startDate) look identical once
    # that field is missing — must not be flagged as duplicates.
    rows = [
        {"userId": "1", "jobCode": "A"},
        {"userId": "1", "jobCode": "B"},
    ]
    _install_keyed(
        monkeypatch,
        tmp_path,
        {
            "stopped_reason": "exhausted",
            "total_records": 2,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": rows,
            "last_page_size": 2,
        },
    )

    result = asyncio.run(
        mcp_server.odata_query(
            path="EmpJob", params={"$select": "userId,jobCode", "$orderby": "userId"}
        )
    )

    assert "duplicate_records" not in result
    assert "warnings" not in result


_EDMX_UNSORTABLE_KEY = _EDMX_WITH_KEY.replace(
    '<Property Name="startDate" Type="Edm.DateTime"/>',
    '<Property Name="startDate" Type="Edm.DateTime" sap:sortable="false"/>',
)


def test_odata_query_warns_when_key_properties_are_not_sortable(monkeypatch, tmp_path):
    odata = _install_keyed(
        monkeypatch,
        tmp_path,
        {
            "stopped_reason": "exhausted",
            "total_records": 1,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": [{"userId": "1", "startDate": "d"}],
            "last_page_size": 1,
        },
        metadata_xml=_EDMX_UNSORTABLE_KEY,
    )

    result = asyncio.run(mcp_server.odata_query(path="EmpJob"))

    assert "orderby_added" not in result
    assert odata.extract_calls[0][2].get("$orderby") is None
    assert any("not sortable" in w for w in result["warnings"])
    assert "duplicate_records" not in result, "an unusable key is the same as no key for dedup too"


class _RetryOData(_KeyedOData):
    """Fails the first extract_all call (as if SF rejected the auto-added
    $orderby) and succeeds on the retry."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.orderby_seen: list[Any] = []

    async def extract_all(self, path, conn=None, params=None, max_pages=10):
        self.extract_calls.append((path, conn, params, max_pages))
        self.orderby_seen.append((params or {}).get("$orderby"))
        if len(self.extract_calls) == 1:
            return {
                "stopped_reason": "http_error",
                "last_status_code": 400,
                "total_records": 0,
                "pages_fetched": 0,
                "next_skiptoken": None,
                "results": [],
                "last_page_size": 0,
            }
        return self.extract_result


def test_odata_query_retries_without_orderby_when_sf_rejects_it(monkeypatch, tmp_path):
    odata = _RetryOData(
        {
            "stopped_reason": "exhausted",
            "total_records": 1,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": [{"userId": "1", "startDate": "d", "jobCode": "A"}],
            "last_page_size": 1,
        }
    )
    monkeypatch.setattr(mcp_server, "_clients", lambda: (odata, _FakeSFAPI()))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()

    result = asyncio.run(mcp_server.odata_query(path="EmpJob"))

    assert len(odata.extract_calls) == 2
    assert odata.orderby_seen == ["userId,startDate", None]
    assert result["total_records"] == 1
    assert "orderby_added" not in result
    assert any("rejected" in w for w in result["warnings"])


def test_odata_query_adds_snapshot_paging_for_multi_page_reads(monkeypatch, tmp_path):
    odata = _install_keyed(
        monkeypatch,
        tmp_path,
        {
            "stopped_reason": "exhausted",
            "total_records": 1,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": [{"userId": "1", "startDate": "d"}],
            "last_page_size": 1,
        },
    )

    result = asyncio.run(mcp_server.odata_query(path="EmpJob"))

    assert result["paging_added"] == "snapshot"
    assert odata.extract_calls[0][2]["paging"] == "snapshot"


def test_odata_query_does_not_override_caller_supplied_paging(monkeypatch, tmp_path):
    odata = _install_keyed(
        monkeypatch,
        tmp_path,
        {
            "stopped_reason": "exhausted",
            "total_records": 1,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": [{"userId": "1", "startDate": "d"}],
            "last_page_size": 1,
        },
    )

    result = asyncio.run(mcp_server.odata_query(path="EmpJob", params={"paging": "cursor"}))

    assert "paging_added" not in result
    assert odata.extract_calls[0][2]["paging"] == "cursor"


def test_odata_query_does_not_override_paging_given_in_path(monkeypatch, tmp_path):
    # "paging=cursor" lives in path's own query string, not in query_params —
    # the client merges the two later (split_path_query); odata_query must
    # still recognise it's present and skip adding its own.
    odata = _install_keyed(
        monkeypatch,
        tmp_path,
        {
            "stopped_reason": "exhausted",
            "total_records": 1,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": [{"userId": "1", "startDate": "d"}],
            "last_page_size": 1,
        },
    )

    result = asyncio.run(mcp_server.odata_query(path="EmpJob?paging=cursor"))

    assert "paging_added" not in result
    assert "paging" not in odata.extract_calls[0][2]


def test_odata_query_does_not_add_paging_for_a_single_page_request(monkeypatch, tmp_path):
    odata = _install_keyed(
        monkeypatch,
        tmp_path,
        {
            "stopped_reason": "exhausted",
            "total_records": 1,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": [{"userId": "1", "startDate": "d"}],
            "last_page_size": 1,
        },
    )

    result = asyncio.run(mcp_server.odata_query(path="EmpJob", max_pages=1))

    assert "paging_added" not in result
    assert "paging" not in odata.extract_calls[0][2]


class _PagingRetryOData:
    """Fails the first extract_all call with SF's "paging not supported"
    error (as PerPersonRelationship does for cursor paging); metadata lookups
    fail too so orderby_added never gets involved, isolating the paging
    fallback."""

    def __init__(self, success_result):
        self.success_result = success_result
        self.extract_calls: list[tuple] = []
        self.request_calls: list[str] = []
        # query_params is a single dict mutated in place across retries, so a
        # stored reference reflects its *final* state, not what it held at
        # call time — capture the value eagerly instead (mirrors
        # _RetryOData.orderby_seen above).
        self.paging_seen: list[Any] = []

    async def request(self, method, path, conn=None, params=None, body=None, extra_headers=None):
        self.request_calls.append(path)
        if path.rstrip("/").endswith("$metadata"):
            return {"status_code": 403, "headers": {}, "body": "no permission"}
        return {
            "status_code": 400,
            "headers": {},
            "body": "Snapshot based pagination not supported by PerPersonRelationship",
        }

    async def extract_all(self, path, conn=None, params=None, max_pages=10):
        self.extract_calls.append((path, conn, params, max_pages))
        self.paging_seen.append((params or {}).get("paging"))
        if len(self.extract_calls) == 1:
            return {
                "stopped_reason": "http_error",
                "last_status_code": 400,
                "total_records": 0,
                "pages_fetched": 0,
                "next_skiptoken": None,
                "results": [],
                "last_page_size": 0,
            }
        return self.success_result


def test_odata_query_retries_without_paging_when_sf_rejects_it(monkeypatch, tmp_path):
    odata = _PagingRetryOData(
        {
            "stopped_reason": "exhausted",
            "total_records": 1,
            "pages_fetched": 1,
            "next_skiptoken": None,
            "results": [{"personIdExternal": "1"}],
            "last_page_size": 1,
        }
    )
    monkeypatch.setattr(mcp_server, "_clients", lambda: (odata, _FakeSFAPI()))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()

    result = asyncio.run(mcp_server.odata_query(path="PerPersonRelationship"))

    assert len(odata.extract_calls) == 2
    assert odata.paging_seen == ["snapshot", None]
    assert result["total_records"] == 1
    assert "paging_added" not in result
    assert any("paging" in w.lower() and "rejected" in w.lower() for w in result["warnings"])


def test_instructions_and_tool_descriptions_fit_client_truncation_limit():
    # Claude Code cuts server instructions and tool descriptions at 2048 chars.
    assert len(mcp_server.mcp.instructions) <= 2048
    for tool in asyncio.run(mcp_server.mcp.list_tools()):
        assert len(tool.description or "") <= 2048, tool.name
