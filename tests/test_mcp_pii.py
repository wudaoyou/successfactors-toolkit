"""MCP tools with PII tokenization: files and previews carry tokens, tokens in
requests reach SF as plaintext, and error bodies never echo that plaintext."""

import asyncio
import json
import logging
import re
from pathlib import Path

import pytest
from lxml import etree

from successfactors_toolkit import mcp_server
from successfactors_toolkit.config import get_settings
from successfactors_toolkit.services.odata_client import split_path_query
from successfactors_toolkit.services.pii_filter import PiiFilter, PiiVaultError, Vault
from tests.systems import write_system

_SSN = "123-45-6789"
_TOKEN = re.compile(r"\[PII-T1-[0-9a-f]{16}\]")


class _OData:
    def __init__(self, results=None, fail=False, next_skiptoken=None):
        self.results = results or []
        self.fail = fail
        self.next_skiptoken = next_skiptoken
        self.sent: list[tuple[str, dict]] = []

    async def extract_all(self, path, conn=None, params=None, max_pages=10):
        self.sent.append((path, dict(params or {})))
        if self.fail:
            return {"stopped_reason": "http_error", "last_status_code": 400, "total_records": 0}
        return {
            "stopped_reason": "exhausted",
            "total_records": len(self.results),
            "pages_fetched": 1,
            "next_skiptoken": self.next_skiptoken,
            "results": self.results,
        }

    async def request(self, method, path, conn=None, params=None, body=None, extra_headers=None):
        # SF echoes the filter in its error body. The padding puts the SSN
        # across the 2000-char cut: truncating before retokenizing would leak "123-4".
        echo = (params or {}).get("$filter", "")
        return {"status_code": 400, "headers": {}, "body": "x" * 1964 + f"Invalid filter: {echo}"}


class _SFAPI:
    def __init__(self, inner):
        self.inner = inner

    async def query(self, query_string, conn=None, params=None):
        body = (
            '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
            "<queryResponse><numResults>1</numResults><hasMore>false</hasMore>"
            f"<person><person_id_external>p1</person_id_external>{self.inner}</person>"
            "</queryResponse></SOAP-ENV:Envelope>"
        )
        return {"status_code": 200, "headers": {}, "body": body}


def _install(
    monkeypatch,
    tmp_path,
    odata=None,
    sfapi=None,
    tier=None,
    vault_dir=None,
    production=False,
    **file,
):
    """Fake clients; the only system, example-a, declared test at `tier`
    (absent: 1) unless production is True, or undeclared when it is None.
    `file` adds keys to example-a.json."""
    monkeypatch.setattr(mcp_server, "_clients", lambda: (odata or _OData(), sfapi or _SFAPI("")))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("PII_VAULT_DIR", str(vault_dir or tmp_path / "vault"))
    get_settings.cache_clear()
    if tier is not None:
        file["pii_filter_tier"] = int(tier)
    directory = write_system(get_settings().systems_dir, "example-a", production=production, **file)
    if production is None:
        (directory / "example-a.json").write_text('{"type": "successfactors"}', encoding="utf-8")


def _national_id_record():
    return {
        "__metadata": {"type": "SFOData.PerNationalId"},
        "personIdExternal": "p1",
        "nationalId": _SSN,
    }


def test_odata_query_writes_tokens_to_file_and_preview(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, odata=_OData([_national_id_record()]))
    result = asyncio.run(mcp_server.odata_query("PerNationalId", max_pages=1, preview=1))
    saved = Path(result["file"]).read_text(encoding="utf-8")
    assert _SSN not in saved and _SSN not in json.dumps(result)
    assert _TOKEN.fullmatch(result["preview"][0]["nationalId"])
    assert result["pii_filter_tier"] == 1 and result["pii_tokenized"] == 1
    assert "PII" in result["pii_note"]


def test_odata_query_tier_zero_writes_plaintext_and_no_pii_keys(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, odata=_OData([_national_id_record()]), tier="0")
    result = asyncio.run(mcp_server.odata_query("PerNationalId", max_pages=1))
    assert _SSN in Path(result["file"]).read_text(encoding="utf-8")
    assert not any(key.startswith("pii_") for key in result)
    assert not (tmp_path / "vault").exists()


