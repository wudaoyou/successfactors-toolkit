"""MCP (stdio) front end for this repo's SuccessFactors clients.

Exposes five tools — system listing, OData $metadata, cross-instance metadata
comparison, OData query, and Compound Employee query — to an MCP host such as
Claude Desktop or Claude Code.

Everything hard is reused from ``successfactors_toolkit.services``: OAuth2 SAML
Bearer, per-system keys, ``queryMore`` paging, next-link following. This module
only maps tool arguments onto those clients and keeps payloads out of the
model's context — results are written under ``{RESULTS_DIR}/mcp/`` and the tool
returns a path plus counts. That matters twice over: a single Compound Employee
payload runs ~80 KB, and it is HR data that has no business being echoed into a
chat transcript.

Run with ``successfactors-mcp`` or ``python -m successfactors_toolkit.mcp_server`` (stdio).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import parse_qsl

import httpx
from lxml import etree
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from successfactors_toolkit.config import get_settings
from successfactors_toolkit.models.common import ODataConnectionConfig, SFAPIConnectionConfig
from successfactors_toolkit.models.sfapi import CEQueryFilter
from successfactors_toolkit.services import pii_filter, pii_query_guard
from successfactors_toolkit.services.ce_query_builder import COMMON_SEGMENTS, build_query_string
from successfactors_toolkit.services.ce_response import parse_page
from successfactors_toolkit.services.http_limits import ExtractBudget
from successfactors_toolkit.services.odata_client import (
    ODataClient,
    split_path_query,
    v4_service_root,
)
from successfactors_toolkit.services.pii_filter import PiiUnknownTokenError, PiiVaultError
from successfactors_toolkit.services.sfapi_client import SFAPIClient
from successfactors_toolkit.services.system_store import SF_TYPE, SystemStore, SystemUnavailable

# Below this size a metadata summary is small enough to hand the model directly
# instead of making it open the file — the usual case for a single entity.
_INLINE_LIMIT = 20_000
_PREVIEW_INLINE_LIMIT = 16 * 1024

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")
# Prefixes seen in the wild include SOAP-ENV, soapenv and S — the hyphen is why
# this is not just \w+.


@asynccontextmanager
async def _lifespan(server):
    global _http, _odata, _sfapi
    try:
        yield {}
    finally:
        if _http is not None:
            await _http.aclose()
        _http = _odata = _sfapi = None


mcp = MCPServer(
    name="successfactors",
    lifespan=_lifespan,
    # Claude Code truncates server instructions (and tool descriptions) at
    # 2048 chars — keep both under that; tests enforce it.
    instructions=(
        "SAP SuccessFactors: OData v2 (any entity set) or v4 services, and the "
        "EC Compound Employee SOAP API. Call list_systems first for the system name. "
        "compare_metadata diffs two instances. Large results go to a "
        "file (container path; under Docker, the host dir bound to "
        "RESULTS_DIR) — read it for the records.\n\n"
        "How to query (in order):\n"
        "1. Decide the population filter first, usually on EmpJob (company, "
        "location, department, emplStatus). Put employment status in it "
        'explicitly: resolve the emplStatus picklist; "active" usually '
        "includes paid/unpaid leave, not just A — say which statuses you "
        "counted. Never rely on a navigation path to drop terminated "
        "employees.\n"
        "2. List the entities the question needs.\n"
        "3. Apply the same filter to each entity server-side, directly or via "
        "navigation in $filter: EmpEmployment `jobInfoNav/...`; PerPerson "
        "`employmentNav/jobInfoNav/...`; Per* with personNav "
        "(PerPersonRelationship, PerNationalId, ...) "
        "`personNav/employmentNav/jobInfoNav/...`; BenefitEnrollment "
        "`workerIdNav/empInfo/jobInfoNav/...`. odata_metadata lists an "
        "entity's navigation properties. Only when no path exists, pull the "
        "entity in full and join locally.\n"
        "4. Resolve codes in bulk: $expand the `<field>Nav`, or query "
        "PicklistOption with `id in 1,2,...` — no N+1 calls.\n\n"
        "Also:\n"
        "- Effective-dated entities (EmpJob, Position, FO*, MDF) return "
        "today's slice unless you pass fromDate/toDate or asOfDate.\n"
        "- Join keys: userId (EmpJob, EmpEmployment, BenefitEnrollment "
        "workerId); personIdExternal (Per*); EmpEmployment bridges both. "
        "PerPersonRelationship maps personIdExternal to a dependent's "
        "relatedPersonIdExternal via relationshipType (picklist); "
        "$expand=relNationalIdNav returns dependents' IDs in the same call.\n"
        "- To check whether a national ID exists, select only cardType/country "
        "— never nationalId values unless the user asks for them.\n"
        "- Multi-value filters: `field in 'a','b'` (no parens), never "
        "`eq...or eq...`; chunk lists over ~1000 values or long URLs.\n"
        "- User returns active users only by default; add "
        "`status in 't','f','T','F'` for inactive too."
    ),
)

_http: httpx.AsyncClient | None = None
_odata: ODataClient | None = None
_sfapi: SFAPIClient | None = None


def _clients() -> tuple[ODataClient, SFAPIClient]:
    """Build the shared clients on first use; their token/session caches live
    on the instances, so the whole process must share one of each."""
    global _http, _odata, _sfapi
    if _http is None:
        settings = get_settings()
        # Same bounded pool as successfactors_toolkit.main: SF rate-limits per tenant.
        _http = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            http2=True,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
        )
        _odata = ODataClient(settings, _http)
        _sfapi = SFAPIClient(settings, _http)
    return _odata, _sfapi  # type: ignore[return-value]


def _write(content: str, tool: str, system: str, suffix: str) -> str:
    """Write a payload under ``{results_dir}/mcp/`` and return its path."""
    # Resolved per call, not at import: the setting is only known once the
    # environment and .env have been read, and tests monkeypatch it.
    settings = get_settings()
    out_dir = settings.results_dir / "mcp"
    out_dir.mkdir(parents=True, exist_ok=True)
    if settings.results_retention_days:
        _prune(out_dir, time.time() - settings.results_retention_days * 86400)
    # Microseconds keep two calls in the same second from overwriting each other.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    name = _SAFE_NAME.sub("_", system) or "default"
    path = out_dir / f"{tool}_{name}_{stamp}.{suffix}"
    # O_EXCL plus mode 0o600: HR payloads are never world-readable, and an
    # existing name is an error rather than an overwrite.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
    return str(path)


def _prune(out_dir: os.PathLike[str], cutoff: float) -> None:
    """Delete regular files in `out_dir` last modified before `cutoff`."""
    for entry in os.scandir(out_dir):
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            # Removed by a concurrent call, or not ours to delete: never fail the write.
            pass


def _attach_preview(out: dict[str, Any], records: list[Any], preview: int) -> None:
    """Inline the first `preview` records when they fit in 16 KiB."""
    if preview <= 0:
        return
    head = records[:preview]
    encoded = json.dumps(head, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= _PREVIEW_INLINE_LIMIT:
        out["preview"] = head
    else:
        out["preview_error"] = (
            "Requested preview exceeds the 16 KiB inline limit; inspect the saved file locally."
        )


_PII_NOTE = (
    "Values like [PII-T1-<hex>] are PII tokens, per system: the same value gives "
    "the same token on the same system, so compare, group and count them freely. "
    "In $filter a tokenized field can only be compared with eq, ne or in against "
    "such tokens (the server resolves them); it can't be sorted, searched or "
    "passed to functions. Records of entities the PII map doesn't cover have "
    "every text value tokenized. "
    "[PII-T<n>-REDACTED] marks binary content that is not available."
)


def _pii_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, pii_filter.SystemRefused):
        return _system_error(exc)
    if isinstance(exc, PiiUnknownTokenError):
        return {
            "error": "pii_unknown_token",
            "tokens": exc.tokens,
            "detail": (
                "Tokens only resolve on the system whose results they came from, and "
                "tokens from an older server version must be refreshed by re-running "
                "the query on this system."
            ),
        }
    if isinstance(exc, pii_query_guard.PiiQueryRefused):
        return {"error": "pii_query_refused", "detail": str(exc)}
    return {"error": "pii_vault_unavailable", "detail": str(exc)}


def _pii_request(
    path: str, params: dict[str, Any] | None, system: str
) -> tuple[pii_filter.PiiFilter | None, str, dict[str, Any] | None, dict[str, str]]:
    """The system's filter, plus path/params with any tokens resolved to
    plaintext and the substitutions made (for retokenizing error bodies).
    `system` is a name `_select` (or plugin_api.select_system) returned.
    Raises PiiVaultError (SystemRefused included) / PiiUnknownTokenError."""
    pii = pii_filter.for_system(get_settings(), system)
    if pii is None:
        return None, path, params, {}
    # The query half is parsed (and URL-decoded) later, so values put there
    # are percent-encoded to keep a +, & or = in the plaintext intact.
    head, sep, query = path.partition("?")
    head, subs = pii_filter.detokenize(head, pii.vault)
    query, more = pii_filter.detokenize(query, pii.vault, encode=True)
    subs.update(more)
    path = head + sep + query
    if params is not None:
        resolved = {}
        for key, value in params.items():
            if isinstance(value, str):
                value, more = pii_filter.detokenize(value, pii.vault)
                subs.update(more)
            resolved[key] = value
        params = resolved
    return pii, path, params, subs


def _pii_mark(out: dict[str, Any], pii: pii_filter.PiiFilter | None, count: int) -> None:
    if pii is not None:
        out["pii_filter_tier"] = pii.tier
        out["pii_tokenized"] = count
        out["pii_note"] = _PII_NOTE


def _pii_tokenize_json_records(
    text: str, pii: pii_filter.PiiFilter, *, fail_closed: bool = False
) -> str | None:
    """`text`'s OData records, tokenized -- v2 ({"d": {"results": [...]}},
    {"d": {...}} for a single entity, or a bare list) or v4 ({"value": [...]},
    or the entity object itself) -- or None if it doesn't parse as JSON in one
    of those shapes and so can't be safely returned. An object without "d" is
    read as v4, so untyped records in it get the fail-safe rules. Raises
    PiiVaultError if the vault itself is unavailable."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    v4 = isinstance(parsed, dict) and "d" not in parsed
    data = parsed.get("d", parsed) if isinstance(parsed, dict) else parsed
    if v4 and isinstance(data.get("value"), list):
        records = data["value"]
    else:
        records = data.get("results", [data]) if isinstance(data, dict) else data
    if not isinstance(records, list):
        return None
    records, _ = pii.tokenize_records(records, v4=v4, fail_closed=fail_closed)
    return json.dumps(records, ensure_ascii=False, default=str)


