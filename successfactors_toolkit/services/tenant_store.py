"""Per-tenant key+cert storage on disk.

Directory layout (one subdir per SF company):

    ${tenant_keys_dir}/
    ├── example-dev/
    │   ├── sf_private_key_example-dev.pem      (mode 600)
    │   ├── sf_saml_signing_example-dev.crt     (mode 644)
    │   └── example-dev.json                    (mode 644)
    ├── example-test/
    │   ├── sf_private_key_example-test.pem
    │   ├── sf_saml_signing_example-test.crt
    │   └── example-test.json
    └── ...

Filenames are derived from company_id; the API rejects uploads that
contain a key/cert that don't pair, or where the cert is expired.

{company_id}.json holds the tenant's settings (TenantConfig): "production"
(required; sets the MCP PII tier, see services/pii_filter.for_tenant), optional
PII settings, and optional connection settings that override the SF_*
environment. It may exist without a key+cert pair (a default tenant keyed from
the environment); such a directory is not listed as a tenant.

Writes are atomic: contents go to a temp directory, then `os.replace` swaps
it into place. A half-written tenant directory should never be visible.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import NameOID
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from successfactors_toolkit.services.connection_policy import ConnectionPolicyError

# company_id allowed chars — also enforced at the URL routing layer.
_COMPANY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# Ids the flag is read for: the ones credentials.load_key_pem resolves keys
# for, so a mixed-case SF_COMPANY_ID keyed from the environment has one too.
_FLAG_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")
_LEGACY_FLAG_FILE = "tenant.json"  # the 0.3.3 name; no longer read


class TenantConnection(BaseModel):
    """The connection keys of {company_id}.json; each overrides its SF_* setting."""

    model_config = ConfigDict(strict=True, extra="ignore")

    host: str | None = None
    token_url: str | None = None
    client_key: str | None = None
    user_id: str | None = None
    odata_version: Literal["v2", "v4"] | None = None

    @model_validator(mode="after")
    def _host_with_token_url(self):
        # A host with the environment's token URL would mint tokens elsewhere.
        if (self.host is None) != (self.token_url is None):
            raise ValueError("host and token_url must be set together")
        return self


class TenantConfig(TenantConnection):
    """The whole {company_id}.json. An unknown key is an error, not ignored."""

    model_config = ConfigDict(strict=True, extra="forbid")

    production: bool
    pii_filter_tier: int | None = Field(default=None, ge=0, le=3)
    pii_extra_fields: dict[str, dict[str, Annotated[int, Field(ge=1, le=3)]]] = {}

    @model_validator(mode="after")
    def _production_is_tier_three(self):
        if self.production and self.pii_filter_tier not in (None, 3):
            raise ValueError("pii_filter_tier must be 3 or absent for a production tenant")
        return self


class TenantStoreError(Exception):
    """Base class for all tenant store failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidCompanyId(TenantStoreError):
    pass


class InvalidKeyOrCert(TenantStoreError):
    pass


class KeyCertMismatch(TenantStoreError):
    pass


class CertExpired(TenantStoreError):
    pass


class TenantNotFound(TenantStoreError):
    pass


class TenantAlreadyExists(TenantStoreError):
    pass


class TenantConfigError(ConnectionPolicyError):
    """{company_id}.json has a value that fails validation. A policy error: on
    the connection path, falling back to SF_* would reach the wrong tenant."""

    def __init__(self, company_id: str, error: ValidationError) -> None:
        self.company_id = company_id
        parts = []
        for e in error.errors():
            where, msg = ".".join(map(str, e["loc"])), e["msg"].removeprefix("Value error, ")
            parts.append(f"{where}: {msg}" if where else msg)
        self.detail = "; ".join(parts)
        super().__init__(f"Invalid {company_id}/{company_id}.json: {self.detail}")


@dataclass(frozen=True)
class CertMetadata:
    cn: str
    sha256_fingerprint: str
    not_before: datetime
    not_after: datetime
    days_until_expiry: int

    @classmethod
    def from_cert(cls, cert: x509.Certificate) -> "CertMetadata":
        cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        cn = cn_attrs[0].value if cn_attrs else ""
        der = cert.public_bytes(serialization.Encoding.DER)
        import hashlib

        fp = hashlib.sha256(der).hexdigest().upper()
        fp_fmt = ":".join(fp[i : i + 2] for i in range(0, len(fp), 2))
        now = datetime.now(timezone.utc)
        return cls(
            cn=cn,
            sha256_fingerprint=fp_fmt,
            not_before=cert.not_valid_before_utc,
            not_after=cert.not_valid_after_utc,
            days_until_expiry=(cert.not_valid_after_utc - now).days,
        )


