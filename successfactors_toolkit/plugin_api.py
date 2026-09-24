"""The supported import surface for successfactors_toolkit plugins.

A plugin is an installed package with an entry point in the group
``successfactors_toolkit.plugins`` whose object is ``register(mcp)``. The MCP
server calls it once at startup; it adds tools with ``@mcp.tool()`` and may
report a status block in list_tenants with ``set_status(<entry point name>,
fn)``. Plugins run with the server's full privileges, like any installed
package. Import only the names below; everything else is internal.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

import httpx

from successfactors_toolkit import mcp_server as _server
from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError
from successfactors_toolkit.services.odata_client import _parse_retry_after as parse_retry_after
from successfactors_toolkit.services.odata_client import api_url, split_path_query
from successfactors_toolkit.services.pii_filter import PiiUnknownTokenError, PiiVaultError

__all__ = [
    "get_settings",
    "Settings",
    "ConnectionPolicyError",
    "api_url",
    "split_path_query",
    "parse_retry_after",
    "http_client",
    "write_result",
    "attach_preview",
    "pii_request",
    "pii_mark",
    "pii_error",
    "pii_safe_error_body",
    "PiiVaultError",
    "PiiUnknownTokenError",
    "set_status",
]

GROUP = "successfactors_toolkit.plugins"

write_result = _server._write
attach_preview = _server._attach_preview
pii_request = _server._pii_request
pii_mark = _server._pii_mark
pii_error = _server._pii_error
pii_safe_error_body = _server._pii_safe_error_body

_plugins: dict[str, dict[str, Any]] = {}
_status_fns: dict[str, Callable[[], dict[str, Any]]] = {}


def http_client() -> httpx.AsyncClient:
    """The server's shared, bounded httpx pool (closed by the server lifespan)."""
    _server._clients()
    return _server._http  # type: ignore[return-value]


def set_status(name: str, fn: Callable[[], dict[str, Any]]) -> None:
    """Report `fn()` under list_tenants' plugins[name]; name = your entry point name."""
    _status_fns[name] = fn


def _load_plugins(mcp) -> None:
    for ep in entry_points(group=GROUP):
        try:
            ep.load()(mcp)
        except Exception as exc:  # a broken plugin must not take the core tools down
            # Type only: an exception message can carry paths or config values.
            print(
                f"successfactors-mcp: plugin {ep.name!r} not loaded ({type(exc).__name__})",
                file=sys.stderr,
            )
            _plugins[ep.name] = {"loaded": False, "error": type(exc).__name__}
        else:
            _plugins[ep.name] = {"loaded": True}


def _statuses() -> dict[str, dict[str, Any]]:
    out = {}
    for name, base in _plugins.items():
        entry = dict(base)
        fn = _status_fns.get(name)
        if base["loaded"] and fn is not None:
            try:
                entry.update(fn())
            except Exception as exc:
                entry["status_error"] = type(exc).__name__
        out[name] = entry
    return out
