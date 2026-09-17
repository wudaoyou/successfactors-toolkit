"""MCP tool tests — no tenant is contacted; both clients are faked.

Two things are worth a check here: that Compound Employee payloads land on
disk and never travel back in the tool result (HR data), and that the EDMX
summary the model diffs across instances actually matches the metadata.
"""

import asyncio
import json
import stat
from pathlib import Path

from successfactors_toolkit import mcp_server
from successfactors_toolkit.config import get_settings

_EDMX = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">
  <edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm"
            xmlns:sap="http://www.sap.com/Protocols/SAPData" Namespace="SFOData">
      <EntityType Name="EmpJob">
        <Property Name="userId" Type="Edm.String" Nullable="false" sap:label="User"/>
        <Property Name="jobCode" Type="Edm.String" MaxLength="128" sap:required="true"/>
      </EntityType>
      <EntityType Name="FOCompany">
        <Property Name="externalCode" Type="Edm.String" Nullable="false"/>
      </EntityType>
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
    assert json.loads(open(result["file"], encoding="utf-8").read()) == fields


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


def test_odata_query_failure_surfaces_the_upstream_error_body(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_clients", lambda: (_FailingOData(), _FakeSFAPI()))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()

    result = asyncio.run(mcp_server.odata_query(path="EmpJob", company_id="example-a"))

    assert result["error"] == "http_error"
    assert result["status_code"] == 403
    assert "No permission" in result["body"], "the status code alone never says why"
    assert not (tmp_path / "results").exists()
