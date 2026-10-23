"""Read-only SAP SuccessFactors Integration Center plugin for the MCP server."""

from successfactors_toolkit.plugin_api import set_status

# Keep equal to `version` in the plugin's pyproject.toml (a test checks it).
__version__ = "0.1.0"


def register(mcp) -> None:
    set_status("integration_center", lambda: {"version": __version__})
