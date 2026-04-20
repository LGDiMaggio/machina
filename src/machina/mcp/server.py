"""MCP server — FastMCP-based server with capability-driven tool registration.

Uses FastMCP from the MCP Python SDK for JSON-RPC transport, with a
lifespan that connects all configured connectors at startup and
disconnects on shutdown.  Tools are auto-registered based on the
capabilities declared by each connector.

For streamable-http transport, static bearer token auth is required.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import structlog

from machina.connectors.base import set_sandbox_mode
from machina.exceptions import ConnectorError

if TYPE_CHECKING:
    from machina.config.schema import MachinaConfig
    from machina.connectors.capabilities import Capability

logger = structlog.get_logger(__name__)


def _require_fastmcp() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:
        raise ConnectorError(
            "The MCP SDK is required. Install with: pip install machina-ai[mcp]"
        ) from exc
    return FastMCP


def build_server(
    config: MachinaConfig,
    *,
    transport: str = "stdio",
) -> Any:
    """Build a FastMCP server wired to Machina connectors.

    Tools are auto-registered from the ``CAPABILITY_TO_TOOL`` map:
    only tools whose required capability is present across all
    configured connectors are registered.

    For ``streamable-http`` transport, bearer token auth and origin
    validation are configured automatically.

    Args:
        config: Parsed Machina configuration.
        transport: Transport type (``"stdio"`` or ``"streamable-http"``).

    Returns:
        A ``FastMCP`` instance ready to run.
    """
    fastmcp_cls = _require_fastmcp()

    @asynccontextmanager
    async def machina_lifespan(server: Any) -> AsyncIterator[dict[str, Any]]:
        from machina.runtime import MachinaRuntime

        runtime = MachinaRuntime.from_config(config)
        set_sandbox_mode(runtime.sandbox_mode)
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

    kwargs: dict[str, Any] = {"lifespan": machina_lifespan}

    if transport == "streamable-http":
        kwargs.update(_build_http_kwargs(config))

    server = fastmcp_cls("machina", **kwargs)
    _register_tools(server, config)
    _register_resources(server)
    _register_prompts(server)
    return server


def _build_http_kwargs(config: MachinaConfig) -> dict[str, Any]:
    """Build FastMCP constructor kwargs for authenticated HTTP transport."""
    from machina.mcp.auth import build_verifier

    verifier = build_verifier(config)

    from mcp.server.auth.settings import (  # type: ignore[import-not-found,unused-ignore]
        AuthSettings,
    )
    from mcp.server.transport_security import (  # type: ignore[import-not-found,unused-ignore]
        TransportSecuritySettings,
    )

    mcp_cfg = getattr(config, "mcp", None)
    allowed_origins = getattr(
        mcp_cfg, "allowed_origins", ["http://localhost", "https://localhost"]
    )

    auth_settings = AuthSettings(
        issuer_url="https://machina.local",
        resource_server_url="https://machina.local",
        required_scopes=["mcp:use"],
    )

    transport_security = TransportSecuritySettings(
        allowed_origins=allowed_origins,
    )

    return {
        "token_verifier": verifier,
        "auth": auth_settings,
        "transport_security": transport_security,
        "stateless_http": True,
        "json_response": False,
    }


def _collect_capabilities(config: MachinaConfig) -> frozenset[Capability]:
    """Gather the union of all capabilities from enabled connectors."""
    from machina.runtime import MachinaRuntime

    runtime = MachinaRuntime.from_config(config)
    all_caps: set[Capability] = set()
    for _name, conn in runtime.connectors.items():
        all_caps.update(conn.capabilities)
    return frozenset(all_caps)


def _register_tools(server: Any, config: MachinaConfig) -> None:
    """Auto-register domain tools based on connector capabilities."""
    from machina.mcp.tools import get_tools_for_capabilities

    capabilities = _collect_capabilities(config)
    tools = get_tools_for_capabilities(capabilities)
    for tool_fn in tools:
        server.add_tool(tool_fn)
    logger.info(
        "mcp_tools_registered",
        tool_count=len(tools),
        tool_names=[t.__name__ for t in tools],
    )

    enable_vendor = getattr(getattr(config, "mcp", None), "enable_vendor_tools", False)
    if enable_vendor:
        from machina.mcp.tools_vendor import VENDOR_TOOLS

        for tool_fn in VENDOR_TOOLS:
            server.add_tool(tool_fn)
        logger.info(
            "mcp_vendor_tools_registered",
            tool_count=len(VENDOR_TOOLS),
            tool_names=[t.__name__ for t in VENDOR_TOOLS],
        )


def _register_resources(server: Any) -> None:
    """Register MCP resources (versioned URI scheme)."""
    from machina.mcp.resources import register_resources

    register_resources(server)


def _register_prompts(server: Any) -> None:
    """Register MCP prompt templates."""
    from machina.mcp.prompts import register_prompts

    register_prompts(server)


# ---------------------------------------------------------------------------
# Health endpoint (ASGI)
# ---------------------------------------------------------------------------


async def health_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Minimal ASGI health endpoint.

    Unauthenticated: returns ``{"status": "healthy"}`` only.
    Authenticated (bearer token): returns full payload with connector
    details, sandbox mode, and version.
    """
    if scope["type"] != "http" or scope["path"] != "/health":
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"Not Found"})
        return

    auth_header = ""
    for header_name, header_value in scope.get("headers", []):
        if header_name == b"authorization":
            auth_header = header_value.decode()
            break

    body: dict[str, Any] = {"status": "healthy"}

    if auth_header.startswith("Bearer ") and hasattr(scope.get("app"), "_runtime_ref"):
        runtime = scope["app"]._runtime_ref
        if runtime:
            body.update(
                {
                    "connectors": list(runtime.connectors.keys()),
                    "sandbox_mode": runtime.sandbox_mode,
                    "version": "0.3.0",
                }
            )

    response_body = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"application/json"],
            ],
        }
    )
    await send({"type": "http.response.body", "body": response_body})


# ---------------------------------------------------------------------------
# serve() — build and run
# ---------------------------------------------------------------------------


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

    server = build_server(config, transport=transport)

    if transport == "stdio":
        server.run(transport="stdio")
    elif transport == "streamable-http":
        server.run(transport="streamable-http", host=host, port=port)
    else:
        msg = f"Unknown transport: {transport!r}. Use 'stdio' or 'streamable-http'."
        raise ValueError(msg)
