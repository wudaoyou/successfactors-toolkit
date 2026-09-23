"""MCP (stdio) front end for this repo's SuccessFactors clients.

Exposes five tools — tenant listing, OData $metadata, cross-instance metadata
comparison, OData query, and Compound Employee query — to an MCP host such as
Claude Desktop or Claude Code.

Everything hard is reused from ``successfactors_toolkit.services``: OAuth2 SAML
Bearer, per-tenant keys, ``queryMore`` paging, ``__next`` following. This module
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
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from lxml import etree
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from successfactors_toolkit.config import get_settings
from successfactors_toolkit.models.common import ODataConnectionConfig, SFAPIConnectionConfig
from successfactors_toolkit.models.sfapi import CEQueryFilter
from successfactors_toolkit.services.ce_query_builder import COMMON_SEGMENTS, build_query_string
from successfactors_toolkit.services.ce_response import parse_page
from successfactors_toolkit.services.odata_client import ODataClient, split_path_query
from successfactors_toolkit.services.sfapi_client import SFAPIClient
from successfactors_toolkit.services.tenant_store import TenantStore

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
        "SAP SuccessFactors: OData v2 (any entity set) and the EC Compound "
        "Employee SOAP API. Call list_tenants first for company_id. Diff two "
        "instances with compare_metadata (server-side). Large results go to a "
        "file (container path; under Docker it's the host dir bound to "
        "RESULTS_DIR) — read it for the records.\n\n"
        "How to query (in order):\n"
        "1. Decide the population filter first, usually on EmpJob (company, "
        "location, department, emplStatus). Put employment status in it "
        "explicitly; never rely on a navigation path to drop terminated "
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
        "PicklistOption with `id in (...)` — no N+1 calls.\n\n"
        "Also:\n"
        "- Pass options in odata_query `params`; $select only needed fields.\n"
        "- Effective-dated entities (EmpJob, Position, FO*, MDF) return "
        "today's slice unless you pass fromDate/toDate or asOfDate.\n"
        "- Join keys: userId (EmpJob, EmpEmployment, BenefitEnrollment "
        "workerId); personIdExternal (Per*); EmpEmployment bridges both. "
        "PerPersonRelationship maps personIdExternal to a dependent's "
        "relatedPersonIdExternal via relationshipType (picklist); "
        "$expand=relNationalIdNav returns dependents' IDs in the same call.\n"
        "- To check whether a national ID exists, select only cardType/country "
        "— never nationalId values unless the user asks for them."
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


def _write(content: str, tool: str, company_id: str, suffix: str) -> str:
    """Write a payload under ``{results_dir}/mcp/`` and return its path."""
    # Resolved per call, not at import: the setting is only known once the
    # environment and .env have been read, and tests monkeypatch it.
    out_dir = get_settings().results_dir / "mcp"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Microseconds keep two calls in the same second from overwriting each other.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    name = _SAFE_NAME.sub("_", company_id) or "default"
    path = out_dir / f"{tool}_{name}_{stamp}.{suffix}"
    # O_EXCL plus mode 0o600: HR payloads are never world-readable, and an
    # existing name is an error rather than an overwrite.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
    return str(path)


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
    (SAP's own default).
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


async def _fetch_metadata_xml(company_id: str, entity: str) -> tuple[str, dict[str, Any] | None]:
    """Fetch one instance's raw $metadata (EDMX). entity="" fetches the whole
    service. Returns (xml, error); shared by every metadata consumer below so
    the request shape (path, company override) stays in one place."""
    odata, _ = _clients()
    path = f"{entity}/$metadata" if entity else "$metadata"
    r = await odata.request(
        method="GET", path=path, conn=ODataConnectionConfig(company_id=company_id or None)
    )
    body = str(r["body"])
    if r["status_code"] >= 400:
        return "", {
            "error": "http_error",
            "company_id": company_id,
            "status_code": r["status_code"],
            "body": body[:2000],
        }
    return body, None


async def _field_map(company_id: str, entity: str) -> tuple[_FieldMap, dict[str, Any] | None]:
    """Fetch one instance's $metadata and reduce it. Returns (fields, error)."""
    xml, error = await _fetch_metadata_xml(company_id, entity)
    if error is not None:
        return {}, error
    try:
        return _parse_edmx(xml), None
    except etree.XMLSyntaxError as exc:
        return {}, {
            "error": "parse_error",
            "company_id": company_id,
            "detail": str(exc),
            "body": xml[:2000],
        }