def _pii_safe_error_body(
    text: str,
    status_code: int,
    pii: pii_filter.PiiFilter | None,
    subs: dict[str, str],
    *,
    fail_closed: bool = False,
) -> str | None:
    """The body to hand back for a failed odata_query call. A >=400
    status is a genuine SF fault: only the plaintext the caller's own request
    put on the wire is put back under its token. A <400 status means the
    request that produced this body actually succeeded — it's not a fault,
    so its shape can't be trusted to be free of untokenized PII — so it's
    tokenized like any other OData JSON page, or withheld if it doesn't even
    parse as one. Raises PiiVaultError if the vault itself is unavailable."""
    if pii is None:
        return text
    if status_code >= 400:
        return pii_filter.retokenize(text, subs)
    return _pii_tokenize_json_records(text, pii, fail_closed=fail_closed)


def _edmx_root(xml: str):
    """Parse EDMX with the hardened settings every parser below needs (no
    DTD/external entities, no network) and reject a DOCTYPE outright."""
    root = etree.fromstring(
        xml.encode("utf-8"), parser=etree.XMLParser(resolve_entities=False, no_network=True)
    )
    if root.getroottree().docinfo.doctype:
        raise etree.XMLSyntaxError("DOCTYPE is not supported", 0, 0, 0)
    return root