@dataclass(frozen=True)
class KeyMetadata:
    algorithm: str
    size_bits: int


@dataclass(frozen=True)
class TenantInfo:
    company_id: str
    directory: str
    private_key_path: str
    certificate_path: str
    certificate: CertMetadata
    private_key: KeyMetadata
    production: bool | None


def validate_company_id(company_id: str) -> None:
    if not _COMPANY_ID_RE.match(company_id):
        raise InvalidCompanyId(
            "invalid_company_id",
            f"company_id must match {_COMPANY_ID_RE.pattern!r} (lowercase, "
            "alphanumeric, underscore, hyphen; up to 63 chars)",
        )


def _parse_key(pem_bytes: bytes):
    try:
        return load_pem_private_key(pem_bytes, password=None)
    except Exception as e:
        raise InvalidKeyOrCert(
            "invalid_private_key",
            f"Could not parse private key as unencrypted PEM: {e}",
        ) from e


def _parse_cert(pem_bytes: bytes) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(pem_bytes, default_backend())
    except Exception as e:
        raise InvalidKeyOrCert(
            "invalid_certificate",
            f"Could not parse certificate as PEM X.509: {e}",
        ) from e


def _check_pair(key, cert: x509.Certificate) -> None:
    key_pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    cert_pub = cert.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if key_pub != cert_pub:
        raise KeyCertMismatch(
            "key_cert_mismatch",
            "The provided private key does not pair with the certificate.",
        )


def _check_expiry(cert: x509.Certificate) -> None:
    now = datetime.now(timezone.utc)
    if cert.not_valid_after_utc <= now:
        raise CertExpired(
            "certificate_expired",
            f"Certificate expired at {cert.not_valid_after_utc.isoformat()}.",
        )