# Entity key lookups are cached per (company_id, entity) for the life of the
# process — $metadata rarely changes and re-fetching it on every paged query
# would double the request count for no benefit.
_key_cache: dict[tuple[str, str], list[str] | None] = {}


async def _entity_key_properties(company_id: str, entity: str) -> list[str] | None:
    """Best-effort $metadata lookup of an entity's key properties, for
    auto-$orderby. Only an exact EntityType-name match is used — guessing at
    an arbitrary entity in the document would hand back the wrong keys.

    Returns None if the entity (or its key) couldn't be determined at all —
    including on any failure, since this is an optimization and a broken or
    unreachable metadata endpoint must not be allowed to fail the actual
    query. Returns [] if the entity's key is known but unusable for
    $orderby (see _parse_edmx_keys). Both cases are cached.
    """
    cache_key = (company_id, entity)
    if cache_key in _key_cache:
        return _key_cache[cache_key]
    keys: list[str] | None = None
    try:
        xml, error = await _fetch_metadata_xml(company_id, entity)
        if error is None:
            keys = _parse_edmx_keys(xml).get(entity)
    except Exception:
        keys = None
    _key_cache[cache_key] = keys
    return keys


# Navigation properties only appear in the *full* service $metadata (entity-
# scoped $metadata omits NavigationProperty entirely), and on a real tenant
# that document can run to ~11 MB — so it's fetched at most once per
# company_id per process. Only the parsed nav map is cached, never the raw
# XML, per the same rationale as _key_cache.
_nav_cache: dict[str, tuple[_NavMap, dict[str, Any] | None]] = {}


async def _nav_properties(company_id: str) -> tuple[_NavMap, dict[str, Any] | None]:
    """Best-effort {EntityType: [nav, ...]} for the whole service, for
    odata_metadata to enrich its per-entity output with. Never raises: a
    failure (or an unreachable/oversized full $metadata) is returned as a
    warning dict and cached as such, so it can't break odata_metadata's
    existing field output and isn't retried on every call.
    """
    if company_id in _nav_cache:
        return _nav_cache[company_id]
    navs: _NavMap = {}
    error: dict[str, Any] | None = None
    try:
        xml, error = await _fetch_metadata_xml(company_id, "")
        if error is None:
            navs = _parse_edmx_navs(xml)
    except Exception as exc:
        error = {"error": "nav_parse_error", "company_id": company_id, "detail": str(exc)}
    result = (navs, error)
    _nav_cache[company_id] = result
    return result


def _entity_from_path(path: str) -> str:
    """Best-effort entity-set name from an odata_query path, e.g.
    "EmpJob('123')?$select=x" -> "EmpJob"."""
    base = path.split("?", 1)[0].split("(", 1)[0]
    return base.strip("/")


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


@mcp.tool()
def list_tenants() -> dict[str, Any]:
    """List the SuccessFactors instances this server can reach.

    Call this first: the company_id values it returns are what the other tools
    take as their `company_id` argument. An empty company_id always means the
    instance configured in the server's own .env, reported here as "default".
    """
    settings = get_settings()
    tenants = [
        {
            "company_id": t.company_id,
            "host": settings.sf_host,
            "odata_version": settings.sf_odata_version,
            "technical_user": settings.sf_user_id,
            "cert_expires": t.certificate.not_after.isoformat(),
            "cert_days_left": t.certificate.days_until_expiry,
        }
        for t in TenantStore(settings.tenant_keys_dir).list_tenants()
    ]
    return {
        "tenants": tenants,
        "default": {
            "company_id": settings.sf_company_id,
            "host": settings.sf_host,
            "odata_version": settings.sf_odata_version,
        },
        "keys_dir": settings.tenant_keys_dir,
    }


_NAV_INLINE_CAP = 50