def _parse_edmx(xml: str) -> dict[str, dict[str, dict[str, str]]]:
    """EDMX -> {EntityType: {Property: {attribute: value}}}.

    Attribute names are reduced to their local part so SAP's annotations
    (sap:label, sap:required, sap:picklist, ...) stay readable — those
    annotations are the actual configuration being compared across instances.
    """
    root = _edmx_root(xml)
    out: dict[str, dict[str, dict[str, str]]] = {}
    for entity in root.iter("{*}EntityType"):
        out[entity.get("Name", "")] = {
            prop.get("Name", ""): {
                etree.QName(key).localname: value
                for key, value in prop.attrib.items()
                if key != "Name"
            }
            for prop in entity.iter("{*}Property")
        }
    return out


_SAP_NS = "http://www.sap.com/Protocols/SAPData"


def _parse_edmx_keys(xml: str) -> dict[str, list[str]]:
    """EDMX -> {EntityType: [key property names, in declaration order]}.

    Maps to ``[]`` (found, but unusable), rather than the real names, when any
    key property is annotated ``sap:sortable="false"`` — SF answers 400 to an
    ``$orderby`` naming one of those, so it's not usable for auto-ordering even
    though the key itself is known. Missing sap:sortable is treated as sortable
    (SAP's own default). v4 CSDL has no sap: attributes (its SortRestrictions
    annotations sit on the entity set, not read here), so v4 keys always
    count as sortable; odata_query retries without an $orderby SF rejects.
    """
    root = _edmx_root(xml)
    out: dict[str, list[str]] = {}
    for entity in root.iter("{*}EntityType"):
        key_names = [
            ref.get("Name", "") for ref in entity.iter("{*}PropertyRef") if ref.get("Name")
        ]
        sortable = {
            prop.get("Name"): prop.get(f"{{{_SAP_NS}}}sortable", "true")
            for prop in entity.iter("{*}Property")
        }
        if key_names and any(sortable.get(name) == "false" for name in key_names):
            key_names = []
        out[entity.get("Name", "")] = key_names
    return out


_NavMap = dict[str, list[dict[str, str]]]


def _parse_edmx_navs(xml: str) -> _NavMap:
    """EDMX -> {EntityType: [{name, target, filterable}, ...]}.

    Only the full service EDMX carries NavigationProperty/Association
    elements at all — entity-scoped $metadata omits them. `target` is the
    navigation's target EntityType, resolved via the NavigationProperty's
    Relationship -> the matching Association's End for ToRole; if an
    Association can't be matched (unexpected EDMX shape), `target` falls
    back to the raw ToRole so the caller still gets something useful.
    v4 CSDL names the target in Type ("NS.Target" or "Collection(NS.Target)")
    and has no sap:filterable (its FilterRestrictions annotations sit on the
    entity set, not read here), so v4 navigations report filterable "true".
    """
    root = _edmx_root(xml)
    # Association name -> {Role: target EntityType local name}
    associations: dict[str, dict[str, str]] = {}
    for assoc in root.iter("{*}Association"):
        ends = {}
        for end in assoc.iter("{*}End"):
            role, type_attr = end.get("Role", ""), end.get("Type", "")
            ends[role] = type_attr.rsplit(".", 1)[-1] if type_attr else ""
        associations[assoc.get("Name", "")] = ends

    out: _NavMap = {}
    for entity in root.iter("{*}EntityType"):
        navs = []
        for nav in entity.iter("{*}NavigationProperty"):
            relationship = nav.get("Relationship", "")
            to_role = nav.get("ToRole", "")
            assoc_name = relationship.rsplit(".", 1)[-1] if relationship else ""
            target = associations.get(assoc_name, {}).get(to_role) or to_role
            if nav.get("Type"):  # v4: "NS.Target" or "Collection(NS.Target)"
                target = nav.get("Type").removeprefix("Collection(").rstrip(")").rsplit(".", 1)[-1]
            navs.append(
                {
                    "name": nav.get("Name", ""),
                    "target": target,
                    "filterable": nav.get(f"{{{_SAP_NS}}}filterable", "true"),
                }
            )
        if navs:
            out[entity.get("Name", "")] = navs
    return out


_FieldMap = dict[str, dict[str, dict[str, str]]]


def _store() -> SystemStore:
    return SystemStore(get_settings().systems_dir)


def _select(system: str) -> str:
    """The successfactors system a tool serves. Raises SystemUnavailable."""
    return _store().select(SF_TYPE, system)


def _system_error(exc: SystemUnavailable | pii_filter.SystemRefused) -> dict[str, Any]:
    return {"error": exc.code, "system": exc.system, "detail": exc.detail}


def _v4(system: str) -> bool:
    """Whether the system speaks OData v4 (its file's odata_version)."""
    try:
        store = _store()
        return store.config(store.select(SF_TYPE, system)).odata_version == "v4"
    except SystemUnavailable:
        return False  # the request itself reports the system's error


def _v4_entity_type(xml: str, entity: str) -> str:
    """The EntityType behind a v4 entity path's entity set, e.g.
    "talent/cdp/Learning.svc/v1/Items" -> "Item" via <EntitySet Name="Items"
    EntityType="NS.Item">; the set name itself if no EntitySet matches; ""
    for a bare service root (the whole service)."""
    name = v4_service_root(entity)[1].split("/", 1)[0].split("(", 1)[0]
    if not name:
        return ""
    for entity_set in _edmx_root(xml).iter("{*}EntitySet"):
        if entity_set.get("Name") == name:
            return entity_set.get("EntityType", "").rsplit(".", 1)[-1] or name
    return name


# Path segments of identifiers (dots only inside one, as in "Learning.svc"),
# ending in $metadata: no query, fragment, escapes, key predicates or dot segments.
_METADATA_PATH = re.compile(r"(?:[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*/)*\$metadata")


