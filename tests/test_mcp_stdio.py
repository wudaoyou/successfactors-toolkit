import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_mcp_stdio_initialization_discovery_and_tenant_call(tmp_path, request_timeout_seconds=15):
    root = Path(__file__).resolve().parents[1]

    async def exercise():
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("SF_") and key not in {"API_KEY", "ADMIN_API_KEY"}
        }
        env.update(
            {
                "PYTHONPATH": str(root),
                "SF_HOST": "api.example.invalid",
                "TENANT_KEYS_DIR": str(tmp_path / "tenants"),
            }
        )
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "successfactors_toolkit.mcp_server"],
            cwd=str(tmp_path),
            env=env,
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=request_timeout_seconds
            ) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "successfactors"
                discovered = await session.list_tools()
                assert {tool.name for tool in discovered.tools} == {
                    "list_tenants",
                    "odata_query",
                    "odata_metadata",
                    "compare_metadata",
                    "ce_query",
                }
                odata_query = next(tool for tool in discovered.tools if tool.name == "odata_query")
                assert odata_query.input_schema["properties"]["preview"]["minimum"] == 0
                assert odata_query.input_schema["properties"]["preview"]["maximum"] == 20
                result = await session.call_tool("list_tenants", {})
                assert not result.is_error
                assert result.structured_content["tenants"] == []

    asyncio.run(asyncio.wait_for(exercise(), timeout=max(30, 2 * request_timeout_seconds)))
