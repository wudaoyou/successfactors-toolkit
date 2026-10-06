"""Per-system configuration on disk: SYSTEMS_DIR/<name>/.

    ${SYSTEMS_DIR}/
    ├── example-dev/
    │   ├── example-dev.json    (mode 644)  "type", "production" and the type's keys
    │   ├── private-key.pem     (mode 600)  SuccessFactors systems: SAML signing key
    │   └── signing-cert.crt    (mode 644)  and its certificate
    └── ...

The name is the directory name and the <name>.json basename, spelled exactly
(see exact_case_path). "type" picks the model that validates the file
(register_type); an unknown key is an error. A system exists once its
<name>.json does; the REST API installs a SuccessFactors system's key and
certificate, flips "production" and deletes systems, but never creates one.

Writes are atomic: a key rotation stages the directory's new contents in a temp
directory, which is then exchanged with the system directory in one rename (see
_exchange), so a half-written pair is never visible and the directory never
disappears. Writers hold an flock on ${SYSTEMS_DIR}/.lock, so they serialize
across threads and processes.
"""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import NameOID
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from successfactors_toolkit.services.connection_policy import ConnectionPolicyError

# System names: also enforced at the URL routing layer.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# A certificate minted seconds ago on a clock slightly ahead of ours is valid.
_CLOCK_SKEW = timedelta(minutes=5)


class SystemStoreError(Exception):
    """Base class for all system store failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidSystemName(SystemStoreError):
    pass


class InvalidKeyOrCert(SystemStoreError):
    pass


class KeyCertMismatch(SystemStoreError):
    pass


class CertExpired(SystemStoreError):
    pass


class CertNotYetValid(SystemStoreError):
    pass


class SystemNotFound(SystemStoreError):
    pass


class KeypairAlreadyExists(SystemStoreError):
    pass


class WrongSystemType(SystemStoreError):
    pass


class InvalidSystemFile(SystemStoreError):
    """<name>.json can't be read as a JSON object."""


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


def validate_system_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise InvalidSystemName(
            "invalid_system_name",
            f"System names must match {NAME_RE.pattern!r} (lowercase letters, digits, "
            "underscore, hyphen; up to 63 chars).",
        )


def exact_case_path(base: Path, *parts: str) -> Path | None:
    """base/parts if each part exists in its parent spelled exactly so, else None.

    A case-insensitive filesystem (macOS, Docker Desktop bind mounts) opens
    systems/demo/ for "DEMO". System names are exact, so "DEMO" and "demo" are
    different systems and one must never read the other's files.
    """
    path = base
    for part in parts:
        try:
            if part not in os.listdir(path):
                return None
        except OSError:
            return None
        path = path / part
    return path


def _exchange(a: Path, b: Path) -> None:
    """Swap two existing paths in one atomic rename."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rc = libc.renamex_np(os.fsencode(a), os.fsencode(b), 0x2)  # RENAME_SWAP
    else:
        rc = libc.renameat2(-100, os.fsencode(a), -100, os.fsencode(b), 0x2)  # AT_FDCWD, EXCHANGE
    if rc:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(b))


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


def _check_key(key) -> None:
    # The SAML assertion is signed rsa-sha256: any other key fails only at runtime.
    if not isinstance(key, rsa.RSAPrivateKey):
        raise InvalidKeyOrCert("invalid_private_key", "The private key must be an RSA key.")
    if key.key_size < 2048:
        raise InvalidKeyOrCert(
            "invalid_private_key",
            f"The RSA private key must be at least 2048 bits, not {key.key_size}.",
        )


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
    if cert.not_valid_before_utc > now + _CLOCK_SKEW:
        raise CertNotYetValid(
            "certificate_not_yet_valid",
            f"Certificate is not valid until {cert.not_valid_before_utc.isoformat()}.",
        )


# ── Systems: SYSTEMS_DIR/<name>/<name>.json ───────────────────────────────
SF_TYPE = "successfactors"
KEY_FILE = "private-key.pem"
CERT_FILE = "signing-cert.crt"
_TYPE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def _validation_detail(error: ValidationError) -> str:
    """Field paths and messages only: pydantic's input values may be secrets."""
    parts = []
    for e in error.errors():
        where, msg = ".".join(map(str, e["loc"])), e["msg"].removeprefix("Value error, ")
        parts.append(f"{where}: {msg}" if where else msg)
    return "; ".join(parts)


class SystemBase(BaseModel):
    """Keys every system file has, whatever its type. Each type's model
    extends this one; an unknown key is an error."""

    model_config = ConfigDict(strict=True, extra="forbid")

    type: str
    production: bool
    pii_filter_tier: int | None = Field(default=None, ge=0, le=3)
    pii_extra_fields: dict[str, dict[str, Annotated[int, Field(ge=1, le=3)]]] = {}

    @model_validator(mode="after")
    def _production_is_tier_three(self):
        if self.production and self.pii_filter_tier not in (None, 3):
            raise ValueError("pii_filter_tier must be 3 or absent for a production system")
        return self