async def _fetch_metadata_xml(system: str, entity: str) -> tuple[str, dict[str, Any] | None]:
    """Fetch one instance's raw $metadata (EDMX). entity="" fetches the whole
    service. Returns (xml, error); shared by every metadata consumer below so
    the request shape (path, system) stays in one place. v4 serves
    $metadata per service only, so there `entity` (a service root, optionally
    followed by an entity set) fetches its service root's document."""
    odata, _ = _clients()
    path = f"{entity}/$metadata" if entity else "$metadata"
    if entity and _v4(system):
        path = f"{v4_service_root(entity)[0]}/$metadata"
    # Metadata output is never PII-tokenized, so the path must not be able to
    # reach anything but a $metadata document: an entity like "EmpJob?x=" would
    # turn the request into a data query whose records come back untokenized.
    if not _METADATA_PATH.fullmatch(path):
        return "", {
            "error": "invalid_entity",
            "system": system,
            "detail": f"entity {entity!r} is not an entity set name or service path.",
        }
    r = await odata.request(method="GET", path=path, conn=ODataConnectionConfig(system=system))
    body = str(r["body"])
    if r["status_code"] >= 400:
        return "", {
            "error": "http_error",
            "system": system,
            "status_code": r["status_code"],
            "body": body[:2000],
        }
    return body, None


async def _field_map(system: str, entity: str) -> tuple[_FieldMap, dict[str, Any] | None]:
    """Fetch one instance's $metadata and reduce it. Returns (fields, error)."""
    xml, error = await _fetch_metadata_xml(system, entity)
    if error is not None:
        return {}, error
    try:
        fields = _parse_edmx(xml)
        if entity and _v4(system):
            # The service document holds every entity type; narrow it to the
            # entity set's type, as v2's entity-scoped $metadata does.
            type_ = _v4_entity_type(xml, entity)
            if type_:
                fields = {k: v for k, v in fields.items() if k == type_}
        return fields, None
    except etree.XMLSyntaxError as exc:
        return {}, {
            "error": "parse_error",
            "system": system,
            "detail": str(exc),
        }


# Entity key lookups are cached per (system, entity) for the life of the
# process — $metadata rarely changes and re-fetching it on every paged query
# would double the request count for no benefit.
_key_cache: dict[tuple[str, str], list[str] | None] = {}


async def _entity_key_properties(system: str, entity: str) -> list[str] | None:
    """Best-effort $metadata lookup of an entity's key properties, for
    auto-$orderby. Only an exact EntityType-name match is used — guessing at
    an arbitrary entity in the document would hand back the wrong keys.

    Returns None if the entity (or its key) couldn't be determined at all —
    including on any failure, since this is an optimization and a broken or
    unreachable metadata endpoint must not be allowed to fail the actual
    query. Returns [] if the entity's key is known but unusable for
    $orderby (see _parse_edmx_keys). Both cases are cached.
    """
    cache_key = (system, entity)
    if cache_key in _key_cache:
        return _key_cache[cache_key]
    keys: list[str] | None = None
    try:
        xml, error = await _fetch_metadata_xml(system, entity)
        if error is None:
            type_ = _v4_entity_type(xml, entity) if _v4(system) else entity
            keys = _parse_edmx_keys(xml).get(type_)
    except Exception:
        keys = None
    _key_cache[cache_key] = keys
    return keys


# Navigation properties only appear in the *full* service $metadata (entity-
# scoped $metadata omits NavigationProperty entirely), and on a real tenant
# that document can run to ~11 MB — so it's fetched at most once per
# system (v4: per system and service root) per process. Only the
# parsed nav map is cached, never the raw XML, per the same rationale as
# _key_cache.
_nav_cache: dict[tuple[str, str], tuple[_NavMap, dict[str, Any] | None]] = {}


async def _nav_properties(system: str, entity: str = "") -> tuple[_NavMap, dict[str, Any] | None]:
    """Best-effort {EntityType: [nav, ...]} for the whole service, for
    odata_metadata to enrich its per-entity output with. Never raises: a
    failure (or an unreachable/oversized full $metadata) is returned as a
    warning dict and cached as such, so it can't break odata_metadata's
    existing field output and isn't retried on every call. entity="" is the
    v2 service; a v4 entity path selects its service's document.
    """
    cache_key = (system, v4_service_root(entity)[0] if entity else "")
    if cache_key in _nav_cache:
        return _nav_cache[cache_key]
    navs: _NavMap = {}
    error: dict[str, Any] | None = None
    try:
        xml, error = await _fetch_metadata_xml(system, entity)
        if error is None:
            navs = _parse_edmx_navs(xml)
    except Exception as exc:
        error = {"error": "nav_parse_error", "system": system, "detail": str(exc)}
    result = (navs, error)
    _nav_cache[cache_key] = result
    return result


def _entity_from_path(path: str) -> str:
    """Best-effort entity-set name from an odata_query path, e.g.
    "EmpJob('123')?$select=x" -> "EmpJob"."""
    base = path.split("?", 1)[0].split("(", 1)[0]
    return base.strip("/")


def _query_entity(path: str) -> str:
    """The entity bare property names in an odata_query path refer to: the last
    resource segment. "" (unknown) after navigation from a key predicate."""
    resource = path.split("?", 1)[0]
    if ")/" in resource:
        return ""
    return resource.rsplit("/", 1)[-1].split("(", 1)[0].strip()


def _query_options(path: str, params: dict[str, Any] | None) -> list[tuple[str, str]]:
    """Every query option the request will send, duplicates included."""
    options = parse_qsl(path.partition("?")[2], keep_blank_values=True)
    options += [
        (k, v if isinstance(v, str) else str(v)) for k, v in (params or {}).items() if v is not None
    ]
    return options


def _looks_like_paging_rejection(body: str) -> bool:
    """SF rejects server-side paging on some entities (e.g.
    PerPersonRelationship) with a 400 body such as 'Cursor based pagination
    not supported by PerPersonRelationship'."""
    lowered = body.lower()
    return "not supported" in lowered and ("paging" in lowered or "pagination" in lowered)


