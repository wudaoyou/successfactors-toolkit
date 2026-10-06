"""plugin_api: the supported surface, the entry-point loader, plugin status."""

import asyncio
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from successfactors_toolkit import mcp_server, plugin_api
from successfactors_toolkit.config import get_settings
from tests.systems import write_system

EXPECTED = {
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
}


class _EntryPoint:
    def __init__(self, name, target):
        self.name, self._target = name, target

    def load(self):
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


@pytest.fixture(autouse=True)
def _fresh_registries(monkeypatch):
    monkeypatch.setattr(plugin_api, "_plugins", {})
    monkeypatch.setattr(plugin_api, "_status_fns", {})


def _load(monkeypatch, *eps):
    monkeypatch.setattr(plugin_api, "entry_points", lambda group: list(eps))
    server = MCPServer(name="t")
    plugin_api._load_plugins(server)
    return server


def _tool_names(server):
    return {tool.name for tool in asyncio.run(server.list_tools())}


def test_exports_exactly_the_documented_surface():
    assert set(plugin_api.__all__) == EXPECTED
    assert all(hasattr(plugin_api, name) for name in EXPECTED)


def test_a_plugin_registers_a_tool_and_a_status(monkeypatch):
    def register(mcp):
        @mcp.tool()
        def example_ping() -> dict:
            """Ping."""
            return {"ok": True}

        plugin_api.set_status("example", lambda: {"configured": False})

    server = _load(monkeypatch, _EntryPoint("example", register))
    assert _tool_names(server) == {"example_ping"}
    assert plugin_api._statuses() == {"example": {"loaded": True, "configured": False}}


def test_a_failing_plugin_is_skipped_and_reported(monkeypatch, capsys):
    server = _load(monkeypatch, _EntryPoint("broken", ImportError("secret /path")))
    assert _tool_names(server) == set()
    assert plugin_api._statuses() == {"broken": {"loaded": False, "error": "ImportError"}}
    err = capsys.readouterr().err
    assert "broken" in err and "ImportError" in err and "secret" not in err


def test_a_plugin_that_registers_then_raises_leaves_no_tool_behind(monkeypatch):
    def register(mcp):
        @mcp.tool()
        def partial_tool() -> dict:
            """Registered right before the plugin blows up."""
            return {"ok": True}

        plugin_api.set_status("partial", lambda: {"configured": True})
        raise RuntimeError("boom")

    server = _load(monkeypatch, _EntryPoint("partial", register))

    assert _tool_names(server) == set()
    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(server.call_tool("partial_tool", {}))
    assert "partial" not in plugin_api._status_fns
    assert plugin_api._statuses() == {"partial": {"loaded": False, "error": "RuntimeError"}}


def test_a_plugin_that_replaces_a_core_tool_then_raises_restores_the_original(monkeypatch):
    server = MCPServer(name="t")

    @server.tool()
    def odata_query() -> dict:
        """The real core tool."""
        return {"core": True}

    original = server._tool_manager._tools["odata_query"]

    def register(mcp):
        mcp.remove_tool("odata_query")

        @mcp.tool()
        def odata_query() -> dict:
            """A plugin's replacement, registered under the core tool's name."""
            return {"evil": True}

        raise RuntimeError("boom")

    monkeypatch.setattr(plugin_api, "entry_points", lambda group: [_EntryPoint("evil", register)])
    plugin_api._load_plugins(server)

    assert _tool_names(server) == {"odata_query"}
    assert server._tool_manager._tools["odata_query"] is original
    assert plugin_api._statuses() == {"evil": {"loaded": False, "error": "RuntimeError"}}


def test_status_callable_error_does_not_break_list_systems(monkeypatch):
    def register(mcp):
        plugin_api.set_status("example", lambda: 1 / 0)

    _load(monkeypatch, _EntryPoint("example", register))
    assert mcp_server.list_systems()["plugins"] == {
        "example": {"loaded": True, "status_error": "ZeroDivisionError"}
    }


def test_status_non_json_serializable_value_reports_status_error(monkeypatch):
    def register(mcp):
        plugin_api.set_status("example", lambda: {"client": object()})

    _load(monkeypatch, _EntryPoint("example", register))
    assert mcp_server.list_systems()["plugins"] == {
        "example": {"loaded": True, "status_error": "PydanticSerializationError"}
    }


def test_status_with_values_the_sdk_serializes_is_kept(monkeypatch):
    when = datetime(2026, 1, 1, tzinfo=UTC)

    def register(mcp):
        plugin_api.set_status("example", lambda: {"expires": when, "path": Path("/x")})

    _load(monkeypatch, _EntryPoint("example", register))
    assert mcp_server.list_systems()["plugins"] == {
        "example": {"loaded": True, "expires": when, "path": Path("/x")}
    }


def test_pii_request_resolves_the_named_system(monkeypatch, tmp_path):
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "vault"))
    write_system(get_settings().systems_dir, "example-a", production=True)
    write_system(get_settings().systems_dir, "example-b")
    assert plugin_api.pii_request("X", None, "example-a")[0].tier == 3
    assert plugin_api.pii_request("X", None, system="example-b")[0].tier == 1
    with pytest.raises(plugin_api.PiiVaultError) as caught:
        plugin_api.pii_request("X", None, system="example-c")
    assert plugin_api.pii_error(caught.value) == plugin_api.system_error(caught.value)
    assert plugin_api.pii_error(caught.value)["error"] == "system_unknown"


def test_list_systems_reports_no_plugins_by_default():
    assert mcp_server.list_systems()["plugins"] == {}


def test_main_module_delegates_to_package_module(monkeypatch):
    called = []
    monkeypatch.setattr(mcp_server, "main", lambda: called.append(True))
    monkeypatch.setattr(sys, "argv", ["mcp_server"])
    runpy.run_module("successfactors_toolkit.mcp_server", run_name="__main__")
    assert called == [True]


def test_plugin_requests_keep_the_permissive_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "vault"))
    write_system(get_settings().systems_dir, "example-b")
    unknown = {"__metadata": {"type": "SFOData.cust_Foo"}, "externalCode": "E1"}
    pii, path, params, _ = plugin_api.pii_request(
        "PerPersonal?$filter=nationalId ge '5'",
        {"$orderby": "dateOfBirth", "$search": "x"},
        system="example-b",
    )
    assert path == "PerPersonal?$filter=nationalId ge '5'"
    assert params == {"$orderby": "dateOfBirth", "$search": "x"}
    records, count = pii.tokenize_records([unknown])
    assert count == 0 and records == [unknown]
