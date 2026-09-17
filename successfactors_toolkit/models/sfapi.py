from pydantic import BaseModel, Field

from successfactors_toolkit.models.common import SFAPIConnectionConfig


class CEQueryFilter(BaseModel):
    """Structured filter parameters — mirrors the SAP CPI Groovy query builder."""

    connection: SFAPIConnectionConfig | None = Field(
        default=None, description="Override default connection settings."
    )

    # Identity filters take precedence over date / org / job filters.
    person_id_external: str = Field(
        default="",
        description=(
            "Comma-separated employee IDs (PERSON_ID_EXTERNAL). When set, date, org, and job filters are ignored; "
            "include_contingent_workers may still be applied."
        ),
        examples=["EMP001,EMP002"],
    )
    user_id: str = Field(
        default="",
        description=(
            "Comma-separated user IDs (USER_ID). When set, date, org, and job filters are ignored; "
            "include_contingent_workers may still be applied. Takes lower precedence than person_id_external."
        ),
        examples=["jsmith,sgrant"],
    )

    # Date / delta filters
    last_modified_on: str = Field(
        default="",
        description="ISO datetime string, e.g. '2026-04-01T00:00:00+0000'. Trailing tz offset is normalised to Z. SAP enforces a max look-back of 3 months.",
    )
    ignore_modified_dates: bool = Field(
        default=False,
        description="Set to true to skip the last_modified_on filter (full extract).",
    )
    include_contingent_workers: bool = Field(
        default=False,
        description="Set to true to include contingent workers alongside regular employees.",
    )

    # Org / job filters
    company: str = Field(default="", description="Comma-separated company codes.")
    business_unit: str = Field(default="", description="Comma-separated business unit codes.")
    pay_group: str = Field(
        default="", description="Comma-separated pay group codes (maps to compensation_pay_group)."
    )
    company_territory_code: str = Field(default="", description="Comma-separated territory codes.")
    division: str = Field(default="", description="Comma-separated division codes.")
    location: str = Field(default="", description="Comma-separated location codes.")
    employee_class: str = Field(default="", description="Comma-separated employee class codes.")

    # SELECT customisation
    select_segments: list[str] | None = Field(
        default=None,
        description=(
            "Override the list of segments in the SELECT clause. "
            "Defaults to DEFAULT_SEGMENTS (all docs-supported segments). "
            "Use a narrower list if your tenant returns INVALID_SFQL for a module-gated segment."
        ),
        examples=[["person", "employment_information", "job_information"]],
    )

    # Pagination control
    max_rows: int | None = Field(
        default=None,
        ge=1,
        le=800,
        description="Max rows per page (1-800). SAP default is 200. Sent as the maxRows <urn:param>.",
    )


class CEQueryByPersonIdRequest(BaseModel):
    """Single-employee lookup by personIdExternal."""

    connection: SFAPIConnectionConfig | None = Field(
        default=None, description="Override default connection settings."
    )
    person_id_external: list[str] = Field(
        ...,
        description="One or more PERSON_ID_EXTERNAL values to look up.",
        examples=[["EMP001", "EMP002"]],
    )
    select_segments: list[str] | None = Field(
        default=None,
        description="Optional segment list override. Defaults to COMMON_SEGMENTS for highest tenant compatibility.",
    )
    include_contingent_workers: bool = Field(
        default=False,
        description="Set to true to include contingent workers alongside regular employees.",
    )


class CEQueryByUserIdRequest(BaseModel):
    """Single-employee lookup by USER_ID."""

    connection: SFAPIConnectionConfig | None = Field(
        default=None, description="Override default connection settings."
    )
    user_id: list[str] = Field(
        ...,
        description="One or more USER_ID values to look up.",
        examples=[["jsmith", "sgrant"]],
    )
    select_segments: list[str] | None = Field(
        default=None,
        description="Optional segment list override. Defaults to COMMON_SEGMENTS for highest tenant compatibility.",
    )
    include_contingent_workers: bool = Field(
        default=False,
        description="Set to true to include contingent workers alongside regular employees.",
    )


class CompoundEmployeeQueryMoreRequest(BaseModel):
    connection: SFAPIConnectionConfig | None = Field(
        default=None, description="Override default connection settings."
    )
    query_session: str = Field(
        ...,
        description="querySessionId returned in the previous /query or /query-more response.",
    )


class CEQueryAllRequest(CEQueryFilter):
    """Auto-pagination variant of CEQueryFilter. Loops queryMore until
    hasMore=false or max_pages is hit."""

    max_pages: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Safety cap on the number of pages to fetch. Default 50 × 200 rows = 10,000 records.",
    )


# ── Response models ───────────────────────────────────────────────────────


class ApiResponse(BaseModel):
    """Generic single-call response — raw SOAP body plus HTTP metadata."""

    status_code: int
    headers: dict[str, str]
    body: str


class CEQueryAllPage(BaseModel):
    """One page in a multi-page query-all result."""

    status_code: int
    body: str
    num_results: int | None = Field(
        default=None, description="Parsed from <numResults> in the response, if present."
    )
    has_more: bool | None = Field(default=None, description="Parsed from <hasMore>.")
    query_session_id: str | None = Field(default=None, description="Parsed from <querySessionId>.")


class CEQueryAllResponse(BaseModel):
    """Concatenated pages from /sfapi/ce/query-all."""

    stopped_reason: str = "exhausted"
    page_count: int
    total_records: int = Field(description="Sum of numResults across all pages.")
    truncated: bool = Field(description="True if pagination stopped before successful exhaustion.")
    pages: list[CEQueryAllPage]