async def _retry_extract_dropping(
    odata: ODataClient,
    path: str,
    conn: ODataConnectionConfig,
    query_params: dict[str, Any],
    max_pages: int,
    param: str,
    warning: str,
) -> tuple[dict[str, Any], str | None]:
    """Drop `param` (an auto-added value SF just rejected) and retry
    extract_all once. Returns the new result and, only if the retry itself
    didn't also error, `warning` for the caller to surface — otherwise the
    outer http_error handling will report the real reason instead."""
    query_params.pop(param, None)
    r = await odata.extract_all(path=path, conn=conn, params=query_params, max_pages=max_pages)
    return r, (warning if r["stopped_reason"] not in ("http_error", "parse_error") else None)


def _diff_field_maps(a: _FieldMap, b: _FieldMap) -> dict[str, Any]:
    """Per-entity differences between two field maps, entities in both only."""
    out: dict[str, Any] = {}
    for entity in sorted(set(a) & set(b)):
        fa, fb = a[entity], b[entity]
        changed = {}
        for field in sorted(set(fa) & set(fb)):
            attrs = {
                key: [fa[field].get(key), fb[field].get(key)]
                for key in sorted(set(fa[field]) | set(fb[field]))
                if fa[field].get(key) != fb[field].get(key)
            }
            if attrs:
                changed[field] = attrs
        only_a = sorted(set(fa) - set(fb))
        only_b = sorted(set(fb) - set(fa))
        if only_a or only_b or changed:
            out[entity] = {
                "fields_only_in_a": only_a,
                "fields_only_in_b": only_b,
                "changed": changed,
            }
    return out


def _plugin_statuses() -> dict[str, Any]:
    from successfactors_toolkit import plugin_api  # imports this module; lazy to avoid a cycle

    return plugin_api._statuses()


@mcp.tool()
def list_systems() -> dict[str, Any]:
    """List the systems this server can reach, one per SYSTEMS_DIR/<name>/<name>.json.

    Call this first: pass a successfactors system's "name" as the other tools'
    `system` argument (it may be empty when there is only one). "production"
    is the declared environment and sets "pii_filter_tier"; a system with
    "error" is refused until its file is fixed. "plugins" reports each
    installed plugin and whether it loaded.
    """
    store = _store()
    systems = [_system_entry(store, name) for name in store.names()]
    out: dict[str, Any] = {
        "systems": systems,
        "systems_dir": str(store.base),
        "plugins": _plugin_statuses(),
    }
    warnings = [
        f"{row['name']}: {row['error']}: {row['detail']}" for row in systems if "error" in row
    ]
    if warnings:
        out["warnings"] = warnings
    return out


def _system_entry(store: SystemStore, name: str) -> dict[str, Any]:
    """One list_systems row: the validated file without secrets, or its error."""
    try:
        config = store.config(name)
    except SystemUnavailable as exc:
        info = store.info(name)
        return {
            "name": name,
            "type": info.type,
            "production": info.production,
            "error": exc.code,
            "detail": exc.detail,
        }
    row = {
        "name": name,
        **config.model_dump(exclude={"client_key", "pii_extra_fields"}),
        "pii_filter_tier": pii_filter.tier_for(config),
    }
    if config.type == SF_TYPE:
        try:
            keypair = store.keypair(name)
        except Exception:  # an unparseable cert must not sink the listing
            keypair = None
        if keypair is not None:
            row["cert_expires"] = keypair.certificate.not_after.isoformat()
            row["cert_days_left"] = keypair.certificate.days_until_expiry
    return row


_NAV_INLINE_CAP = 50


@mcp.tool()
async def odata_metadata(system: str = "", entity: str = "") -> dict[str, Any]:
    """Fetch OData $metadata (EDMX) and reduce it to a compact field map.

    entity="" pulls the whole service metadata (large — hundreds of entity
    types); entity="EmpJob" pulls just that entity set. The full
    {entity: {field: attributes}} map is written to a JSON file, and a small
    map is returned inline as well, so two instances can be compared without
    ever loading raw EDMX into the conversation. When entity is given, its
    navigation properties (name, target entity type, filterable) are listed
    too — entity-scoped $metadata doesn't carry them, so this resolves them
    from the full service $metadata instead (fetched once per system per
    process, then cached); a lookup failure is reported as a warning and
    never blocks the field output above. Use the navigation names to scope
    filters into other entities via $filter (see the server's "Scope with
    EmpJob first" guidance and odata_query's docstring) instead of pulling
    whole entity sets and joining locally.

    On a v4 system, entity is a service path: its root for the whole service
    ("talent/cdp/Learning.svc/v1"), or root plus entity set for one
    (".../Learning.svc/v1/Items"); both read that service's $metadata.
    """
    try:
        system = _select(system)
    except SystemUnavailable as exc:
        return _system_error(exc)
    fields, error = await _field_map(system, entity)
    if error is not None:
        return error

    fields_doc = json.dumps(fields, indent=2, sort_keys=True, ensure_ascii=False)
    navigation: list[dict[str, str]] = []
    nav_warning: str | None = None
    if entity:
        v4 = _v4(system)
        nav_map, nav_error = await _nav_properties(system, entity if v4 else "")
        if nav_error is not None:
            nav_warning = (
                f"navigation properties unavailable: {nav_error.get('error', 'unknown error')}"
            )
        # v4 fields are keyed by the entity set's EntityType (see _field_map).
        name = (next(iter(fields)) if len(fields) == 1 else "") if v4 else entity
        navigation = nav_map.get(name, [])

    file_doc = json.dumps(
        {"fields": fields, "navigation": navigation}, indent=2, sort_keys=True, ensure_ascii=False
    )
    out: dict[str, Any] = {
        "entity_count": len(fields),
        "field_count": sum(len(v) for v in fields.values()),
        "file": _write(file_doc, "odata_metadata", system, "json"),
    }
    if len(fields_doc) <= _INLINE_LIMIT:
        out["fields"] = fields
    else:
        out["entities"] = sorted(fields)

    if entity:
        out["navigation"] = navigation[:_NAV_INLINE_CAP]
        if len(navigation) > _NAV_INLINE_CAP:
            out["navigation_note"] = (
                f"{len(navigation)} navigation properties total; showing the first "
                f"{_NAV_INLINE_CAP} inline — see file for the rest."
            )
        if nav_warning:
            out["navigation_warning"] = nav_warning
    return out


