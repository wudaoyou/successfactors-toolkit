"""System management API — inspect systems and manage their keys and environment.

Every endpoint (reads included) requires an `X-Admin-Key` header that matches
`Settings.admin_api_key`. If `admin_api_key` is empty (the default), the
endpoints reject every request — operators must opt in by setting the env var.

See successfactors_toolkit/services/system_store.py for the on-disk layout and validation rules.
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
from successfactors_toolkit.services.audit import audit
from successfactors_toolkit.services.system_store import (
    SF_TYPE,
    KeypairInfo,
    SystemStore,
    SystemStoreError,
)


def require_admin_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_admin_key: Annotated[Optional[str], Header(alias="X-Admin-Key")] = None,
) -> None:
    """Reject every request if admin_api_key is unset (safe default) or the
    header is missing/wrong."""
    admin_key = settings.admin_api_key.get_secret_value()
    if not admin_key:
        audit("auth", "denied", scope="admin", reason="disabled")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="System admin API is disabled (set ADMIN_API_KEY to enable).",
        )
    # compare_digest raises TypeError on non-ASCII str, so compare bytes.
    if not x_admin_key or not compare_digest(x_admin_key.encode(), admin_key.encode()):
        audit("auth", "denied", scope="admin", reason="invalid" if x_admin_key else "missing")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Admin-Key header.",
        )


# Reads expose system settings and key/cert metadata (CN, fingerprints, paths) — admin-only too.
router = APIRouter(
    prefix="/api/systems",
    tags=["System Management"],
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


class KeypairDTO(BaseModel):
    private_key_path: str
    certificate_path: str
    certificate: CertMetadataDTO
    private_key: KeyMetadataDTO


class SystemInfoDTO(BaseModel):
    name: str
    type: Optional[str] = Field(description="From <name>.json; null = missing or not a string.")
    production: Optional[bool] = Field(
        description="From <name>.json; null = unset, so the MCP data tools refuse the system.",
    )
    directory: str
    error: Optional[str] = Field(
        default=None, description="Why the system is refused (system_invalid, system_unsupported)."
    )
    detail: Optional[str] = None
    keypair: Optional[KeypairDTO] = Field(
        default=None, description="SuccessFactors systems with a key and certificate installed."
    )


class EnvironmentDTO(BaseModel):
    production: StrictBool


def _keypair_dto(info: KeypairInfo) -> KeypairDTO:
    cert = info.certificate
    return KeypairDTO(
        private_key_path=info.private_key_path,
        certificate_path=info.certificate_path,
        certificate=CertMetadataDTO(
            cn=cert.cn,
            sha256_fingerprint=cert.sha256_fingerprint,
            not_before=cert.not_before,
            not_after=cert.not_after,
            days_until_expiry=cert.days_until_expiry,
            warning=(
                f"Expires in {cert.days_until_expiry} days" if cert.days_until_expiry < 90 else None
            ),
        ),
        private_key=KeyMetadataDTO(
            algorithm=info.private_key.algorithm, size_bits=info.private_key.size_bits
        ),
    )


def _to_dto(store: SystemStore, name: str) -> SystemInfoDTO:
    info = store.info(name)
    keypair = None
    if info.type == SF_TYPE:
        try:
            keypair = store.keypair(name)
        except SystemStoreError:
            keypair = None  # an unparsable key or cert is listed without metadata
    return SystemInfoDTO(
        name=info.name,
        type=info.type,
        production=info.production,
        directory=info.directory,
        error=info.error,
        detail=info.detail,
        keypair=None if keypair is None else _keypair_dto(keypair),
    )


# ── deps ──────────────────────────────────────────────────────────────────


def get_system_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SystemStore:
    return SystemStore(settings.systems_dir)


def _invalidate_session_cache(request: Request, name: str) -> None:
    """Evict this system's sessions and tokens after key replacement or deletion."""
    for client_name, cache_name in (("sfapi_client", "_sessions"), ("odata_client", "_tokens")):
        client = getattr(request.app.state, client_name, None)
        cache = getattr(client, cache_name, {})
        for key in list(cache):
            if key[0] == name:
                cache.pop(key, None)


# ── path validators ───────────────────────────────────────────────────────