class TenantStore:
    """File-backed registry of per-tenant key+cert pairs."""

    def __init__(self, base_dir: str) -> None:
        self._base = Path(base_dir)

    # ── path helpers ──────────────────────────────────────────────────────
    def tenant_dir(self, company_id: str) -> Path:
        return self._base / company_id

    def key_path(self, company_id: str) -> Path:
        return self.tenant_dir(company_id) / f"sf_private_key_{company_id}.pem"

    def cert_path(self, company_id: str) -> Path:
        return self.tenant_dir(company_id) / f"sf_saml_signing_{company_id}.crt"

    def config_path(self, company_id: str) -> Path:
        return self.tenant_dir(company_id) / f"{company_id}.json"

    # ── queries ───────────────────────────────────────────────────────────
    def exists(self, company_id: str) -> bool:
        validate_company_id(company_id)
        return self.key_path(company_id).exists() and self.cert_path(company_id).exists()

    def list_tenants(self) -> list[TenantInfo]:
        if not self._base.exists():
            return []
        out: list[TenantInfo] = []
        for entry in sorted(self._base.iterdir()):
            if not entry.is_dir():
                continue
            cid = entry.name
            if not _COMPANY_ID_RE.match(cid):
                # Stray directories are ignored, not an error — operators may
                # use the same volume for other purposes.
                continue
            if self.exists(cid):
                out.append(self.get(cid))
        return out

    def get(self, company_id: str) -> TenantInfo:
        validate_company_id(company_id)
        if not self.exists(company_id):
            raise TenantNotFound("tenant_not_found", f"No tenant registered for {company_id!r}.")
        key_bytes = self.key_path(company_id).read_bytes()
        cert_bytes = self.cert_path(company_id).read_bytes()
        key = _parse_key(key_bytes)
        cert = _parse_cert(cert_bytes)
        return TenantInfo(
            company_id=company_id,
            directory=str(self.tenant_dir(company_id)),
            private_key_path=str(self.key_path(company_id)),
            certificate_path=str(self.cert_path(company_id)),
            certificate=CertMetadata.from_cert(cert),
            private_key=KeyMetadata(
                algorithm=type(key).__name__.replace("PrivateKey", ""), size_bits=key.key_size
            ),
            production=self.production(company_id),
        )

    def _read(self, company_id: str) -> dict:
        """{company_id}.json as a dict; {} if missing, unreadable or not an object."""
        if not _FLAG_ID_RE.fullmatch(company_id):
            return {}
        try:
            data = json.loads(self.config_path(company_id).read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def production(self, company_id: str) -> bool | None:
        """The declared environment: True = production, False = test, None =
        unset (no file, unreadable, not JSON, or no boolean "production")."""
        value = self._read(company_id).get("production")
        return value if isinstance(value, bool) else None

    def config(self, company_id: str) -> TenantConfig | None:
        """The validated file, or None when unset (see production). Raises
        TenantConfigError when production is declared but another value is invalid."""
        data = self._read(company_id)
        if not isinstance(data.get("production"), bool):
            return None
        try:
            return TenantConfig.model_validate(data)
        except ValidationError as exc:
            raise TenantConfigError(company_id, exc) from exc

    def connection(self, company_id: str) -> dict[str, str]:
        """The connection keys the file sets, whatever the rest of it says.
        Raises TenantConfigError when one of them is invalid."""
        try:
            conn = TenantConnection.model_validate(self._read(company_id))
        except ValidationError as exc:
            raise TenantConfigError(company_id, exc) from exc
        return conn.model_dump(exclude_none=True)

    def has_legacy_flag(self, company_id: str) -> bool:
        """A 0.3.3 tenant.json is still there; it is no longer read."""
        return bool(_FLAG_ID_RE.fullmatch(company_id)) and (
            (self.tenant_dir(company_id) / _LEGACY_FLAG_FILE).exists()
        )

    # ── mutations ─────────────────────────────────────────────────────────
    def install(
        self,
        company_id: str,
        key_bytes: bytes,
        cert_bytes: bytes,
        *,
        force: bool = False,
    ) -> TenantInfo:
        """Validate and atomically install a key+cert pair for a company.

        Raises TenantAlreadyExists if the tenant is already registered and
        ``force=False``. Raises various validation errors otherwise.
        """
        validate_company_id(company_id)
        if self.exists(company_id) and not force:
            raise TenantAlreadyExists(
                "tenant_already_exists",
                f"Tenant {company_id!r} already exists. Re-POST with ?force=true to overwrite.",
            )

        key = _parse_key(key_bytes)
        cert = _parse_cert(cert_bytes)
        _check_pair(key, cert)
        _check_expiry(cert)

        # Normalize: write the key + cert from the parsed objects so we always
        # store standard, single-block PEM (rejects SailPoint-style combined
        # files, comments, etc.).
        normalized_key = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        normalized_cert = cert.public_bytes(serialization.Encoding.PEM)

        self._base.mkdir(parents=True, exist_ok=True)

        # Atomic: stage in a sibling temp dir, then os.replace into place.
        # If we crashed mid-install, the destination would still hold the
        # previous (working) version.
        with tempfile.TemporaryDirectory(prefix=f".{company_id}-stage-", dir=self._base) as stage:
            stage_path = Path(stage)
            tmp_key = stage_path / self.key_path(company_id).name
            tmp_cert = stage_path / self.cert_path(company_id).name
            tmp_key.write_bytes(normalized_key)
            tmp_cert.write_bytes(normalized_cert)
            os.chmod(tmp_key, 0o600)
            os.chmod(tmp_cert, 0o644)

            dest = self.tenant_dir(company_id)
            # A key rotation keeps the tenant's settings.
            config = self.config_path(company_id)
            if config.exists():
                shutil.copy2(config, stage_path / config.name)
            if dest.exists():
                shutil.rmtree(dest)
            # Rename the temp dir into place. Both paths are on the same
            # filesystem (sibling of base dir) so this is atomic on POSIX.
            os.rename(stage, dest)

            # tempfile context exit will try to remove `stage`, which we just
            # moved away — re-create it so the cleanup does not raise.
            stage_path.mkdir(exist_ok=True)

        return self.get(company_id)

    def set_production(self, company_id: str, production: bool) -> None:
        """Set "production" in {company_id}.json, keeping its other keys and its
        file mode (new file: 0644), atomically (temp file, then os.replace)."""
        validate_company_id(company_id)
        dest = self.tenant_dir(company_id)
        dest.mkdir(parents=True, exist_ok=True)
        path = self.config_path(company_id)
        data = {**self._read(company_id), "production": production}
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            mode = 0o644
        descriptor, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=dest)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as out:
                json.dump(data, out, indent=2)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise

    def delete(self, company_id: str) -> None:
        validate_company_id(company_id)
        if not self.exists(company_id):
            raise TenantNotFound("tenant_not_found", f"No tenant registered for {company_id!r}.")
        shutil.rmtree(self.tenant_dir(company_id))