@mcp.tool()
async def compare_metadata(system_a: str, system_b: str, entity: str = "") -> dict[str, Any]:
    """Compare the OData configuration of two instances and return the drift.

    entity="EmpJob" compares one entity set; entity="" compares the whole
    service (v4: a service path, as in odata_metadata). The comparison runs
    here, not in the conversation: one instance's EmpJob metadata alone is
    ~40 KB, so diffing two of them in context is both expensive and easy to
    get wrong.

    Returns in_sync plus, per entity, the fields missing on either side and the
    fields whose attributes differ, each as [value_in_a, value_in_b]. The `sap:`
    attributes are the configuration itself — required, visible, upsertable,
    picklist, MaxLength — so a changed picklist or a field that never left the
    dev instance shows up here.
    """
    try:
        system_a = _select(system_a)
    except SystemUnavailable as exc:
        return _system_error(exc)
    try:
        system_b = _select(system_b)
    except SystemUnavailable as exc:
        return _system_error(exc)
    (fields_a, error_a), (fields_b, error_b) = await asyncio.gather(
        _field_map(system_a, entity), _field_map(system_b, entity)
    )
    if error_a is not None or error_b is not None:
        return {"error": "fetch_failed", "a": error_a, "b": error_b}

    differences = _diff_field_maps(fields_a, fields_b)
    entities_only_in_a = sorted(set(fields_a) - set(fields_b))
    entities_only_in_b = sorted(set(fields_b) - set(fields_a))
    result: dict[str, Any] = {
        "entity": entity or "(whole service)",
        "system_a": system_a,
        "system_b": system_b,
        "in_sync": not (differences or entities_only_in_a or entities_only_in_b),
        "summary": {
            "entities_compared": len(set(fields_a) & set(fields_b)),
            "entities_only_in_a": len(entities_only_in_a),
            "entities_only_in_b": len(entities_only_in_b),
            "entities_with_differences": len(differences),
            "fields_only_in_a": sum(len(d["fields_only_in_a"]) for d in differences.values()),
            "fields_only_in_b": sum(len(d["fields_only_in_b"]) for d in differences.values()),
            "fields_changed": sum(len(d["changed"]) for d in differences.values()),
        },
        "entities_only_in_a": entities_only_in_a,
        "entities_only_in_b": entities_only_in_b,
        "differences": differences,
    }
    doc = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False)
    result["file"] = _write(doc, "compare_metadata", f"{system_a}_vs_{system_b}", "json")
    if len(doc) > _INLINE_LIMIT:
        # Two instances far enough apart can out-grow the context this tool
        # exists to save. Keep the summary, point at the file for the detail.
        result["differences"] = {
            "truncated": True,
            "entities": sorted(differences),
            "note": "Full diff is in the file; ask for one entity to see it inline.",
        }
    return result


