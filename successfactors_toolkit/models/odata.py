from typing import Any

from pydantic import BaseModel, Field

from successfactors_toolkit.models.common import ODataConnectionConfig

# Reused field description: callers consistently forget that EmpJob, Position,
# and all FO* entities are effective-dated. Without asOfDate/fromDate/toDate
# the API silently returns only the time slice valid today (§6.5.2.1).
_PATH_DESC = (
    "OData entity path, e.g. 'EmpJob' or 'User?$top=10'. "
    "NOTE: effective-dated entities (EmpJob, Position, FO*, MDF) without "
    "asOfDate/fromDate/toDate return ONLY today's effective record. "
    "Pass fromDate=1900-01-01&toDate=9999-12-31 for full history."
)


class ODataRequest(BaseModel):
    connection: ODataConnectionConfig | None = Field(
        default=None, description="Override default connection settings."
    )
    method: str = Field(default="GET", pattern="^(GET|POST|PATCH|PUT|DELETE)$")
    path: str = Field(..., description=_PATH_DESC)
    params: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


class ODataExtractRequest(BaseModel):
    """Bulk-extract an entity set, auto-following __next links until exhausted."""

    connection: ODataConnectionConfig | None = None
    path: str = Field(..., description=_PATH_DESC)
    params: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Pass paging=cursor for entities that support it (EmpJob, User, "
            "MDF Generic Objects, most FO* — see Dev Guide §5.5.3.2.1). "
            "Otherwise client-side $skip+$top pagination is used."
        ),
    )
    max_pages: int = Field(default=100, ge=1, le=10000, description="Safety cap.")


class ODataExtractResponse(BaseModel):
    pages_fetched: int
    total_records: int
    results: list[dict[str, Any]]
    next_skiptoken: str | None = Field(
        default=None,
        description="If non-null, max_pages was hit before exhaustion; pass "
        "back in params['$skiptoken'] to resume.",
    )
    last_status_code: int
    last_headers: dict[str, str]
    stopped_reason: str = Field(
        description="One of: exhausted | max_pages | http_error | parse_error"
    )


class ODataExtractByFilterInRequest(BaseModel):
    """Bulk-extract with a large IN list, chunked under SF's 1000-value limit."""

    connection: ODataConnectionConfig | None = None
    path: str = Field(..., description=_PATH_DESC)
    column: str = Field(..., description="Column to filter on, e.g. 'externalCode'.")
    values: list[str] = Field(..., description="Values to match. Deduplicated automatically.")
    params: dict[str, Any] | None = Field(
        default=None,
        description="Additional query params. An existing $filter is AND-combined with the IN clause.",
    )
    chunk_size: int = Field(default=1000, ge=1, le=1000)
    max_pages_per_chunk: int = Field(default=100, ge=1, le=10000)


class ODataExtractByFilterInResponse(BaseModel):
    chunks_processed: int
    total_pages_fetched: int
    total_records: int
    results: list[dict[str, Any]]
    chunk_diagnostics: list[dict[str, Any]] = Field(
        description="Per-chunk breakdown for observability (chunk_index, records_fetched, etc.)."
    )


class ApiResponse(BaseModel):
    status_code: int
    headers: dict[str, str]
    body: str