# Same regex as system_store.NAME_RE — enforced here at the URL routing layer
# so traversal attempts (../, /) are rejected before any code runs.
NamePath = Path(
    ...,
    pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$",
    description="Lowercase alphanumeric, underscore, hyphen; 1-63 chars.",
)


# ── error mapping ─────────────────────────────────────────────────────────

_STATUS = {
    "invalid_system_name": status.HTTP_400_BAD_REQUEST,
    "invalid_private_key": status.HTTP_400_BAD_REQUEST,
    "invalid_certificate": status.HTTP_400_BAD_REQUEST,
    "key_cert_mismatch": status.HTTP_400_BAD_REQUEST,
    "certificate_expired": status.HTTP_400_BAD_REQUEST,
    "certificate_not_yet_valid": status.HTTP_400_BAD_REQUEST,
    "wrong_system_type": status.HTTP_400_BAD_REQUEST,
    "system_invalid": status.HTTP_400_BAD_REQUEST,
    "system_not_found": status.HTTP_404_NOT_FOUND,
    "keypair_already_exists": status.HTTP_409_CONFLICT,
}


def _raise_http(e: SystemStoreError, event: str | None = None, system: str | None = None) -> None:
    if event:
        audit(event, "failed", system=system, code=e.code)
    raise HTTPException(
        status_code=_STATUS.get(e.code, status.HTTP_500_INTERNAL_SERVER_ERROR),
        detail={"error": e.code, "message": e.message},
    )


# ── endpoints ─────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=list[SystemInfoDTO],
    summary="List every system under SYSTEMS_DIR",
)
async def list_systems(
    store: Annotated[SystemStore, Depends(get_system_store)],
) -> list[SystemInfoDTO]:
    return [_to_dto(store, name) for name in store.names()]


@router.get(
    "/{name}",
    response_model=SystemInfoDTO,
    summary="Get a single system's settings and key+cert metadata",
)
async def get_system(
    name: Annotated[str, NamePath],
    store: Annotated[SystemStore, Depends(get_system_store)],
) -> SystemInfoDTO:
    if name not in store.names():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "system_not_found", "message": f"No system {name!r}."},
        )
    return _to_dto(store, name)


@router.post(
    "/{name}/keypair",
    response_model=SystemInfoDTO,
    status_code=status.HTTP_201_CREATED,
    summary="Install or replace a successfactors system's private key + certificate",
)
async def install_keypair(
    request: Request,
    name: Annotated[str, NamePath],
    store: Annotated[SystemStore, Depends(get_system_store)],
    private_key: Annotated[UploadFile, File(description="PEM-encoded RSA private key")],
    certificate: Annotated[UploadFile, File(description="PEM-encoded X.509 certificate")],
    force: Annotated[bool, Query(description="Replace an installed keypair")] = False,
) -> SystemInfoDTO:
    key_bytes = await private_key.read()
    cert_bytes = await certificate.read()
    try:
        store.install(name, key_bytes, cert_bytes, force=force)
    except SystemStoreError as e:
        _raise_http(e, "key_install", name)
    audit("key_install", "ok", system=name, force=force)
    _invalidate_session_cache(request, name)
    return _to_dto(store, name)


@router.put(
    "/{name}/environment",
    response_model=SystemInfoDTO,
    summary="Declare a system production or test (sets its MCP PII tier)",
)
async def set_environment(
    name: Annotated[str, NamePath],
    body: EnvironmentDTO,
    store: Annotated[SystemStore, Depends(get_system_store)],
) -> SystemInfoDTO:
    try:
        previous = store.info(name).production
        store.set_production(name, body.production)  # system_not_found before anything is written
    except SystemStoreError as e:
        _raise_http(e, "production_flag", name)
    audit(
        "production_flag",
        "ok",
        system=name,
        previous="unset" if previous is None else previous,
        production=body.production,
    )
    return _to_dto(store, name)


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a system's directory (its file, key and certificate)",
)
async def delete_system(
    request: Request,
    name: Annotated[str, NamePath],
    store: Annotated[SystemStore, Depends(get_system_store)],
) -> None:
    try:
        store.delete(name)
    except SystemStoreError as e:
        _raise_http(e, "key_delete", name)
    audit("key_delete", "ok", system=name)
    _invalidate_session_cache(request, name)
