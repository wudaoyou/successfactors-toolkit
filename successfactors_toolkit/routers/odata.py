from typing import Annotated

from fastapi import APIRouter, Depends, Request

from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.models.odata import (
    ApiResponse,
    ODataExtractByFilterInRequest,
    ODataExtractByFilterInResponse,
    ODataExtractRequest,
    ODataExtractResponse,
    ODataRequest,
)
from successfactors_toolkit.services.odata_client import ODataClient

router = APIRouter(prefix="/odata", tags=["OData"])


def get_odata_client(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> ODataClient:
    # Reuse the singleton client created at app startup so the OAuth token
    # cache survives across requests (see successfactors_toolkit.main.lifespan).
    return request.app.state.odata_client


@router.post("/execute", response_model=ApiResponse, summary="Execute a single OData request")
async def execute_odata(
    payload: ODataRequest,
    client: Annotated[ODataClient, Depends(get_odata_client)],
) -> ApiResponse:
    result = await client.request(
        method=payload.method,
        path=payload.path,
        conn=payload.connection,
        params=payload.params,
        body=payload.body,
        extra_headers=payload.headers,
    )
    return ApiResponse(**result)


@router.post(
    "/extract",
    response_model=ODataExtractResponse,
    summary="Bulk-extract an entity set, auto-following __next links",
)
async def extract_odata(
    payload: ODataExtractRequest,
    client: Annotated[ODataClient, Depends(get_odata_client)],
) -> ODataExtractResponse:
    result = await client.extract_all(
        path=payload.path,
        conn=payload.connection,
        params=payload.params,
        max_pages=payload.max_pages,
    )
    return ODataExtractResponse(**result)


@router.post(
    "/extract-by-filter-in",
    response_model=ODataExtractByFilterInResponse,
    summary="Bulk-extract by IN-list filter (auto-chunked to SF's 1000-value cap)",
)
async def extract_by_filter_in(
    payload: ODataExtractByFilterInRequest,
    client: Annotated[ODataClient, Depends(get_odata_client)],
) -> ODataExtractByFilterInResponse:
    result = await client.extract_by_filter_in(
        path=payload.path,
        column=payload.column,
        values=payload.values,
        conn=payload.connection,
        params=payload.params,
        chunk_size=payload.chunk_size,
        max_pages_per_chunk=payload.max_pages_per_chunk,
    )
    return ODataExtractByFilterInResponse(**result)
