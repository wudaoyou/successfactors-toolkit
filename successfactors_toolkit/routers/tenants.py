"""Tenant management API — register and inspect per-company key+cert pairs.

Every endpoint (reads included) requires an `X-Admin-Key` header that matches
`Settings.admin_api_key`. If `admin_api_key` is empty (the default), the
endpoints reject every request — operators must opt in by setting the env var.

See successfactors_toolkit/services/tenant_store.py for the on-disk layout and validation rules.
"""

from __future__ import annotations

from datetime import datetime
from secrets import compare_digest
from typing import Annotated, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field, StrictBool

from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.services.tenant_store import (
    TenantInfo,
    TenantStore,
    TenantStoreError,
)


def require_admin_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_admin_key: Annotated[Optional[str], Header(alias="X-Admin-Key")] = None,
) -> None:
    """Reject every request if admin_api_key is unset (safe default) or the
    header is missing/wrong."""
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tenant admin API is disabled (set ADMIN_API_KEY to enable).",
        )
    if not x_admin_key or not compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Admin-Key header.",
        )


# Reads expose key/cert metadata (CN, fingerprints, paths) — admin-only too.
router = APIRouter(
    prefix="/api/tenants",
    tags=["Tenant Management"],
    dependencies=[Depends(require_admin_key)],
)


# ── DTOs ──────────────────────────────────────────────────────────────────


class CertMetadataDTO(BaseModel):
    cn: str
    sha256_fingerprint: str
    not_before: datetime
    not_after: datetime
    days_until_expiry: int
    warning: Optional[str] = Field(
        default=None,
        description="Set when the certificate is expiring soon (< 90 days).",
    )


class KeyMetadataDTO(BaseModel):
    algorithm: str
    size_bits: int


class TenantInfoDTO(BaseModel):
    company_id: str
    directory: str
    private_key_path: str
    certificate_path: str
    certificate: CertMetadataDTO
    private_key: KeyMetadataDTO
    production: Optional[bool] = Field(
        description="From tenant.json; null = unset, so the MCP data tools refuse the tenant.",
    )


class EnvironmentDTO(BaseModel):
    production: StrictBool


def _to_dto(info: TenantInfo) -> TenantInfoDTO:
    cert = CertMetadataDTO(
        cn=info.certificate.cn,
        sha256_fingerprint=info.certificate.sha256_fingerprint,
        not_before=info.certificate.not_before,
        not_after=info.certificate.not_after,
        days_until_expiry=info.certificate.days_until_expiry,
        warning=(
            f"Expires in {info.certificate.days_until_expiry} days"
            if info.certificate.days_until_expiry < 90
            else None
        ),
    )
    return TenantInfoDTO(
        company_id=info.company_id,
        directory=info.directory,
        private_key_path=info.private_key_path,
        certificate_path=info.certificate_path,
        certificate=cert,
        private_key=KeyMetadataDTO(
            algorithm=info.private_key.algorithm,
            size_bits=info.private_key.size_bits,
        ),
        production=info.production,
    )


# ── deps ──────────────────────────────────────────────────────────────────


def get_tenant_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> TenantStore:
    return TenantStore(settings.tenant_keys_dir)


def _invalidate_session_cache(request: Request, company_id: str) -> None:
    """Evict this tenant's sessions and tokens after key replacement or deletion."""
    for client_name, cache_name in (("sfapi_client", "_sessions"), ("odata_client", "_tokens")):
        client = getattr(request.app.state, client_name, None)
        cache = getattr(client, cache_name, {})
        for key in list(cache):
            if key[1] == company_id:
                cache.pop(key, None)


# ── path validators ───────────────────────────────────────────────────────

# Same regex as TenantStore.validate_company_id — enforced here at the URL
# routing layer so traversal attempts (../, /) are rejected before any code
# runs.
CompanyIdPath = Path(
    ...,
    pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$",
    description="Lowercase alphanumeric, underscore, hyphen; 1-63 chars.",
)


# ── error mapping ─────────────────────────────────────────────────────────


def _raise_http(e: TenantStoreError) -> None:
    code_to_status = {
        "invalid_company_id": status.HTTP_400_BAD_REQUEST,
        "invalid_private_key": status.HTTP_400_BAD_REQUEST,
        "invalid_certificate": status.HTTP_400_BAD_REQUEST,
        "key_cert_mismatch": status.HTTP_400_BAD_REQUEST,
        "certificate_expired": status.HTTP_400_BAD_REQUEST,
        "certificate_not_yet_valid": status.HTTP_400_BAD_REQUEST,
        "tenant_already_exists": status.HTTP_409_CONFLICT,
        "tenant_not_found": status.HTTP_404_NOT_FOUND,
    }
    raise HTTPException(
        status_code=code_to_status.get(e.code, status.HTTP_500_INTERNAL_SERVER_ERROR),
        detail={"error": e.code, "message": e.message},
    )


# ── endpoints ─────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=list[TenantInfoDTO],
    summary="List all registered tenants",
)
async def list_tenants(
    store: Annotated[TenantStore, Depends(get_tenant_store)],
) -> list[TenantInfoDTO]:
    return [_to_dto(t) for t in store.list_tenants()]


@router.get(
    "/{company_id}",
    response_model=TenantInfoDTO,
    summary="Get a single tenant's key+cert metadata",
)
async def get_tenant(
    company_id: Annotated[str, CompanyIdPath],
    store: Annotated[TenantStore, Depends(get_tenant_store)],
) -> TenantInfoDTO:
    try:
        return _to_dto(store.get(company_id))
    except TenantStoreError as e:
        _raise_http(e)


@router.post(
    "/{company_id}/keypair",
    response_model=TenantInfoDTO,
    status_code=status.HTTP_201_CREATED,
    summary="Install or replace a tenant's private key + certificate",
)
async def install_keypair(
    request: Request,
    company_id: Annotated[str, CompanyIdPath],
    store: Annotated[TenantStore, Depends(get_tenant_store)],
    private_key: Annotated[UploadFile, File(description="PEM-encoded RSA private key")],
    certificate: Annotated[UploadFile, File(description="PEM-encoded X.509 certificate")],
    force: Annotated[bool, Query(description="Overwrite if tenant already exists")] = False,
) -> TenantInfoDTO:
    key_bytes = await private_key.read()
    cert_bytes = await certificate.read()
    try:
        info = store.install(company_id, key_bytes, cert_bytes, force=force)
    except TenantStoreError as e:
        _raise_http(e)
    _invalidate_session_cache(request, company_id)
    return _to_dto(info)


@router.put(
    "/{company_id}/environment",
    response_model=TenantInfoDTO,
    summary="Declare a tenant production or test (sets its MCP PII tier)",
)
async def set_environment(
    company_id: Annotated[str, CompanyIdPath],
    body: EnvironmentDTO,
    store: Annotated[TenantStore, Depends(get_tenant_store)],
) -> TenantInfoDTO:
    try:
        store.get(company_id)  # tenant_not_found before anything is written
        store.set_production(company_id, body.production)
        return _to_dto(store.get(company_id))
    except TenantStoreError as e:
        _raise_http(e)


@router.delete(
    "/{company_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a tenant's key+cert directory",
)
async def delete_tenant(
    request: Request,
    company_id: Annotated[str, CompanyIdPath],
    store: Annotated[TenantStore, Depends(get_tenant_store)],
) -> None:
    try:
        store.delete(company_id)
    except TenantStoreError as e:
        _raise_http(e)
    _invalidate_session_cache(request, company_id)
