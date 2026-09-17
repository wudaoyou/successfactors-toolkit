"""Namespace-aware Compound Employee page validation shared by REST and MCP."""

from lxml import etree


def parse_page(
    body: str, status_code: int = 200
) -> tuple[int | None, bool | None, str | None, str | None]:
    try:
        root = etree.fromstring(
            body.encode(), parser=etree.XMLParser(resolve_entities=False, no_network=True)
        )
    except etree.XMLSyntaxError:
        return None, None, None, "http_error" if status_code >= 400 else "parse_error"
    if root.getroottree().docinfo.doctype:
        return None, None, None, "parse_error"
    if any(True for _ in root.iter("{*}Fault")):
        return None, None, None, "soap_fault"
    if status_code >= 400:
        return None, None, None, "http_error"

    def value(name: str) -> str | None:
        return next((node.text.strip() for node in root.iter("{*}" + name) if node.text), None)

    count, more, session = value("numResults"), value("hasMore"), value("querySessionId")
    if count is None or not count.isdigit() or more not in ("true", "false"):
        return None, None, session, "parse_error"
    has_more = more == "true"
    error = "missing_query_session" if has_more and not session else None
    return int(count), has_more, session, error


def parse_page_metadata(body: str) -> tuple[int | None, bool | None, str | None]:
    return parse_page(body)[:3]