def test_odata_query_sends_plaintext_for_a_token_and_retokenizes_the_error_body(
    monkeypatch, tmp_path
):
    vault = Vault(tmp_path / "vault", "example-a")
    token = f"[PII-T1-{vault.digest(_SSN)}]"
    vault.save({vault.digest(_SSN): _SSN})
    odata = _OData(fail=True)
    _install(monkeypatch, tmp_path, odata=odata)
    result = asyncio.run(
        mcp_server.odata_query(
            "PerNationalId", max_pages=1, params={"$filter": f"nationalId eq '{token}'"}
        )
    )
    assert odata.sent[0][1]["$filter"] == f"nationalId eq '{_SSN}'"
    assert result["error"] == "http_error"
    assert "123-4" not in result["body"]
    assert len(result["body"]) == 2000


def test_odata_query_tokenizes_a_status_200_body_recovered_after_a_parse_error(
    monkeypatch, tmp_path
):
    # parse_error only fires when the original response status was <400 (the
    # JSON just failed to decode). The follow-up probe request re-fetches the
    # same query and, here, gets back a full valid page: that's not an SF
    # fault, so it must be tokenized like any other page rather than echoed
    # raw as an "error" body.
    class _ODataFlakyRetry:
        async def extract_all(self, path, conn=None, params=None, max_pages=10):
            return {"stopped_reason": "parse_error", "last_status_code": 200, "total_records": 0}

        async def request(
            self, method, path, conn=None, params=None, body=None, extra_headers=None
        ):
            return {
                "status_code": 200,
                "headers": {},
                "body": json.dumps({"d": {"results": [_national_id_record()]}}),
            }

    _install(monkeypatch, tmp_path, odata=_ODataFlakyRetry())
    result = asyncio.run(mcp_server.odata_query("PerNationalId", max_pages=1))
    assert result["error"] == "parse_error"
    assert _SSN not in json.dumps(result)
    assert _TOKEN.search(result["body"])


def test_odata_query_next_skiptoken_is_a_token_that_resumes_paging(monkeypatch, tmp_path):
    # A server may build $skiptoken from the last row's key, which can be PII.
    skiptoken = f"nationalId-'{_SSN}'"
    odata = _OData([_national_id_record()], next_skiptoken=skiptoken)
    _install(monkeypatch, tmp_path, odata=odata)
    first = asyncio.run(mcp_server.odata_query("PerNationalId", max_pages=1))
    assert _SSN not in json.dumps(first)
    assert _TOKEN.fullmatch(first["next_skiptoken"])
    asyncio.run(
        mcp_server.odata_query(
            "PerNationalId", max_pages=1, params={"$skiptoken": first["next_skiptoken"]}
        )
    )
    assert odata.sent[1][1]["$skiptoken"] == skiptoken


def test_odata_query_next_skiptoken_is_plaintext_at_tier_zero(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, odata=_OData(next_skiptoken="abc"), tier="0")
    assert asyncio.run(mcp_server.odata_query("PerNationalId"))["next_skiptoken"] == "abc"


def test_odata_query_unknown_token_is_refused_before_any_request(monkeypatch, tmp_path):
    odata = _OData()
    _install(monkeypatch, tmp_path, odata=odata)
    result = asyncio.run(
        mcp_server.odata_query("PerNationalId?$filter=nationalId eq '[PII-T1-0123456789abcdef]'")
    )
    assert result["error"] == "pii_unknown_token"
    assert result["tokens"] == ["[PII-T1-0123456789abcdef]"]
    assert odata.sent == []


def test_odata_query_unusable_vault_writes_nothing(monkeypatch, tmp_path):
    (tmp_path / "not_a_dir").write_text("x")
    _install(
        monkeypatch,
        tmp_path,
        odata=_OData([_national_id_record()]),
        vault_dir=tmp_path / "not_a_dir",
    )
    result = asyncio.run(mcp_server.odata_query("PerNationalId", max_pages=1))
    assert result["error"] == "pii_vault_unavailable"
    assert not (tmp_path / "results").exists()


