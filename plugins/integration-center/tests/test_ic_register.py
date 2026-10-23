"""register(): the declared entry point loads, and it adds no tools yet."""

import asyncio
import tomllib
from importlib.metadata import EntryPoint
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from successfactors_toolkit import plugin_api

PROJECT = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
DECLARED = PROJECT["entry-points"][plugin_api.GROUP]


def test_the_declared_entry_point_loads_a_callable_register():
    assert DECLARED == {"integration_center": "sf_integration_center:register"}
    assert callable(
        EntryPoint("integration_center", DECLARED["integration_center"], plugin_api.GROUP).load()
    )


def test_register_adds_no_tools_and_reports_the_package_version(monkeypatch):
    monkeypatch.setattr(plugin_api, "_plugins", {})
    monkeypatch.setattr(plugin_api, "_status_fns", {})
    entry = EntryPoint("integration_center", DECLARED["integration_center"], plugin_api.GROUP)
    monkeypatch.setattr(plugin_api, "entry_points", lambda group: [entry])
    server = MCPServer(name="t")

    plugin_api._load_plugins(server)

    assert asyncio.run(server.list_tools()) == []
    assert plugin_api._statuses() == {
        "integration_center": {"loaded": True, "version": PROJECT["version"]}
    }
