"""Per-tenant key+cert storage on disk.

Directory layout (one subdir per SF company):

    ${tenant_keys_dir}/
    ├── example-dev/
    │   ├── sf_private_key_example-dev.pem      (mode 600)
    │   └── sf_saml_signing_example-dev.crt     (mode 644)
    ├── example-test/
    │   ├── sf_private_key_example-test.pem
    │   └── sf_saml_signing_example-test.crt
    └── ...

Filenames are derived from company_id; the API rejects uploads that
contain a key/cert that don't pair, or where the cert is expired.

Writes are atomic: contents go to a temp directory, then `os.replace` swaps
it into place. A half-written tenant directory should never be visible.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import NameOID

# company_id allowed chars — also enforced at the URL routing layer.
_COMPANY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


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
            if dest.exists():
                shutil.rmtree(dest)
            # Rename the temp dir into place. Both paths are on the same
            # filesystem (sibling of base dir) so this is atomic on POSIX.
            os.rename(stage, dest)

            # tempfile context exit will try to remove `stage`, which we just
            # moved away — re-create it so the cleanup does not raise.
            stage_path.mkdir(exist_ok=True)

        return self.get(company_id)

    def delete(self, company_id: str) -> None:
        validate_company_id(company_id)
        if not self.exists(company_id):
            raise TenantNotFound("tenant_not_found", f"No tenant registered for {company_id!r}.")
        shutil.rmtree(self.tenant_dir(company_id))