def test_ce_query_tokenizes_each_page(monkeypatch, tmp_path):
    inner = f"<national_id_card><national_id>{_SSN}</national_id></national_id_card>"
    _install(monkeypatch, tmp_path, sfapi=_SFAPI(inner))
    result = asyncio.run(mcp_server.ce_query(person_id_external="p1"))
    [page] = result["files"]
    text = Path(page).read_text(encoding="utf-8")
    assert _SSN not in text and _TOKEN.search(text)
    assert result["pii_tokenized"] == 1 and result["pii_filter_tier"] == 1


def test_ce_query_missing_query_session_tokenizes_the_body_before_returning_it(
    monkeypatch, tmp_path
):
    # missing_query_session (hasMore=true, no querySessionId) is SF's own
    # continuation token going missing on an otherwise normal, full page of
    # employee data -- not a fault -- so the page must be tokenized like any
    # other before it's shown as an "error" body.
    inner = f"<national_id_card><national_id>{_SSN}</national_id></national_id_card>"
    body = (
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
        "<queryResponse><numResults>1</numResults><hasMore>true</hasMore>"
        f"<person><person_id_external>p1</person_id_external>{inner}</person>"
        "</queryResponse></SOAP-ENV:Envelope>"
    )

    class _SFAPIMissingSession:
        async def query(self, query_string, conn=None, params=None):
            return {"status_code": 200, "headers": {}, "body": body}

    _install(monkeypatch, tmp_path, sfapi=_SFAPIMissingSession())
    result = asyncio.run(mcp_server.ce_query(person_id_external="p1"))
    assert result["error"] == "missing_query_session"
    assert _SSN not in json.dumps(result)
    assert _TOKEN.search(result["body"])


def test_ce_query_tokenize_failure_writes_nothing(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    def broken(self, xml):
        raise etree.XMLSyntaxError("boom", 0, 0, 0)

    monkeypatch.setattr(PiiFilter, "tokenize_xml", broken)
    result = asyncio.run(mcp_server.ce_query(person_id_external="p1"))
    assert result == {"error": "pii_tokenize_failed", "failed_on_page": 1, "files": []}
    assert not (tmp_path / "results").exists()


def test_main_fails_fast_without_a_systems_dir(monkeypatch):
    monkeypatch.delenv("SYSTEMS_DIR")
    get_settings.cache_clear()
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: pytest.fail("must not serve"))
    with pytest.raises(Exception, match="SYSTEMS_DIR"):
        mcp_server.main()


def _seed(tmp_path, value):
    vault = Vault(tmp_path / "vault", "example-a")
    vault.save({vault.digest(value): value})
    return f"[PII-T1-{vault.digest(value)}]"


@pytest.mark.parametrize("tier", ["0", "1"])
def test_odata_query_drops_uris_only_when_tokenizing(monkeypatch, tmp_path, tier):
    uri = "https://api/odata/v2/EmpWorkPermit(documentNumber='A1234567',userId='u1')"
    record = {
        "__metadata": {"uri": uri, "type": "SFOData.EmpWorkPermit"},
        "userId": "u1",
        "documentNumber": "A1234567",
        "userNav": {"__deferred": {"uri": uri + "/userNav"}},
    }
    _install(monkeypatch, tmp_path, odata=_OData([record]), tier=tier)
    result = asyncio.run(mcp_server.odata_query("EmpWorkPermit", max_pages=1))
    saved = json.loads(Path(result["file"]).read_text(encoding="utf-8"))
    if tier == "0":
        assert saved == [record]
    else:
        assert "A1234567" not in json.dumps(saved)


def test_odata_query_token_in_path_query_keeps_special_characters(monkeypatch, tmp_path):
    email = "a+b&c's@x.com"
    token = _seed(tmp_path, email)
    odata = _OData()
    _install(monkeypatch, tmp_path, odata=odata)
    asyncio.run(mcp_server.odata_query(f"PerEmail?$filter=emailAddress eq '{token}'", max_pages=1))
    # The client splits the path the same way before sending.
    _, params = split_path_query(odata.sent[0][0])
    assert params == {"$filter": "emailAddress eq 'a+b&c''s@x.com'"}


