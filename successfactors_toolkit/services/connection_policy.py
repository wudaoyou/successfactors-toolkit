"""Allowlist for per-request connection overrides.

`SFAPIConnectionConfig` / `ODataConnectionConfig` let a caller holding only the
plain API key redirect the server: an arbitrary `host`/`token_url` turns it into
an authenticated HTTPS relay, an arbitrary `private_key_path` makes it read
any local file, and `user_id`/`client_key` choose which SF identity signs in.
These checks run where an override is consumed — before any network or
filesystem I/O — so REST and MCP are covered by the same code path.
Values that come from Settings are trusted by definition.
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


def deny(field: str, message: str, company_id: str | None = None) -> ConnectionPolicyError:
    """The error to raise when `field` fails the policy; records the denial in
    the audit log (the field's name, never its value)."""
    audit("connection_override", "denied", company_id=company_id, field=field)
    return ConnectionPolicyError(message)


def audit_overrides(conn: BaseModel, company_id: str, settings: Settings, tenant: dict) -> None:
    """Record, once the checks passed, which connection values the request
    supplied in place of the tenant's configured ones (names only)."""
    fields = [
        f
        for f in ("host", "token_url", "client_key", "user_id")
        if getattr(conn, f) not in (None, tenant.get(f, getattr(settings, f"sf_{f}")))
    ]
    if getattr(conn, "private_key_path", None):
        fields.append("private_key_path")
    if fields:
        audit("connection_override", "ok", company_id=company_id, fields=",".join(fields))


def check_host(host: str, settings: Settings, field: str = "host") -> str:
    """Return `host` if it is a bare hostname this server may talk to."""
    if host and host == settings.sf_host:
        return host  # the operator's own configuration, whatever shape it has
    if not host or not _HOSTNAME_RE.fullmatch(host):
        raise deny(field, f"Invalid host {host!r}: expected a bare hostname.")
    lowered = host.lower()
    allowed = {settings.sf_host.lower(), *(h.lower() for h in settings.sf_allowed_hosts)}
    if lowered in allowed or lowered.endswith(_SAP_SUFFIXES):
        return host
    raise deny(
        field,
        f"Host {host!r} is not allowed: use the configured SF_HOST, a host listed in "
        "SF_ALLOWED_HOSTS, or a SAP SuccessFactors datacenter host.",
    )


def check_token_url(token_url: str, settings: Settings) -> str:
    """Return `token_url` if it is https on an allowed host."""
    if token_url and token_url == settings.sf_token_url:
        return token_url  # the operator's own configuration
    try:
        parts = urlsplit(token_url)
    except ValueError as exc:  # e.g. an unterminated IPv6 literal
        raise deny("token_url", f"token_url {token_url!r} is not a valid URL.") from exc
    if parts.scheme != "https":
        raise deny("token_url", f"token_url {token_url!r} must use https.")
    hostname = parts.hostname or ""
    if parts.netloc.lower() != hostname:
        raise deny("token_url", "token_url must not carry credentials or a port.")
    check_host(hostname, settings, "token_url")
    return token_url


def check_key_path(path: str, settings: Settings, company_id: str) -> Path:
    """Return the resolved key path if it stays inside this tenant's key directory."""
    tenant_dir = (Path(settings.tenant_keys_dir) / company_id).resolve()
    # resolve() follows symlinks, so a link inside the directory pointing out fails.
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError) as exc:  # e.g. an embedded NUL, or a name too long
        raise deny(
            "private_key_path", f"private_key_path {path!r} is not a usable path.", company_id
        ) from exc
    if not resolved.is_relative_to(tenant_dir):
        raise deny(
            "private_key_path",
            f"private_key_path {path!r} must be inside the key directory of tenant {company_id!r}.",
            company_id,
        )
    return resolved


def check_identity(field: str, override: str | None, configured: str) -> str:
    """Return the `user_id`/`client_key` to sign in with.

    `configured` is what the operator set for the tenant ({company_id}.json,
    else Settings). A request may repeat it but not replace it; it may only
    supply a value where nothing is configured.
    """
    if override is None or override == configured:
        return configured
    if configured:
        raise deny(
            field, f"{field} is configured for this tenant and cannot be overridden per request."
        )
    return override
