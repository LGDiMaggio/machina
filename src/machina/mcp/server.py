"""MCP server — FastMCP-based server with Plant lifespan.

Replaces the v0.2 placeholder stub. Uses FastMCP from the MCP Python
SDK for JSON-RPC transport, with a lifespan that connects all
configured connectors at startup and disconnects on shutdown.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import structlog

from machina.exceptions import ConnectorError

if TYPE_CHECKING:
    from machina.config.schema import MachinaConfig

logger = structlog.get_logger(__name__)


def _require_fastmcp() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ConnectorError(
            "The MCP SDK is required. Install with: pip install machina-ai[mcp]"
        ) from exc
    return FastMCP


def build_server(config: MachinaConfig) -> Any:
    """Build a FastMCP server wired to Machina connectors.

    Args:
        config: Parsed Machina configuration.

    Returns:
        A ``FastMCP`` instance ready to run.
    """
    fastmcp_cls = _require_fastmcp()

    @asynccontextmanager
    async def machina_lifespan(server: Any) -> AsyncIterator[dict[str, Any]]:
        from machina.runtime import MachinaRuntime

        runtime = MachinaRuntime.from_config(config)
        await runtime.connect_all()
        logger.info(
            "mcp_server_ready",
            connectors=list(runtime.connectors.keys()),
            sandbox=runtime.sandbox_mode,
        )
        try:
            yield {"runtime": runtime}
        finally:
            await runtime.disconnect_all()

    server = fastmcp_cls("machina", lifespan=machina_lifespan)
    _register_tools(server)
    return server


def _register_tools(server: Any) -> None:
    """Register proof-of-life tools on the server."""
    from machina.mcp.tools import machina_list_assets

    server.add_tool(machina_list_assets)


def serve(
    config: MachinaConfig,
    *,
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = 8000,
) -> None:
    """Build and run the MCP server (blocking).

    Args:
        config: Parsed Machina configuration.
        transport: ``"stdio"`` or ``"streamable-http"``.
        host: Host for HTTP transport.
        port: Port for HTTP transport.
    """
    if transport == "stdio":
        os.environ["MACHINA_MCP_STDIO"] = "1"

    server = build_server(config)

    if transport == "stdio":
        server.run(transport="stdio")
    elif transport == "streamable-http":
        server.run(transport="streamable-http", host=host, port=port)
    else:
        msg = f"Unknown transport: {transport!r}. Use 'stdio' or 'streamable-http'."
        raise ValueError(msg)