def test_main_quiets_httpx_request_logging_when_tokenizing(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: None)
    logger = logging.getLogger("httpx")
    before = logger.level
    try:
        logger.setLevel(logging.INFO)
        mcp_server.main()
        assert logger.level == logging.WARNING
    finally:
        logger.setLevel(before)


def test_ce_query_tier_zero_writes_plaintext_and_no_pii_keys(monkeypatch, tmp_path):
    inner = f"<national_id_card><national_id>{_SSN}</national_id></national_id_card>"
    _install(monkeypatch, tmp_path, sfapi=_SFAPI(inner), tier="0")
    result = asyncio.run(mcp_server.ce_query(person_id_external="p1"))
    [page] = result["files"]
    assert _SSN in Path(page).read_text(encoding="utf-8")
    assert not any(key.startswith("pii_") for key in result)
    assert not (tmp_path / "vault").exists()


@pytest.mark.parametrize(("status", "shown"), [(401, True), (200, False)])
def test_ce_query_non_xml_error_body_is_shown_only_for_a_fault_status(
    monkeypatch, tmp_path, status, shown
):
    page = "<html>Unauthorized" + "!" * 3000

    class _SFAPIHtml:
        async def query(self, query_string, conn=None, params=None):
            return {"status_code": status, "headers": {}, "body": page}

    _install(monkeypatch, tmp_path, sfapi=_SFAPIHtml())
    result = asyncio.run(mcp_server.ce_query(person_id_external="p1"))
    assert result["body"] == (page[:2000] if shown else None)


def test_ce_query_vault_failure_on_a_later_page_reports_the_files_written(monkeypatch, tmp_path):
    def page(more):
        return {
            "status_code": 200,
            "headers": {},
            "body": (
                '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
                f"<queryResponse><numResults>1</numResults><hasMore>{more}</hasMore>"
                "<querySessionId>s1</querySessionId><person/></queryResponse>"
                "</SOAP-ENV:Envelope>"
            ),
        }

    class _SFAPITwoPages:
        async def query(self, query_string, conn=None, params=None):
            return page("true")

        async def query_more(self, session, conn=None):
            return page("false")

    real = PiiFilter.tokenize_xml
    calls = []

    def fail_second(self, xml):
        calls.append(xml)
        if len(calls) == 2:
            raise PiiVaultError("disk gone")
        return real(self, xml)

    _install(monkeypatch, tmp_path, sfapi=_SFAPITwoPages())
    monkeypatch.setattr(PiiFilter, "tokenize_xml", fail_second)
    result = asyncio.run(mcp_server.ce_query(person_id_external="p1"))
    assert result["error"] == "pii_vault_unavailable"
    [written] = result["files"]
    assert Path(written).exists()


class _SFAPICounting(_SFAPI):
    def __init__(self):
        super().__init__("")
        self.calls = 0

    async def query(self, query_string, conn=None, params=None):
        self.calls += 1
        return await super().query(query_string, conn, params)


@pytest.mark.parametrize(
    ("system", "code"),
    [("", "system_invalid"), ("example-a", "system_invalid"), ("nope", "system_unknown")],
)
def test_undeclared_or_unknown_system_is_refused_before_any_request(
    monkeypatch, tmp_path, system, code
):
    odata, sfapi = _OData([_national_id_record()]), _SFAPICounting()
    _install(monkeypatch, tmp_path, odata=odata, sfapi=sfapi, production=None)
    name = system or "example-a"
    for result in (
        asyncio.run(mcp_server.odata_query("PerNationalId", system=system)),
        asyncio.run(mcp_server.ce_query(system=system)),
    ):
        assert result["error"] == code and result["system"] == name
    assert odata.sent == [] and sfapi.calls == 0
    assert not (tmp_path / "results").exists() and not (tmp_path / "vault").exists()


def _person():
    return {"__metadata": {"type": "SFOData.PerPersonal"}, "firstName": "Alice"}


