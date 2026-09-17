"""SFQL query builder for SAP SuccessFactors CompoundEmployee API.

CompoundEmployee does not support `SELECT *` — per the SAP docs the API
requires explicit enumeration of segments. Within each segment all fields
are returned (field-level restriction is not supported).

DEFAULT_SEGMENTS lists every segment documented as supported by both the
Effective-Dated Delta and Period-Based Delta modes. Some entries
(`BenefitsIntegration*`, `EmpCostAssignment`, `ItDeclaration`, etc.) require
the corresponding SF module to be enabled in the tenant; if they are not,
SF returns an INVALID_SFQL error naming the offending item.

Callers that hit such errors should override `select_segments` with the
narrower set they need.

Reference: SAP Employee Central Compound Employee API guide, sections
"SELECT Items" and "Select Parameters for the WHERE Clause".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from successfactors_toolkit.models.sfapi import CEQueryFilter


# Full list per the SAP Compound Employee API guide (2H 2025).
# Listed in the same hierarchical order SAP documents them.
DEFAULT_SEGMENTS: tuple[str, ...] = (
    "person",
    "personal_information",
    "address_information",
    "email_information",
    "phone_information",
    "person_relation",
    "national_id_card",
    "dependent_information",
    "emergency_contact_primary",
    "personal_documents_information",
    "employment_information",
    "global_assignment_information",
    "job_information",
    "job_relation",
    "alternative_cost_distribution",
    "compensation_information",
    "paycompensation_recurring",
    "paycompensation_non_recurring",
    "payment_information",
    "direct_deposit",
    "deduction_recurring",
    "deduction_non_recurring",
)

# Smaller set known to work on most tenants without module-specific
# entitlements — used by query-by-person-id / query-by-user-id when no
# explicit override is given, to maximise success rate for the common
# Postman lookup case.
COMMON_SEGMENTS: tuple[str, ...] = (
    "person",
    "personal_information",
    "address_information",
    "email_information",
    "phone_information",
    "national_id_card",
    "employment_information",
    "job_information",
    "job_relation",
    "compensation_information",
    "paycompensation_recurring",
)


def _in_clause(column: str, raw_value: str) -> str:
    """Build: column in('A','B','C') from a comma-separated raw_value."""
    vals = "','".join(v.strip() for v in raw_value.split(","))
    return f"{column} in('{vals}')"


def _normalize_datetime(dt: str) -> str:
    """Normalize an ISO timestamp to UTC without discarding its offset."""
    try:
        parsed = datetime.fromisoformat(dt)
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        raise ValueError("last_modified_on must be an ISO timestamp with a timezone.") from None
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_select_clause(segments: list[str] | tuple[str, ...] | None) -> str:
    """Build `SELECT a, b, c FROM CompoundEmployee` from an optional segment list.

    Empty/None falls back to DEFAULT_SEGMENTS.
    """
    seg = segments or DEFAULT_SEGMENTS
    return f"SELECT {', '.join(s.strip() for s in seg if s.strip())} FROM CompoundEmployee"


def build_where_clause(f: CEQueryFilter) -> str:
    """Return a WHERE clause string without the WHERE keyword.

    Identity filters take precedence over date, org, and job filters. The
    contingent-worker flag is the one exception and may be combined with an
    identity filter.
    """
    cw_filter = "isContingentWorker in('true','false')" if f.include_contingent_workers else ""

    if f.person_id_external.strip():
        base = _in_clause("person_id_external", f.person_id_external)
        return f"{base} and {cw_filter}" if cw_filter else base

    if f.user_id.strip():
        base = _in_clause("user_id", f.user_id)
        return f"{base} and {cw_filter}" if cw_filter else base

    conditions: list[str] = []

    if cw_filter:
        conditions.append(cw_filter)

    if not f.ignore_modified_dates and f.last_modified_on.strip():
        dt = _normalize_datetime(f.last_modified_on.strip())
        conditions.append(f"last_modified_on>to_datetime('{dt}')")

    for column, value in [
        ("company", f.company),
        ("business_unit", f.business_unit),
        ("compensation_pay_group", f.pay_group),
        ("company_territory_code", f.company_territory_code),
        ("division", f.division),
        ("location", f.location),
        ("employee_class", f.employee_class),
    ]:
        if value.strip():
            conditions.append(_in_clause(column, value))

    return " and ".join(conditions)


def build_query_string(f: CEQueryFilter) -> str:
    """Return the full SFQL query string for CompoundEmployee."""
    select = build_select_clause(f.select_segments)
    where = build_where_clause(f)
    return f"{select} where {where}" if where else select