@mcp.tool()
async def odata_metadata(company_id: str = "", entity: str = "") -> dict[str, Any]:
    """Fetch OData $metadata (EDMX) and reduce it to a compact field map.

    entity="" pulls the whole service metadata (large — hundreds of entity
    types); entity="EmpJob" pulls just that entity set. The full
    {entity: {field: attributes}} map is written to a JSON file, and a small
    map is returned inline as well, so two instances can be compared without
    ever loading raw EDMX into the conversation. When entity is given, its
    navigation properties (name, target entity type, filterable) are listed
    too — entity-scoped $metadata doesn't carry them, so this resolves them
    from the full service $metadata instead (fetched once per company_id per
    process, then cached); a lookup failure is reported as a warning and
    never blocks the field output above. Use the navigation names to scope
    filters into other entities via $filter (see the server's "Scope with
    EmpJob first" guidance and odata_query's docstring) instead of pulling
    whole entity sets and joining locally.
    """
    fields, error = await _field_map(company_id, entity)
    if error is not None:
        return error

    fields_doc = json.dumps(fields, indent=2, sort_keys=True, ensure_ascii=False)
    navigation: list[dict[str, str]] = []
    nav_warning: str | None = None
    if entity:
        nav_map, nav_error = await _nav_properties(company_id)
        if nav_error is not None:
            nav_warning = (
                f"navigation properties unavailable: {nav_error.get('error', 'unknown error')}"
            )
        navigation = nav_map.get(entity, [])

    file_doc = json.dumps(
        {"fields": fields, "navigation": navigation}, indent=2, sort_keys=True, ensure_ascii=False
    )
    out: dict[str, Any] = {
        "entity_count": len(fields),
        "field_count": sum(len(v) for v in fields.values()),
        "file": _write(file_doc, "odata_metadata", company_id, "json"),
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
async def compare_metadata(company_a: str, company_b: str, entity: str = "") -> dict[str, Any]:
    """Compare the OData configuration of two instances and return the drift.

    entity="EmpJob" compares one entity set; entity="" compares the whole
    service. The comparison runs here, not in the conversation: one instance's
    EmpJob metadata alone is ~40 KB, so diffing two of them in context is both
    expensive and easy to get wrong.

    Returns in_sync plus, per entity, the fields missing on either side and the
    fields whose attributes differ, each as [value_in_a, value_in_b]. The `sap:`
    attributes are the configuration itself — required, visible, upsertable,
    picklist, MaxLength — so a changed picklist or a field that never left the
    dev instance shows up here.
    """
    (fields_a, error_a), (fields_b, error_b) = await asyncio.gather(
        _field_map(company_a, entity), _field_map(company_b, entity)
    )
    if error_a is not None or error_b is not None:
        return {"error": "fetch_failed", "a": error_a, "b": error_b}

    differences = _diff_field_maps(fields_a, fields_b)
    entities_only_in_a = sorted(set(fields_a) - set(fields_b))
    entities_only_in_b = sorted(set(fields_b) - set(fields_a))
    result: dict[str, Any] = {
        "entity": entity or "(whole service)",
        "company_a": company_a,
        "company_b": company_b,
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
    result["file"] = _write(doc, "compare_metadata", f"{company_a}_vs_{company_b}", "json")
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
    company_id: str = "",
    params: dict[str, Any] | None = None,
    max_pages: int = 10,
    preview: Annotated[int, Field(ge=0, le=20, description="Inline records; maximum 20.")] = 0,
) -> dict[str, Any]:
    """Run an OData v2 query, following __next until exhausted or max_pages.

    path is the entity set and may carry query options, e.g. "FOCompany" or
    "EmpJob?$select=userId,jobCode" — those are parsed out of path and merged
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
    $orderby yourself. Rows are checked for duplicate keys when every key
    field is in the records (`duplicate_records` + warning if found). A final
    page exactly $top-sized with no __next gets a truncation warning (some
    MDF entities stop early); resume with $skip and an explicit $orderby.

    Records are written to a JSON file; the tool returns counts, the field names
    of the first record, and the path. preview accepts 0-20; values above zero
    return that many records inline only when their serialized UTF-8 size is at
    most 16 KiB. Otherwise, inspect the saved file locally.
    """
    if not 1 <= max_pages <= 10000 or not 0 <= preview <= 20:
        raise ValueError("max_pages must be 1-10000 and preview must be 0-20.")
    odata, _ = _clients()
    conn = ODataConnectionConfig(company_id=company_id or None)

    query_params = dict(params or {})
    _, path_params = split_path_query(path)
    merged_view = {**path_params, **query_params}  # what will actually be sent, for peeking
    warnings: list[str] = []
    orderby_added: str | None = None
    keys: list[str] | None = None
    # "Paged pull" = pagination is possible at all; a single-page request can't
    # produce cross-page duplicates/gaps, so there's nothing to order or dedupe.
    paged = max_pages > 1
    if paged:
        keys = await _entity_key_properties(company_id, _entity_from_path(path))
        if "$orderby" not in merged_view:
            if keys:
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

    r = await odata.extract_all(path=path, conn=conn, params=query_params, max_pages=max_pages)
    if r["stopped_reason"] == "http_error" and orderby_added:
        # A key that looked sortable in $metadata (or stale/incomplete
        # metadata) can still get a 400 from SF. Retry once without forcing
        # an order rather than failing a query that would otherwise work.
        rejected_orderby = orderby_added
        query_params.pop("$orderby", None)
        orderby_added = None
        r = await odata.extract_all(path=path, conn=conn, params=query_params, max_pages=max_pages)
        if r["stopped_reason"] not in ("http_error", "parse_error"):
            warnings.append(
                f"Auto-added $orderby={rejected_orderby} was rejected by "
                "SuccessFactors; retried without it, so page ordering (and "
                "duplicate/skip safety) is not guaranteed. Pass a working $orderby "
                "explicitly if this matters."
            )

    if r["stopped_reason"] in ("http_error", "parse_error"):
        # extract_all keeps the status but drops the response body, and SF puts
        # the actual reason (unknown entity, missing permission, bad $filter)
        # in that body. One extra call on the failure path buys a usable error.
        detail = await odata.request("GET", path, conn=conn, params=query_params)
        return {
            "error": r["stopped_reason"],
            "status_code": r["last_status_code"],
            "records_before_error": r["total_records"],
            "body": str(detail["body"])[:2000],
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
                f"The last page returned exactly $top={top_n} rows with no __next link "
                "(reported 'exhausted'). Some MDF/custom entities silently stop paging "
                "before all data is returned — if the row count looks short, resume "
                "manually with $skip (and an explicit $orderby) past this point."
            )

    out: dict[str, Any] = {
        "total_records": r["total_records"],
        "pages_fetched": r["pages_fetched"],
        "stopped_reason": r["stopped_reason"],
        "next_skiptoken": r["next_skiptoken"],
        "fields": sorted(results[0]) if results else [],
        "file": _write(
            json.dumps(results, indent=2, ensure_ascii=False, default=str),
            "odata_query",
            company_id,
            "json",
        ),
    }
    if orderby_added:
        out["orderby_added"] = orderby_added
    if duplicate_records > 0:
        out["duplicate_records"] = duplicate_records
        warnings.append(
            f"{duplicate_records} duplicate record(s) detected across pages — see "
            "duplicate_records. Re-run with an explicit $orderby on a fully unique key "
            "if this persists."
        )
    if warnings:
        out["warnings"] = warnings
    if preview > 0:
        preview_records = results[:preview]
        if (
            len(json.dumps(preview_records, ensure_ascii=False, default=str).encode("utf-8"))
            <= _PREVIEW_INLINE_LIMIT
        ):
            out["preview"] = preview_records
        else:
            out["preview_error"] = (
                "Requested preview exceeds the 16 KiB inline limit; inspect the saved file locally."
            )
    return out


@mcp.tool()
async def ce_query(
    company_id: str = "",
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
    tenant — pass a narrower list.

    Each queryMore page is written as its own XML file. The tool returns counts
    and paths only: one employee's payload is ~80 KB of HR data.
    """
    if not 1 <= max_pages <= 500:
        raise ValueError("max_pages must be 1-500.")
    _, sfapi = _clients()
    conn = SFAPIConnectionConfig(company_id=company_id or None)
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
    result = await sfapi.query(query, conn=conn, params=params)

    while True:
        body = str(result["body"])
        num_results, has_more, session, error = parse_page(body, int(result["status_code"]))
        if error:
            return {
                "error": error,
                "status_code": result["status_code"],
                "failed_on_page": pages + 1,
                # Faults are short and name the offending segment/filter — the
                # one payload the model must actually see.
                "body": body[:2000],
                "files": files,
            }
        pages += 1
        total += num_results or 0
        files.append(_write(body, f"ce_query_p{pages}", company_id, "xml"))
        if not (has_more and session and pages < max_pages):
            break
        result = await sfapi.query_more(session, conn=conn)

    return {
        "page_count": pages,
        "total_records": total,
        "truncated": bool(has_more) and pages >= max_pages,
        "query": query,
        "files": files,
    }


def main() -> None:
    """Console-script entry point: serve MCP over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