@pytest.mark.parametrize("tier", [None, "3"])
def test_production_system_tokenizes_tier_three(monkeypatch, tmp_path, tier):
    _install(monkeypatch, tmp_path, odata=_OData([_person()]), tier=tier, production=True)
    result = asyncio.run(mcp_server.odata_query("PerPersonal", max_pages=1, preview=1))
    assert re.fullmatch(r"\[PII-T3-[0-9a-f]{16}\]", result["preview"][0]["firstName"])
    assert result["pii_filter_tier"] == 3


def test_test_system_at_default_tier_leaves_tier_three_plain(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, odata=_OData([_person()]))
    result = asyncio.run(mcp_server.odata_query("PerPersonal", max_pages=1, preview=1))
    assert result["preview"][0]["firstName"] == "Alice" and result["pii_filter_tier"] == 1


def test_each_call_uses_its_own_systems_flag(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, production=True)
    assert asyncio.run(mcp_server.ce_query())["pii_filter_tier"] == 3
    write_system(get_settings().systems_dir, "example-b")
    assert asyncio.run(mcp_server.ce_query())["error"] == "system_required"
    assert asyncio.run(mcp_server.ce_query(system="example-a"))["pii_filter_tier"] == 3
    assert asyncio.run(mcp_server.ce_query(system="example-b"))["pii_filter_tier"] == 1


# --- query guard, system-bound tokens, fail-closed results ---------------


def _query(path, params=None, **kwargs):
    return asyncio.run(mcp_server.odata_query(path, params=params, max_pages=1, **kwargs))


def _last_name_record(entity="PerPersonal", name="Smith"):
    return {"__metadata": {"type": f"SFOData.{entity}"}, "personIdExternal": "p1", "lastName": name}


def test_a_token_from_another_system_is_unknown_and_nothing_is_sent(monkeypatch, tmp_path):
    odata = _OData([_last_name_record()])
    _install(monkeypatch, tmp_path, odata=odata, production=True)
    token = _query("PerPersonal", preview=1)["preview"][0]["lastName"]
    write_system(get_settings().systems_dir, "example-b")
    assert re.fullmatch(r"\[PII-T3-[0-9a-f]{16}\]", token)
    sent_before = len(odata.sent)
    result = _query("PerPersonal", {"$filter": f"lastName eq '{token}'"}, system="example-b")
    assert result["error"] == "pii_unknown_token" and result["tokens"] == [token]
    assert "refresh" in result["detail"] or "re-run" in result["detail"]
    assert len(odata.sent) == sent_before


_PROBES = [
    "startswith(nationalId,'1')",
    "nationalId ge '5'",
    "personNav/lastName ge 'M'",
    "lastName eq 'Smith'",
]


def _refused(monkeypatch, tmp_path, path, params=None, production=True):
    odata = _OData([_last_name_record()])
    _install(monkeypatch, tmp_path, odata=odata, production=production)
    result = _query(path, params)
    assert result["error"] == "pii_query_refused", result
    assert odata.sent == []
    return result


@pytest.mark.parametrize("probe", _PROBES)
def test_production_system_refuses_a_probing_filter_in_path_or_params(monkeypatch, tmp_path, probe):
    _refused(monkeypatch, tmp_path, f"PerPersonal?$filter={probe}")
    _refused(monkeypatch, tmp_path, "PerPersonal", {"$filter": probe})


@pytest.mark.parametrize(
    "option",
    ["$orderby=dateOfBirth", "$search=Smith", "$expand=personNav($filter=lastName ge 'M')"],
)
def test_production_system_refuses_orderby_search_and_nested_options(monkeypatch, tmp_path, option):
    name, value = option.split("=", 1)
    _refused(monkeypatch, tmp_path, f"PerPersonal?{option}")
    _refused(monkeypatch, tmp_path, "PerPersonal", {name: value})


def test_production_system_refuses_a_bad_second_filter_in_the_path(monkeypatch, tmp_path):
    _refused(
        monkeypatch,
        tmp_path,
        "PerPersonal?$filter=personIdExternal eq 'p1'&$filter=nationalId gt '1'",
    )


