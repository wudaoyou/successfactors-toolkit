from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from successfactors_toolkit.models.sfapi import (
    ApiResponse,
    CEQueryAllPage,
    CEQueryAllRequest,
    CEQueryAllResponse,
    CEQueryByPersonIdRequest,
    CEQueryByUserIdRequest,
    CEQueryFilter,
    CompoundEmployeeQueryMoreRequest,
)
from successfactors_toolkit.services.ce_query_builder import (
    COMMON_SEGMENTS,
    build_query_string,
)
from successfactors_toolkit.services.ce_response import parse_page
from successfactors_toolkit.services.sfapi_client import SFAPIClient

router = APIRouter(prefix="/sfapi", tags=["EC SFAPI — Compound Employee (SOAP)"])


def get_sfapi_client(request: Request) -> SFAPIClient:
    """Return the app-state singleton so JSESSIONID cache survives between
    requests, and the tenant management router can invalidate entries."""
    return request.app.state.sfapi_client


def _params_from_filter(payload: CEQueryFilter) -> list[tuple[str, str]] | None:
    """Translate filter knobs into <urn:param> tuples for SFAPI."""
    params: list[tuple[str, str]] = []
    if payload.max_rows is not None:
        params.append(("maxRows", str(payload.max_rows)))
    return params or None


def _query_and_params(payload: CEQueryFilter) -> tuple[str, list[tuple[str, str]] | None]:
    """Build the SFQL query and its params. A filter the builder rejects (an
    ISO timestamp without a timezone, say) is the caller's mistake — 400, not
    an unhandled 500."""
    try:
        return build_query_string(payload), _params_from_filter(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/ce/query",
    response_model=ApiResponse,
    summary="Query Compound Employee with structured filters",
)
async def ce_query(
    payload: CEQueryFilter,
    client: Annotated[SFAPIClient, Depends(get_sfapi_client)],
) -> ApiResponse:
    query_string, params = _query_and_params(payload)
    result = await client.query(query_string, conn=payload.connection, params=params)
    return ApiResponse(**result)


@router.post(
    "/ce/query-by-person-id",
    response_model=ApiResponse,
    summary="Query Compound Employee by PERSON_ID_EXTERNAL (typical Postman lookup)",
)
async def ce_query_by_person_id(
    payload: CEQueryByPersonIdRequest,
    client: Annotated[SFAPIClient, Depends(get_sfapi_client)],
) -> ApiResponse:
    query_string = build_query_string(
        CEQueryFilter(
            person_id_external=",".join(payload.person_id_external),
            include_contingent_workers=payload.include_contingent_workers,
            select_segments=payload.select_segments or list(COMMON_SEGMENTS),
        )
    )
    result = await client.query(query_string, conn=payload.connection)
    return ApiResponse(**result)


@router.post(
    "/ce/query-by-user-id",
    response_model=ApiResponse,
    summary="Query Compound Employee by USER_ID (typical Postman lookup)",
)
async def ce_query_by_user_id(
    payload: CEQueryByUserIdRequest,
    client: Annotated[SFAPIClient, Depends(get_sfapi_client)],
) -> ApiResponse:
    query_string = build_query_string(
        CEQueryFilter(
            user_id=",".join(payload.user_id),
            include_contingent_workers=payload.include_contingent_workers,
            select_segments=payload.select_segments or list(COMMON_SEGMENTS),
        )
    )
    result = await client.query(query_string, conn=payload.connection)
    return ApiResponse(**result)


@router.post(
    "/ce/query-more", response_model=ApiResponse, summary="Paginate Compound Employee results"
)
async def ce_query_more(
    payload: CompoundEmployeeQueryMoreRequest,
    client: Annotated[SFAPIClient, Depends(get_sfapi_client)],
) -> ApiResponse:
    result = await client.query_more(payload.query_session, conn=payload.connection)
    return ApiResponse(**result)


@router.post(
    "/ce/query-all",
    response_model=CEQueryAllResponse,
    summary="Query Compound Employee and auto-paginate until hasMore=false",
)
async def ce_query_all(
    payload: CEQueryAllRequest,
    client: Annotated[SFAPIClient, Depends(get_sfapi_client)],
) -> CEQueryAllResponse:
    """Run the initial query, then loop queryMore until SF reports
    hasMore=false or max_pages is hit. Pages are returned as a list — caller
    stitches the sfobjects from each body."""
    pages: list[CEQueryAllPage] = []
    query_string, params = _query_and_params(payload)
    result = await client.query(query_string, conn=payload.connection, params=params)
    stopped_reason = "exhausted"
    while True:
        count, more, session, error = parse_page(str(result["body"]), int(result["status_code"]))
        pages.append(
            CEQueryAllPage(
                status_code=result["status_code"],
                body=result["body"],
                num_results=count,
                has_more=more,
                query_session_id=session,
            )
        )
        if error:
            stopped_reason = error
            break
        if not more:
            break
        if len(pages) >= payload.max_pages:
            stopped_reason = "max_pages"
            break
        result = await client.query_more(session, conn=payload.connection)
    return CEQueryAllResponse(
        page_count=len(pages),
        total_records=sum(page.num_results or 0 for page in pages),
        truncated=stopped_reason != "exhausted",
        stopped_reason=stopped_reason,
        pages=pages,
    )
