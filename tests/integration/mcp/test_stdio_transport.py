"""End-to-end test of the stdio MCP transport with the SDK's own client.

Spawns ``python -m machina.mcp`` as a subprocess, exactly as an MCP client
(Claude Desktop, an IDE) would, and drives it over stdio: initialize, list
the tools, call one.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="MCP SDK not installed (pip install machina-ai[mcp])")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_CMMS = REPO_ROOT / "examples" / "sample_data" / "cmms"


@pytest.mark.asyncio
async def test_stdio_server_lists_and_calls_tools(tmp_path: Path) -> None:
    config = tmp_path / "machina.yaml"
    config.write_text(
        "name: stdio-test\n"
        "sandbox: true\n"
        "connectors:\n"
        "  cmms:\n"
        "    type: generic_cmms\n"
        "    primary: true\n"
        "    settings:\n"
        f"      data_dir: {json.dumps(str(SAMPLE_CMMS))}\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    # Run the code under test, not whatever ``machina`` the interpreter
    # would otherwise import (e.g. an editable install of another checkout).
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "machina.mcp", "--config", str(config)],
        env=env,
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.serverInfo.name == "machina"

        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert "machina_list_assets" in names
        for tool in tools.tools:
            assert "ctx" not in tool.inputSchema.get("properties", {}), tool.name

        result = await session.call_tool("machina_list_assets", {})
        assert result.isError is False
        assert "P-201" in json.dumps([c.model_dump() for c in result.content])
