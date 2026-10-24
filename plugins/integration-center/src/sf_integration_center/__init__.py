"""Read-only SAP SuccessFactors Integration Center plugin for the MCP server."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import Field

from successfactors_toolkit.plugin_api import (
    ConnectionPolicyError,
    SystemUnavailable,
    attach_preview,
    odata_get,
    select_system,
    set_status,
    system_error,
    write_result,
)

# Keep equal to `version` in the plugin's pyproject.toml (a test checks it).
__version__ = "0.1.0"

# The one path this plugin requests: SAP's internal, unsupported IC service.
SERVICE = "restricted/_IntegrationCenterRestrictedService"
_DEFINITIONS = f"{SERVICE}/IntegrationProcessDefinition"

# The allowlist: the only properties requested and the only ones read back.
# Everything else (connection tokens, usernames, keys, the key and OAuth
# entities, anything SAP adds later) is neither selected nor copied, on any
# tier. Person references (createdBy, lastModifiedBy) are left out too.
_VERSION = "latestVersionNav"
_OPERATION = f"{_VERSION}/destinationOperation"
_CONNECTION = f"{_OPERATION}/connection"
_FIELDS = {
    "": ("externalCode", "externalName", "isActive"),
    _VERSION: ("triggerType", "lastAttemptedRunTime", "lastSuccessfulRunTime"),
    _OPERATION: ("type", "method", "directory", "fileName"),
    _CONNECTION: ("type", "uri", "port", "directory", "externalName", "dependencyName"),
}
SELECT = ",".join(f"{nav}/{f}" if nav else f for nav, fields in _FIELDS.items() for f in fields)
EXPAND = ",".join(nav for nav in _FIELDS if nav)
_MAX_PAGES = 50

_DATE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")


def _scalar(record: Any, field: str, kind: type = str) -> Any:
    """An allowlisted field's value if it has the field's type, else None: a
    value of another shape (SAP may change one) is dropped, not passed on."""
    value = record.get(field) if isinstance(record, dict) else None
    return value if type(value) is kind else None


def _port(record: Any) -> int | None:
    """Edm.Int64, which v2 JSON may send as a digit string."""
    value = record.get("port") if isinstance(record, dict) else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return value if type(value) is int else None


def _nav(record: Any, name: str) -> list[dict[str, Any]]:
    """An expanded navigation as a list: v2 gives {"results": [...]} for a
    collection, the object for a single entity, {"__deferred": ...} if not expanded."""
    value = record.get(name) if isinstance(record, dict) else None
    if isinstance(value, dict):
        value = value.get("results", [value]) if "__deferred" not in value else []
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _time(value: Any) -> str | None:
    """A v2 JSON DateTimeOffset ("/Date(<ms>+0000)/") as ISO 8601 UTC; None
    for anything else, out-of-range dates included."""
    match = _DATE.fullmatch(value) if isinstance(value, str) else None
    try:
        return datetime.fromtimestamp(int(match[1]) / 1000, UTC).isoformat() if match else None
    except (ValueError, OverflowError, OSError):
        return None


def _endpoint(value: Any) -> str | None:
    """A connection URI as scheme, host, port and path only. Userinfo,
    query and fragment can carry credentials: everything up to the last "@"
    goes first (a password may hold "/", "?" or "#", so the userinfo can't be
    found by parsing), then query and fragment; None if what is left isn't a
    plain URI."""
    if not isinstance(value, str):
        return None
    scheme, sep, rest = value.partition("://") if "://" in value else ("", "", value)
    if sep and not _SCHEME.fullmatch(scheme):
        return None
    try:
        parts = urlsplit("//" + rest.rpartition("@")[2])
        parts.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    if not parts.netloc:
        return None
    return f"{scheme}{sep}{parts.netloc}{parts.path}"


def _downstream(operation: dict[str, Any]) -> dict[str, Any]:
    connections = _nav(operation, "connection")
    connection = connections[0] if connections else {}
    return {
        "operation_type": _scalar(operation, "type"),
        "method": _scalar(operation, "method"),
        "operation_directory": _scalar(operation, "directory"),
        "file_name": _scalar(operation, "fileName"),
        "connection_type": _scalar(connection, "type"),
        "connection_name": _scalar(connection, "externalName"),
        "dependency_name": _scalar(connection, "dependencyName"),
        "endpoint": _endpoint(connection.get("uri")),
        "port": _port(connection),
        "connection_directory": _scalar(connection, "directory"),
    }


def integration_row(definition: dict[str, Any]) -> dict[str, Any]:
    """One integration from an IntegrationProcessDefinition record, built
    only from allowlisted fields."""
    versions = _nav(definition, _VERSION)
    version = versions[0] if versions else {}
    return {
        "id": _scalar(definition, "externalCode"),
        "name": _scalar(definition, "externalName"),
        "active": _scalar(definition, "isActive", bool),
        "trigger_type": _scalar(version, "triggerType"),
        "last_attempted_run": _time(version.get("lastAttemptedRunTime")),
        "last_successful_run": _time(version.get("lastSuccessfulRunTime")),
        "downstream": [_downstream(op) for op in _nav(version, "destinationOperation")],
    }


def ran_within(rows: list[dict[str, Any]], days: int, now: datetime) -> list[dict[str, Any]]:
    """Rows whose last attempted run, successful or not, is in the last `days` days."""
    cutoff = now - timedelta(days=days)
    return [
        r
        for r in rows
        if r["last_attempted_run"] and datetime.fromisoformat(r["last_attempted_run"]) >= cutoff
    ]


async def ic_integrations(
    system: str = "",
    ran_within_days: Annotated[
        int | None,
        Field(ge=1, le=3650, description="Only integrations last attempted in these days."),
    ] = None,
    preview: Annotated[int, Field(ge=0, le=20, description="Inline rows; maximum 20.")] = 0,
) -> dict[str, Any]:
    """List the Integration Center integrations ("My Integrations") of a
    SuccessFactors system, with each one's downstream.

    Per integration: id, name, active, trigger_type, last_attempted_run and
    last_successful_run (from its latest version; ISO 8601 UTC), and
    downstream: the destination operation's type, method, directory and
    file name, and its connection's type, name, endpoint (scheme, host,
    port, path), port and directory. ran_within_days keeps integrations whose
    last attempted run, successful or not, falls in that many days.
    Connection credentials and key material are never returned. Rows are
    written to a JSON file; preview inlines up to 20 when they fit in 16 KiB.

    Reads SAP's internal Integration Center OData service, which SAP does
    not support for customer use and may change; the user needs Integration
    Center access. A REST connection "from Security Center" may have its
    effective URL there rather than on the connection. last_attempted_run is
    the definition's own run time and can differ from the UI's "Last Run".
    """
    try:
        system = select_system("successfactors", system)
        result = await odata_get(
            system,
            _DEFINITIONS,
            {
                "$filter": "definitionType eq 'SAVEDCONTENT'",
                "$select": SELECT,
                "$expand": EXPAND,
                "$orderby": "externalCode",
            },
            max_pages=_MAX_PAGES,
        )
    except SystemUnavailable as exc:
        return system_error(exc)
    except ConnectionPolicyError as exc:
        return {"error": "connection_policy", "detail": str(exc)}
    if result["stopped_reason"] in ("http_error", "parse_error"):
        # Never SF's body: an error can echo the request or record content.
        return {"error": result["stopped_reason"], "status_code": result["last_status_code"]}

    rows = [integration_row(r) for r in result["results"] if isinstance(r, dict)]
    if ran_within_days is not None:
        rows = ran_within(rows, ran_within_days, datetime.now(UTC))
    out: dict[str, Any] = {
        "total_integrations": len(rows),
        "file": write_result(
            json.dumps(rows, indent=2, ensure_ascii=False), "ic_integrations", system, "json"
        ),
    }
    if result["stopped_reason"] == "max_pages":
        out["warning"] = f"Stopped after {_MAX_PAGES} pages; the list is incomplete."
    attach_preview(out, rows, preview)
    return out


def register(mcp) -> None:
    mcp.tool()(ic_integrations)
    set_status("integration_center", lambda: {"version": __version__})
