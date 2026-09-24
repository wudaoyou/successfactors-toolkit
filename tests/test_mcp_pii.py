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

_SSN = "123-45-6789"
_TOKEN = re.compile(r"\[PII-T1-[0-9a-f]{16}\]")


class _OData:
    def __init__(self, results=None, fail=False):
        self.results = results or []
        self.fail = fail
        self.sent: list[tuple[str, dict]] = []

    async def extract_all(self, path, conn=None, params=None, max_pages=10):
        self.sent.append((path, dict(params or {})))
        if self.fail:
            return {"stopped_reason": "http_error", "last_status_code": 400, "total_records": 0}
        return {
            "stopped_reason": "exhausted",
            "total_records": len(self.results),
            "pages_fetched": 1,
            "next_skiptoken": None,
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


def _install(monkeypatch, tmp_path, odata=None, sfapi=None, tier="1", vault_dir=None):
    monkeypatch.setattr(mcp_server, "_clients", lambda: (odata or _OData(), sfapi or _SFAPI("")))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("PII_FILTER_TIER", tier)
    monkeypatch.setenv("PII_VAULT_DIR", str(vault_dir or tmp_path / "vault"))
    get_settings.cache_clear()


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
    vault = Vault(tmp_path / "vault")
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


def test_main_fails_fast_on_bad_pii_settings(monkeypatch):
    monkeypatch.setenv("PII_FILTER_TIER", "9")
    get_settings.cache_clear()
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: pytest.fail("must not serve"))
    with pytest.raises(Exception, match="pii_filter_tier"):
        mcp_server.main()


def _seed(tmp_path, value):
    vault = Vault(tmp_path / "vault")
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
    monkeypatch.setenv("PII_FILTER_TIER", "1")
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
