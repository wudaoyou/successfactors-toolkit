from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from successfactors_toolkit.config import get_settings
from successfactors_toolkit.routers import odata, sfapi, tenants
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError
from successfactors_toolkit.services.odata_client import ODataClient
from successfactors_toolkit.services.sfapi_client import SFAPIClient

_VERSION = (Path(__file__).parent.parent / "VERSION").read_text().strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bounded pool + HTTP/2 multiplexing keeps bulk extracts efficient without
    # tripping SF's per-tenant rate limits (§12.7). Defaults of 100/10 are too
    # aggressive for a single OAuth client.
    app.state.http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        http2=True,
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
    )
    settings = get_settings()
    # Singleton SFAPIClient so its JSESSIONID cache persists across requests
    # AND the tenant management router can invalidate cached sessions when a
    # tenant's key is replaced or deleted.
    app.state.sfapi_client = SFAPIClient(settings, app.state.http_client)
    # Singleton ODataClient so its OAuth token cache survives across requests.
    app.state.odata_client = ODataClient(settings, app.state.http_client)
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(
    title="SuccessFactors Toolkit",
    description="Tools for SAP SuccessFactors Compound Employee (SOAP) and OData APIs.",
    version=_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ConnectionPolicyError)
async def connection_policy_error(_: Request, exc: ConnectionPolicyError) -> JSONResponse:
    """A rejected connection override is a bad request, not a server fault."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: Annotated[str | None, Depends(_api_key_header)]) -> None:
    expected = get_settings().api_key
    if not expected:
        raise HTTPException(503, "REST API disabled: configure API_KEY.")
    if not key or not compare_digest(key, expected):
        raise HTTPException(401, "Missing or invalid X-API-Key header.")


app.include_router(sfapi.router, prefix="/api", dependencies=[Depends(require_api_key)])
app.include_router(odata.router, prefix="/api", dependencies=[Depends(require_api_key)])
app.include_router(tenants.router, dependencies=[Depends(require_api_key)])


@app.get("/health", tags=["System"])
async def health() -> dict[str, str]:
    return {"status": "ok", "version": _VERSION}


@app.get("/", tags=["System"])
async def root() -> dict[str, str]:
    return {"message": "SuccessFactors Toolkit", "docs": "/docs"}