@mcp.tool()
async def odata_query(
    path: str,
    system: str = "",
    params: dict[str, Any] | None = None,
    max_pages: int = 10,
    preview: Annotated[int, Field(ge=0, le=20, description="Inline records; maximum 20.")] = 0,
) -> dict[str, Any]:
    """Run an OData query, following next links until exhausted or max_pages.

    path is the entity set and may carry query options, e.g. "FOCompany" or
    "EmpJob?$select=userId,jobCode" (v4 system: service root first,
    "talent/cdp/Learning.svc/v1/Items") — those are parsed out of path and merged
    into the request; pass options either way, but prefer `params` (params win
    on conflicts). Always $select only the fields you need. Effective-dated
    entities (EmpJob, Position, FO*, MDF) return ONLY today's time slice unless
    you pass fromDate=1900-01-01 and toDate=9999-12-31 (or asOfDate) in params.
    Scope with a population filter pushed through navigation in $filter
    (e.g. `personNav/employmentNav/jobInfoNav/company in (...)`) rather than
    pulling whole entity sets — see the server instructions.

    When max_pages > 1 and no $orderby is given, one is added from the
    entity's key properties (reported as `orderby_added`) so $skip paging
    can't duplicate or skip rows; if keys are unknown or unsortable, or SF
    rejects it, the query runs without and a warning says so — then pass
    $orderby yourself. Same condition, without $top/$skip, also adds
    paging=snapshot on v2 (reported as `paging_added`); if SF rejects it for
    that entity, the retry drops it and warns. Rows are checked for
    duplicate keys when every key field is in the records (`duplicate_records`
    + warning if found). A final page exactly $top-sized with no next link gets
    a truncation warning (some MDF entities stop early); resume with $skip
    and an explicit $orderby.

    Records are written to a JSON file; the tool returns counts, the field names
    of the first record, and the path. preview accepts 0-20; values above zero
    return that many records inline only when their serialized UTF-8 size is at
    most 16 KiB. Otherwise, inspect the saved file locally.
    """
    try:
        system = _select(system)
    except SystemUnavailable as exc:
        return _system_error(exc)
    if not 1 <= max_pages <= 1000 or not 0 <= preview <= 20:
        raise ValueError("max_pages must be 1-1000 and preview must be 0-20.")
    raw_path, raw_params = path, params
    entity = _query_entity(path)
    try:
        pii, path, params, pii_subs = _pii_request(path, params, system)
        if pii is not None:
            pii_query_guard.check_query(_query_options(raw_path, raw_params), entity, pii.protects)
    except (PiiVaultError, PiiUnknownTokenError) as exc:
        return _pii_error(exc)
    odata, _ = _clients()
    conn = ODataConnectionConfig(system=system)

    query_params = dict(params or {})
    _, path_params = split_path_query(path)
    merged_view = {**path_params, **query_params}  # what will actually be sent, for peeking
    warnings: list[str] = []
    v4 = _v4(system)
    orderby_added: str | None = None
    paging_added: str | None = None
    keys: list[str] | None = None
    # "Paged pull" = pagination is possible at all; a single-page request can't
    # produce cross-page duplicates/gaps, so there's nothing to order or dedupe.
    paged = max_pages > 1
    if paged:
        keys = await _entity_key_properties(system, _entity_from_path(path))
        if "$orderby" not in merged_view:
            if keys and pii is not None and any(pii.protects(entity, key) for key in keys):
                warnings.append(
                    "No $orderby was given and this entity's key properties hold "
                    "tokenized PII on this system, so the server doesn't sort by them; "
                    "paged results may contain duplicated or skipped rows. Pass "
                    "$orderby on a non-PII field to avoid this."
                )
            elif keys:
                orderby_added = ",".join(keys)
                query_params["$orderby"] = orderby_added
            elif keys == []:
                warnings.append(
                    "No $orderby was given and this entity's key properties are not "
                    "sortable (SuccessFactors rejects $orderby on them); paged results "
                    "may contain duplicated or skipped rows. Pass $orderby explicitly "
                    "to avoid this."
                )
            else:
                warnings.append(
                    "No $orderby was given and this entity's key properties could not "
                    "be determined from $metadata; paged results may contain duplicated "
                    "or skipped rows. Pass $orderby explicitly to avoid this."
                )
        # SF rejects paging=snapshot together with $top or $skip. It is a v2
        # option: SF documents no such v4 query option, and v4 pages by
        # @odata.nextLink anyway.
        if not v4 and "paging" not in merged_view and not {"$top", "$skip"} & merged_view.keys():
            paging_added = "snapshot"
            query_params["paging"] = paging_added

    r = await odata.extract_all(path=path, conn=conn, params=query_params, max_pages=max_pages)

    if r["stopped_reason"] == "http_error" and paging_added:
        # Server-side paging isn't supported on every entity (e.g.
        # PerPersonRelationship); only retry without it when SF's error body
        # actually says so, rather than blindly assuming it's the cause.
        detail = await odata.request("GET", path, conn=conn, params=query_params)
        if _looks_like_paging_rejection(str(detail["body"])):
            rejected_paging = paging_added
            paging_added = None
            r, warning = await _retry_extract_dropping(
                odata,
                path,
                conn,
                query_params,
                max_pages,
                "paging",
                f"Auto-added paging={rejected_paging} was rejected by SuccessFactors "
                "for this entity; retried without it, so multi-page results use "
                "client-side $skip paging instead. Pass paging explicitly if this "
                "matters.",
            )
            if warning:
                warnings.append(warning)

    if r["stopped_reason"] == "http_error" and orderby_added:
        # A key that looked sortable in $metadata (or stale/incomplete
        # metadata) can still get a 400 from SF. Retry once without forcing
        # an order rather than failing a query that would otherwise work.
        rejected_orderby = orderby_added
        orderby_added = None
        r, warning = await _retry_extract_dropping(
            odata,
            path,
            conn,
            query_params,
            max_pages,
            "$orderby",
            f"Auto-added $orderby={rejected_orderby} was rejected by "
            "SuccessFactors; retried without it, so page ordering (and "
            "duplicate/skip safety) is not guaranteed. Pass a working $orderby "
            "explicitly if this matters.",
        )
        if warning:
            warnings.append(warning)

    if r["stopped_reason"] in ("http_error", "parse_error"):
        # extract_all keeps the status but drops the response body, and SF puts
        # the actual reason (unknown entity, missing permission, bad $filter)
        # in that body. One extra call on the failure path buys a usable error.
        detail = await odata.request("GET", path, conn=conn, params=query_params)
        try:
            error_body = _pii_safe_error_body(
                str(detail["body"]), int(detail["status_code"]), pii, pii_subs, fail_closed=True
            )
        except PiiVaultError as exc:
            return _pii_error(exc)
        return {
            "error": r["stopped_reason"],
            "status_code": r["last_status_code"],
            "records_before_error": r["total_records"],
            # Cut after retokenizing/tokenizing, so no plaintext fragment survives it.
            "body": error_body[:2000] if error_body is not None else None,
        }

    results = r["results"]
    duplicate_records = 0
    # Only count duplicates when every key property actually made it into the
    # records — an incomplete $select (e.g. omitting a composite key part)
    # makes distinct rows collide on a missing/None field. No full-record
    # fallback either: two rows can legitimately share every *selected*
    # non-key field.
    if paged and keys and results and all(k in results[0] for k in keys):
        seen: set[tuple[Any, ...]] = set()
        for record in results:
            identity = tuple(record.get(k) for k in keys)
            if identity in seen:
                duplicate_records += 1
            else:
                seen.add(identity)

    top = merged_view.get("$top")
    if r["stopped_reason"] == "exhausted" and top is not None and r.get("last_page_size"):
        try:
            top_n = int(top)
        except (TypeError, ValueError):
            top_n = None
        if top_n and r["last_page_size"] == top_n:
            warnings.append(
                f"The last page returned exactly $top={top_n} rows with no next link "
                "(reported 'exhausted'). Some MDF/custom entities silently stop paging "
                "before all data is returned — if the row count looks short, resume "
                "manually with $skip (and an explicit $orderby) past this point."
            )

    pii_count = 0
    next_skiptoken = r["next_skiptoken"]
    if pii is not None:
        try:
            results, pii_count = pii.tokenize_records(
                results, v4=v4, entity=entity, fail_closed=True
            )
            # The server may build it from the last row's key; passed back in
            # $skiptoken, the token resolves like any other.
            if next_skiptoken:
                next_skiptoken = pii.tokenize_value(next_skiptoken)
        except PiiVaultError as exc:
            return _pii_error(exc)

    out: dict[str, Any] = {
        "total_records": r["total_records"],
        "pages_fetched": r["pages_fetched"],
        "stopped_reason": r["stopped_reason"],
        "next_skiptoken": next_skiptoken,
        "fields": sorted(results[0]) if results else [],
        "file": _write(
            json.dumps(results, indent=2, ensure_ascii=False, default=str),
            "odata_query",
            system,
            "json",
        ),
    }
    if orderby_added:
        out["orderby_added"] = orderby_added
    if paging_added:
        out["paging_added"] = paging_added
    if duplicate_records > 0:
        out["duplicate_records"] = duplicate_records
        warnings.append(
            f"{duplicate_records} duplicate record(s) detected across pages — see "
            "duplicate_records. Re-run with an explicit $orderby on a fully unique key "
            "if this persists."
        )
    if warnings:
        out["warnings"] = warnings
    _pii_mark(out, pii, pii_count)
    _attach_preview(out, results, preview)
    return out


