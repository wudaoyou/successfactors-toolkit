"""Allowlist for per-request connection overrides.

`SFAPIConnectionConfig` / `ODataConnectionConfig` let a caller holding only the
plain API key redirect the server: an arbitrary `host`/`token_url` turns it into
an authenticated HTTPS relay, and an arbitrary `private_key_path` makes it read
any local file. These checks run where an override is consumed — before any
network or filesystem I/O — so REST and MCP are covered by the same code path.
Values that come from Settings are trusted by definition.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from successfactors_toolkit.config import Settings

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


def check_host(host: str, settings: Settings) -> str:
    """Return `host` if it is a bare hostname this server may talk to."""
    if host and host == settings.sf_host:
        return host  # the operator's own configuration, whatever shape it has
    if not host or not _HOSTNAME_RE.fullmatch(host):
        raise ConnectionPolicyError(f"Invalid host {host!r}: expected a bare hostname.")
    lowered = host.lower()
    allowed = {settings.sf_host.lower(), *(h.lower() for h in settings.sf_allowed_hosts)}
    if lowered in allowed or lowered.endswith(_SAP_SUFFIXES):
        return host
    raise ConnectionPolicyError(
        f"Host {host!r} is not allowed: use the configured SF_HOST, a host listed in "
        "SF_ALLOWED_HOSTS, or a SAP SuccessFactors datacenter host."
    )


def check_token_url(token_url: str, settings: Settings) -> str:
    """Return `token_url` if it is https on an allowed host."""
    if token_url and token_url == settings.sf_token_url:
        return token_url  # the operator's own configuration
    try:
        parts = urlsplit(token_url)
    except ValueError as exc:  # e.g. an unterminated IPv6 literal
        raise ConnectionPolicyError(f"token_url {token_url!r} is not a valid URL.") from exc
    if parts.scheme != "https":
        raise ConnectionPolicyError(f"token_url {token_url!r} must use https.")
    hostname = parts.hostname or ""
    if parts.netloc.lower() != hostname:
        raise ConnectionPolicyError("token_url must not carry credentials or a port.")
    check_host(hostname, settings)
    return token_url


def check_key_path(path: str, settings: Settings) -> Path:
    """Return the resolved key path if it stays inside the tenant key store."""
    keys_dir = Path(settings.tenant_keys_dir).resolve()
    # resolve() follows symlinks, so a link inside the store pointing out fails.
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError) as exc:  # e.g. an embedded NUL, or a name too long
        raise ConnectionPolicyError(f"private_key_path {path!r} is not a usable path.") from exc
    if not resolved.is_relative_to(keys_dir):
        raise ConnectionPolicyError(
            f"private_key_path {path!r} must be inside the tenant key store."
        )
    return resolved