class SuccessFactorsSystem(SystemBase):
    type: Literal["successfactors"]
    company_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
    host: str = Field(min_length=1)
    token_url: str = Field(min_length=1)
    client_key: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    odata_version: Literal["v2", "v4"] = "v2"


_TYPES: dict[str, type[SystemBase]] = {SF_TYPE: SuccessFactorsSystem}


def register_type(type_: str, model: type[SystemBase]) -> None:
    """Validate files whose "type" is `type_` with `model`. Registering the
    same model again is a no-op; any other clash is a ValueError."""
    if not _TYPE_RE.fullmatch(type_) or not issubclass(model, SystemBase):
        raise ValueError(f"Invalid system type registration {type_!r}.")
    current = _TYPES.get(type_)
    if current is model:
        return
    if current is not None:
        raise ValueError(f"System type {type_!r} is already registered.")
    _TYPES[type_] = model


class SystemUnavailable(ConnectionPolicyError):
    """A system that can't be served: system_unknown, system_required,
    system_unsupported or system_invalid. A ConnectionPolicyError, so the
    connection path refuses it like any other policy failure."""

    def __init__(self, code: str, system: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.system = system
        self.detail = detail


@dataclass(frozen=True)
class SystemInfo:
    name: str
    type: str | None
    production: bool | None
    directory: str
    error: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class KeypairInfo:
    private_key_path: str
    certificate_path: str
    certificate: CertMetadata
    private_key: KeyMetadata


class SystemStore:
    """One directory per system: <base>/<name>/<name>.json plus its secret files."""

    def __init__(self, base: str | Path) -> None:
        self.base = Path(base)

    def system_dir(self, name: str) -> Path:
        return self.base / name

    @contextlib.contextmanager
    def _locked(self):
        """Hold the store's write lock. flock on a fresh descriptor conflicts
        with every other descriptor, so threads of one process exclude each other too."""
        self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.base / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # releases the lock

    def config_path(self, name: str) -> Path:
        return self.system_dir(name) / f"{name}.json"

    def names(self) -> list[str]:
        try:
            entries = sorted(os.listdir(self.base))
        except OSError:
            return []
        return [
            name
            for name in entries
            if NAME_RE.fullmatch(name) and exact_case_path(self.base, name, f"{name}.json")
        ]

    def _load(self, name: str) -> dict:
        path = exact_case_path(self.base, name, f"{name}.json") if NAME_RE.fullmatch(name) else None
        if path is None:
            raise SystemUnavailable(
                "system_unknown", name, f"No system {name!r} under SYSTEMS_DIR."
            )
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            raise SystemUnavailable(
                "system_invalid", name, f"{name}/{name}.json is not readable JSON."
            ) from None
        if not isinstance(data, dict):
            raise SystemUnavailable(
                "system_invalid", name, f"{name}/{name}.json must hold a JSON object."
            )
        return data

    def raw(self, name: str) -> dict:
        """<name>.json as a dict; {} if missing, unreadable or not an object."""
        try:
            return self._load(name)
        except SystemUnavailable:
            return {}

    def config(self, name: str) -> SystemBase:
        """The file validated by its type's model. Raises SystemUnavailable."""
        return self._validate(name, self._load(name))

    def _validate(self, name: str, data: dict) -> SystemBase:
        type_ = data.get("type")
        if not isinstance(type_, str):
            raise SystemUnavailable(
                "system_invalid", name, 'type: a string such as "successfactors" is required'
            )
        model = _TYPES.get(type_)
        if model is None:
            raise SystemUnavailable(
                "system_unsupported", name, f"No installed handler for type {type_!r}."
            )
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise SystemUnavailable("system_invalid", name, _validation_detail(exc)) from None

    def select(self, type_: str, name: str = "") -> str:
        """`name` if it is a valid system of `type_`; for "", the only one.
        Fails closed while any file has no readable type, since that file
        could be the one meant. Raises SystemUnavailable."""
        if name:
            config = self.config(name)
            if config.type != type_:
                raise SystemUnavailable(
                    "system_unknown", name, f"{name!r} is a {config.type} system, not {type_}."
                )
            return name
        raws = {n: self.raw(n) for n in self.names()}
        unreadable = [n for n, data in raws.items() if not isinstance(data.get("type"), str)]
        matches = [n for n, data in raws.items() if data.get("type") == type_]
        if not matches and not unreadable:
            raise SystemUnavailable("system_unknown", "", f"No {type_} system under SYSTEMS_DIR.")
        if len(matches) != 1 or unreadable:
            raise SystemUnavailable(
                "system_required",
                "",
                f"Pass system as one of: {', '.join(matches + unreadable)}"
                + (f" ({', '.join(unreadable)}: no readable type)" if unreadable else "")
                + ".",
            )
        self._validate(matches[0], raws[matches[0]])
        return matches[0]

    def info(self, name: str) -> SystemInfo:
        """What the file declares, plus the error that refuses it (if any)."""
        raw = self.raw(name)
        type_, production = raw.get("type"), raw.get("production")
        info = SystemInfo(
            name=name,
            type=type_ if isinstance(type_, str) else None,
            production=production if isinstance(production, bool) else None,
            directory=str(self.system_dir(name)),
        )
        try:
            self.config(name)
        except SystemUnavailable as exc:
            return replace(info, error=exc.code, detail=exc.detail)
        return info

    def keypair(self, name: str) -> KeypairInfo | None:
        """The SF signing key + certificate metadata, None while either file is
        missing. Raises InvalidKeyOrCert when one doesn't parse."""
        if not NAME_RE.fullmatch(name):
            return None
        key_path = exact_case_path(self.base, name, KEY_FILE)
        cert_path = exact_case_path(self.base, name, CERT_FILE)
        if key_path is None or cert_path is None:
            return None
        key = _parse_key(key_path.read_bytes())
        cert = _parse_cert(cert_path.read_bytes())
        return KeypairInfo(
            private_key_path=str(key_path),
            certificate_path=str(cert_path),
            certificate=CertMetadata.from_cert(cert),
            private_key=KeyMetadata(
                algorithm=type(key).__name__.replace("PrivateKey", ""), size_bits=key.key_size
            ),
        )

    # ── mutations ─────────────────────────────────────────────────────────
    def _exists(self, name: str) -> None:
        """Raises InvalidSystemName, or SystemNotFound without a <name>.json."""
        validate_system_name(name)
        if exact_case_path(self.base, name, f"{name}.json") is None:
            raise SystemNotFound(
                "system_not_found", f"No system {name!r}: create {name}/{name}.json first."
            )

    def _existing(self, name: str) -> dict:
        """The raw file of a system that exists. Raises InvalidSystemFile when
        it can't be read, so a mutation never rewrites a file it doesn't understand."""
        self._exists(name)
        try:
            return self._load(name)
        except SystemUnavailable as exc:
            raise InvalidSystemFile("system_invalid", exc.detail) from None

    def install(
        self, name: str, key_bytes: bytes, cert_bytes: bytes, *, force: bool = False
    ) -> KeypairInfo:
        """Validate and atomically install an SF system's key + certificate,
        keeping every other file in its directory."""
        with self._locked():
            # Checked under the lock: concurrent POSTs cannot both see no keypair.
            if self._existing(name).get("type") != SF_TYPE:
                raise WrongSystemType(
                    "wrong_system_type", f"System {name!r} is not a {SF_TYPE} system."
                )
            dest = self.system_dir(name)
            if not force and all(
                exact_case_path(self.base, name, f) for f in (KEY_FILE, CERT_FILE)
            ):
                raise KeypairAlreadyExists(
                    "keypair_already_exists",
                    f"System {name!r} already has a keypair. Re-POST with ?force=true to replace it.",
                )
            key = _parse_key(key_bytes)
            cert = _parse_cert(cert_bytes)
            _check_key(key)
            _check_pair(key, cert)
            _check_expiry(cert)
            # Normalize: write the key + cert from the parsed objects so we always
            # store standard, single-block PEM (rejects SailPoint-style combined
            # files, comments, etc.).
            with tempfile.TemporaryDirectory(prefix=f".{name}-stage-", dir=self.base) as stage:
                stage_path = Path(stage)
                for entry in dest.iterdir():
                    if entry.name not in (KEY_FILE, CERT_FILE) and entry.is_file():
                        shutil.copy2(entry, stage_path / entry.name)
                (stage_path / KEY_FILE).write_bytes(
                    key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.TraditionalOpenSSL,
                        serialization.NoEncryption(),
                    )
                )
                (stage_path / CERT_FILE).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
                os.chmod(stage_path / KEY_FILE, 0o600)
                os.chmod(stage_path / CERT_FILE, 0o644)
                try:
                    # The stage then holds the old version, removed on exit.
                    _exchange(stage_path, dest)
                except (AttributeError, OSError):
                    # ponytail: no exchange on this filesystem (some FUSE or network
                    # mounts): replace the files in place, so the directory never
                    # disappears, but the pair changes one file at a time.
                    for file_name in (CERT_FILE, KEY_FILE):
                        os.replace(stage_path / file_name, dest / file_name)
        return self.keypair(name)

    def set_production(self, name: str, production: bool) -> None:
        """Set "production" in <name>.json, keeping its other keys and its file
        mode, atomically (temp file, then os.replace)."""
        with self._locked():
            data = {**self._existing(name), "production": production}
            path = self.config_path(name)
            mode = stat.S_IMODE(path.stat().st_mode)
            descriptor, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=self.system_dir(name))
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as out:
                    json.dump(data, out, indent=2)
                os.chmod(tmp, mode)
                os.replace(tmp, path)
            except BaseException:
                os.unlink(tmp)
                raise

    def delete(self, name: str) -> None:
        """Remove the system's directory, readable file or not."""
        with self._locked():
            self._exists(name)
            shutil.rmtree(self.system_dir(name))