@mcp.tool()
async def ce_query(
    system: str = "",
    person_id_external: str = "",
    user_id: str = "",
    last_modified_on: str = "",
    include_contingent_workers: bool = False,
    select_segments: list[str] | None = None,
    max_rows: int | None = None,
    max_pages: int = 5,
) -> dict[str, Any]:
    """Query the EC Compound Employee (SOAP) API and save the payload to disk.

    person_id_external / user_id are comma-separated and take precedence over
    every other filter when set. last_modified_on is an ISO datetime for a delta
    pull (SAP allows at most 3 months of look-back). With no filter at all this
    is a full extract, capped by max_pages.

    CompoundEmployee SFQL allows only ONE condition on last_modified_on in the
    WHERE clause — a query with both a lower and an upper bound
    (last_modified_on>X and last_modified_on<=Y) fails with INVALID_SFQL: Only
    one condition is allowed. This tool only ever emits the lower bound; apply
    any upper bound to the returned rows client-side, not by adding a second
    last_modified_on condition.

    last_modified_on's filtering also depends on an SFQL parameter,
    isNotFirstQuery, that this tool does not currently set: per tenant testing,
    a query without isNotFirstQuery ignores last_modified_on entirely and
    returns a full snapshot of the matching window, while isNotFirstQuery=true
    makes last_modified_on act as a real delta filter. Until this tool exposes
    that parameter, treat last_modified_on here as a full-window snapshot, not
    a guaranteed delta — don't rely on it alone to mean "only changed rows".

    select_segments defaults to the widely supported COMMON_SEGMENTS. If SF
    answers INVALID_SFQL naming a segment, that module is not enabled on the
    tenant — pass a narrower list. Only documented segment names are accepted,
    and person_id_external / user_id values may contain only letters, digits,
    space and _ . @ : / + -.

    Each queryMore page is written as its own XML file. The tool returns counts
    and paths only: one employee's payload is ~80 KB of HR data.
    """
    try:
        system = _select(system)
    except SystemUnavailable as exc:
        return _system_error(exc)
    if not 1 <= max_pages <= 500:
        raise ValueError("max_pages must be 1-500.")
    try:
        pii = pii_filter.for_system(get_settings(), system)
    except PiiVaultError as exc:
        return _pii_error(exc)
    pii_count = 0
    _, sfapi = _clients()
    conn = SFAPIConnectionConfig(system=system)
    query = build_query_string(
        CEQueryFilter(
            person_id_external=person_id_external,
            user_id=user_id,
            last_modified_on=last_modified_on,
            include_contingent_workers=include_contingent_workers,
            select_segments=select_segments or list(COMMON_SEGMENTS),
            max_rows=max_rows,
        )
    )
    params = [("maxRows", str(max_rows))] if max_rows else None

    files: list[str] = []
    pages = 0
    total = 0
    has_more: bool | None = None
    budget = ExtractBudget(get_settings())
    result = await sfapi.query(query, conn=conn, params=params)

    while True:
        body = str(result["body"])
        num_results, has_more, session, error = parse_page(body, int(result["status_code"]))
        if error:
            error_body: str | None = body[:2000]
            if pii is not None:
                # A soap_fault is a short fault string, but missing_query_session
                # and parse_error can both be a full, valid, otherwise-normal
                # employee page (e.g. missing_query_session is just SF's
                # continuation token going missing) — tokenize it like any
                # other page before it's shown, and withhold it entirely if
                # it isn't even valid XML.
                try:
                    tokenized_body, _ = pii.tokenize_xml(body)
                except etree.XMLSyntaxError:
                    # A >=400 non-XML body is an auth/gateway page: the request
                    # carried only IDs, so there is nothing of ours to echo.
                    if int(result["status_code"]) < 400:
                        error_body = None
                except PiiVaultError as exc:
                    return {**_pii_error(exc), "files": files}
                else:
                    error_body = tokenized_body[:2000]
            return {
                "error": error,
                "status_code": result["status_code"],
                "failed_on_page": pages + 1,
                "body": error_body,
                "files": files,
            }
        pages += 1
        total += num_results or 0
        if pii is not None:
            try:
                body, tokenized = pii.tokenize_xml(body)
            except etree.XMLSyntaxError:
                return {"error": "pii_tokenize_failed", "failed_on_page": pages, "files": files}
            except PiiVaultError as exc:
                return {**_pii_error(exc), "files": files}
            pii_count += tokenized
        files.append(_write(body, f"ce_query_p{pages}", system, "xml"))
        if not (has_more and session and pages < max_pages):
            break
        budget.check_time()
        result = await sfapi.query_more(session, conn=conn)

    out = {
        "page_count": pages,
        "total_records": total,
        "truncated": bool(has_more) and pages >= max_pages,
        "query": query,
        "files": files,
    }
    _pii_mark(out, pii, pii_count)
    return out


def main() -> None:
    """Console-script entry point: serve MCP over stdio."""
    get_settings()  # bad PII (or other) settings stop the server here
    # httpx logs every request URL at INFO; with tokens resolved, that URL can
    # hold plaintext PII. Whatever a system's pii_filter_tier says, production
    # always tokenizes.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from successfactors_toolkit import plugin_api

    plugin_api._load_plugins(mcp)
    mcp.run()


if __name__ == "__main__":
    # Serve from the importable module, not this __main__ copy: plugins reach
    # the server through plugin_api, which imports successfactors_toolkit.mcp_server.
    from successfactors_toolkit.mcp_server import main as _main

    _main()
