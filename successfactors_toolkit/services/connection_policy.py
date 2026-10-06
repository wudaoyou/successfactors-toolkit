"""Allowlist for per-request connection overrides.

`SFAPIConnectionConfig` / `ODataConnectionConfig` let a caller holding only the
plain API key redirect the server: an arbitrary `host`/`token_url` turns it into
an authenticated HTTPS relay, an arbitrary `private_key_path` makes it read
any local file, and `user_id`/`client_key` choose which SF identity signs in.
These checks run where an override is consumed — before any network or
filesystem I/O — so REST and MCP are covered by the same code path.
Values from the system file are operator input.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel

from successfactors_toolkit.config import Settings
from successfactors_toolkit.services.audit import audit

# SAP SuccessFactors datacenter domains (all DCs live under these).
_SAP_SUFFIXES = (
    ".successfactors.com",
    ".successfactors.eu",
    ".successfactors.cn",
    ".sapsf.com",
    ".sapsf.eu",
    ".sapsf.cn",
)
# Bare hostname only: no scheme, userinfo, port, path or whitespace.
_HOSTNAME_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


class ConnectionPolicyError(ValueError):
    """An override points somewhere this server is not allowed to reach."""


def deny(field: str, message: str, system: str, *, requested: bool = True) -> ConnectionPolicyError:
    """The error to raise when `field` fails the policy; records it in the audit
    log (the field's name, never its value). `requested` is whether the request
    supplied the value: a rejected value that came from the system file is a
    configuration error, not a refused override."""
    if requested:
        audit("connection_override", "denied", system=system, field=field)
    else:
        audit("connection_config", "failed", system=system, field=field)
    return ConnectionPolicyError(message)


def audit_overrides(conn: BaseModel, system: str, config) -> None:
    """Record, once the checks passed, which connection values the request
    supplied in place of the system file's (names only)."""
    fields = [
        f
        for f in ("host", "token_url", "client_key", "user_id")
        if getattr(conn, f) not in (None, getattr(config, f))
    ]
    if getattr(conn, "private_key_path", None):
        fields.append("private_key_path")
    if fields:
        audit("connection_override", "ok", system=system, fields=",".join(fields))


def check_host(
    host: str, settings: Settings, system: str, *, requested: bool = True, field: str = "host"
) -> str:
    """Return `host` if it is a bare hostname this server may talk to."""
    if not host or not _HOSTNAME_RE.fullmatch(host):
        raise deny(
            field,
            f"Invalid host {host!r}: expected a bare hostname.",
            system,
            requested=requested,
        )
    lowered = host.lower()
    allowed = {h.lower() for h in settings.sf_allowed_hosts}
    if lowered in allowed or lowered.endswith(_SAP_SUFFIXES):
        return host
    raise deny(
        field,
        f"Host {host!r} is not allowed: use a SAP SuccessFactors datacenter host or one listed in SF_ALLOWED_HOSTS.",
        system,
        requested=requested,
    )


def check_token_url(
    token_url: str, settings: Settings, system: str, *, requested: bool = True
) -> str:
    """Return `token_url` if it is https on an allowed host."""
    try:
        parts = urlsplit(token_url)
    except ValueError as exc:  # e.g. an unterminated IPv6 literal
        raise deny(
            "token_url",
            f"token_url {token_url!r} is not a valid URL.",
            system,
            requested=requested,
        ) from exc
    if parts.scheme != "https":
        raise deny(
            "token_url", f"token_url {token_url!r} must use https.", system, requested=requested
        )
    hostname = parts.hostname or ""
    if parts.netloc.lower() != hostname:
        raise deny(
            "token_url",
            "token_url must not carry credentials or a port.",
            system,
            requested=requested,
        )
    check_host(hostname, settings, system, requested=requested, field="token_url")
    return token_url


def check_key_path(path: str, settings: Settings, system: str) -> Path:
    """Return the resolved key path if it stays inside this system's directory."""
    system_dir = (Path(settings.systems_dir) / system).resolve()
    # resolve() follows symlinks, so a link inside the directory pointing out fails.
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError) as exc:  # e.g. an embedded NUL, or a name too long
        raise deny(
            "private_key_path", f"private_key_path {path!r} is not a usable path.", system
        ) from exc
    if not resolved.is_relative_to(system_dir):
        raise deny(
            "private_key_path",
            f"private_key_path {path!r} must be inside the directory of system {system!r}.",
            system,
        )
    return resolved


def check_identity(field: str, override: str | None, configured: str, system: str) -> str:
    """Return the `user_id`/`client_key` to sign in with.

    `configured` is the system file's value. A request may repeat it but not
    replace it.
    """
    if override is None or override == configured:
        return configured
    if configured:
        raise deny(
            field,
            f"{field} is configured for this system and cannot be overridden per request.",
            system,
        )
    return override
