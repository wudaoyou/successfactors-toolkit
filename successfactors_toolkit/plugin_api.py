"""The supported import surface for successfactors_toolkit plugins.

A plugin is an installed package with an entry point in the group
``successfactors_toolkit.plugins`` whose object is ``register(mcp)``. The MCP
server calls it once at startup; it adds tools with ``@mcp.tool()`` and may
report a status block in list_systems with ``set_status(<entry point name>,
fn)``. A plugin may claim a system type with ``register_system_type``.
Plugins run with the server's full privileges, like any installed
package. Import only the names below; everything else is internal.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import httpx
from pydantic_core import to_jsonable_python

from successfactors_toolkit import mcp_server as _server
from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.services import system_store as _systems
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError
from successfactors_toolkit.services.odata_client import _parse_retry_after as parse_retry_after
from successfactors_toolkit.services.odata_client import api_url, split_path_query
from successfactors_toolkit.services.pii_filter import PiiUnknownTokenError, PiiVaultError
from successfactors_toolkit.services.system_store import SystemBase, SystemUnavailable

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
    "SystemBase",
    "SystemUnavailable",
    "register_system_type",
    "select_system",
    "system_config",
    "system_dir",
    "system_error",
]

GROUP = "successfactors_toolkit.plugins"

write_result = _server._write
attach_preview = _server._attach_preview
pii_request = _server._pii_request
pii_mark = _server._pii_mark
pii_error = _server._pii_error
pii_safe_error_body = _server._pii_safe_error_body
system_error = _server._system_error

_plugins: dict[str, dict[str, Any]] = {}
_status_fns: dict[str, Callable[[], dict[str, Any]]] = {}


def http_client() -> httpx.AsyncClient:
    """The server's shared, bounded httpx pool (closed by the server lifespan)."""
    _server._clients()
    return _server._http  # type: ignore[return-value]


def set_status(name: str, fn: Callable[[], dict[str, Any]]) -> None:
    """Report `fn()` under list_systems' plugins[name]; name = your entry point name."""
    _status_fns[name] = fn


def register_system_type(type_: str, model: type[SystemBase]) -> None:
    """Validate SYSTEMS_DIR files whose "type" is `type_` with `model`, a
    SystemBase subclass. Call it from register(mcp)."""
    _systems.register_type(type_, model)


def select_system(type_: str, system: str = "") -> str:
    """`system` if it is a valid system of `type_`, or the only one for "".
    Raises SystemUnavailable (answer it with system_error)."""
    return _server._store().select(type_, system)


def system_config(system: str) -> SystemBase:
    """The system's validated file. Raises SystemUnavailable."""
    return _server._store().config(system)


def system_dir(system: str) -> Path:
    """The directory of a system select_system or system_config accepted."""
    return _server._store().system_dir(system)


def _load_plugins(mcp) -> None:
    for ep in entry_points(group=GROUP):
        saved_tools = dict(mcp._tool_manager._tools)
        saved_types = dict(_systems._TYPES)
        try:
            ep.load()(mcp)
        except Exception as exc:  # a broken plugin must not take the core tools down
            # Registration is atomic: a plugin that raises after adding,
            # removing or replacing tools (even a core one, by registering
            # under its name) or a status fn must not leave any of that
            # behind — restore the exact pre-registration snapshot rather
            # than diffing tool names, which would miss a replaced tool.
            mcp._tool_manager._tools.clear()
            mcp._tool_manager._tools.update(saved_tools)
            _systems._TYPES.clear()
            _systems._TYPES.update(saved_types)
            _status_fns.pop(ep.name, None)
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
                candidate = dict(base)
                candidate.update(fn())
                # Fail here, not in the SDK's result serialization, which would sink list_systems.
                to_jsonable_python(candidate)
            except Exception as exc:
                entry["status_error"] = type(exc).__name__
            else:
                entry = candidate
        out[name] = entry
    return out