def test_production_system_allows_token_equality_and_sends_plaintext(monkeypatch, tmp_path):
    odata = _OData()
    _install(monkeypatch, tmp_path, odata=odata, production=True)
    token = _seed(tmp_path, "Smith")
    result = _query("PerPersonal", {"$filter": f"lastName eq '{token}'"})
    assert "error" not in result
    assert odata.sent[0][1]["$filter"] == "lastName eq 'Smith'"
    _query(f"PerPersonal?$filter=lastName eq '{token}'")
    _, params = split_path_query(odata.sent[1][0])
    assert params == {"$filter": "lastName eq 'Smith'"}


@pytest.mark.parametrize(
    "query",
    ["EmpJob?$filter=company in 'A','B'", "EmpJob?$filter=jobInfoNav/company eq 'X'"],
)
def test_production_system_allows_filters_on_non_pii_fields(monkeypatch, tmp_path, query):
    odata = _OData()
    _install(monkeypatch, tmp_path, odata=odata, production=True)
    assert "error" not in _query(query)
    assert len(odata.sent) == 1


def test_tier_zero_test_system_has_no_query_guard(monkeypatch, tmp_path):
    odata = _OData()
    _install(monkeypatch, tmp_path, odata=odata, tier="0", production=False)
    result = _query("PerNationalId", {"$filter": "nationalId ge '5'"})
    assert "error" not in result
    assert odata.sent[0][1]["$filter"] == "nationalId ge '5'"


def _stub_keys(monkeypatch, keys):
    async def fake(system, entity):
        return keys

    monkeypatch.setattr(mcp_server, "_entity_key_properties", fake)


def test_auto_orderby_is_skipped_when_a_key_is_tokenized(monkeypatch, tmp_path):
    odata = _OData()
    _install(monkeypatch, tmp_path, odata=odata)
    _stub_keys(monkeypatch, ["documentNumber", "userId"])
    result = asyncio.run(mcp_server.odata_query("EmpWorkPermit", max_pages=2))
    assert "$orderby" not in odata.sent[0][1] and "orderby_added" not in result
    [warning] = [w for w in result["warnings"] if "tokenized PII" in w]
    assert "$orderby" in warning and "non-PII" in warning


def test_auto_orderby_is_still_added_for_non_pii_keys(monkeypatch, tmp_path):
    odata = _OData()
    _install(monkeypatch, tmp_path, odata=odata)
    _stub_keys(monkeypatch, ["userId", "seqNumber"])
    result = asyncio.run(mcp_server.odata_query("EmpJob", max_pages=2))
    assert odata.sent[0][1]["$orderby"] == "userId,seqNumber"
    assert result["orderby_added"] == "userId,seqNumber"


def _foo_and_job():
    return [
        {"__metadata": {"type": "SFOData.cust_Foo"}, "externalCode": "E1", "count": 3},
        {"__metadata": {"type": "SFOData.EmpJob"}, "userId": "u1", "jobCode": "J1"},
    ]


def test_unknown_entity_records_are_tokenized_on_a_tier_one_system(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, odata=_OData(_foo_and_job()))
    result = _query("cust_Foo", preview=2)
    foo, job = result["preview"]
    assert _TOKEN.fullmatch(foo["externalCode"]) and foo["count"] == 3
    assert job["userId"] == "u1" and job["jobCode"] == "J1"
    assert result["pii_tokenized"] == 1


def test_untyped_records_take_the_entity_from_the_path(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, odata=_OData([{"externalCode": "E1"}]))
    assert _TOKEN.fullmatch(
        _query("cust_Foo?$select=externalCode", preview=1)["preview"][0]["externalCode"]
    )


def test_an_extra_fields_entry_marks_a_custom_entity_as_reviewed(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, odata=_OData(_foo_and_job()), pii_extra_fields={"cust_Foo": {}})
    foo, job = _query("cust_Foo", preview=2)["preview"]
    assert foo["externalCode"] == "E1" and job["jobCode"] == "J1"
