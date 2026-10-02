"""MCP server — FastMCP-based server with capability-driven tool registration.

Uses FastMCP from the MCP Python SDK for JSON-RPC transport. Tools are
auto-registered based on the capabilities declared by each configured
connector.

Two transports:

* ``stdio`` — one client per process. The FastMCP lifespan builds and
  connects a :class:`~machina.runtime.MachinaRuntime` when the session
  starts and disconnects it when the session ends.
* ``streamable-http`` — static bearer-token auth is required. Requests are
  handled statelessly, and the MCP SDK enters the FastMCP lifespan once per
  request in that mode, so the runtime is owned by the HTTP application
  instead: :func:`build_http_app` connects it once at startup, every request
  shares it, and it is disconnected at shutdown. The app also serves an
  unauthenticated ``GET /health``.
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
    from machina.runtime import MachinaRuntime

logger = structlog.get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# Scope every static bearer token carries; also gates the detailed /health.
_MCP_SCOPE = "mcp:use"


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
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> Any:
    """Build a FastMCP server wired to Machina connectors.

    Tools are auto-registered from the ``CAPABILITY_TO_TOOL`` map:
    only tools whose required capability is present across all
    configured connectors are registered.

    For ``streamable-http`` transport, bearer token auth, Host/Origin
    validation and the ``/health`` route are configured automatically. Run
    the HTTP transport through :func:`build_http_app` (or :func:`serve`),
    which owns a single runtime for the whole process.

    Args:
        config: Parsed Machina configuration.
        transport: Transport type (``"stdio"`` or ``"streamable-http"``).
        host: Bind address recorded in the server settings (HTTP only).
        port: Port recorded in the server settings (HTTP only).

    Returns:
        A ``FastMCP`` instance ready to run.
    """
    return _build_server(config, transport=transport, host=host, port=port, shared=None)


def _build_server(
    config: MachinaConfig,
    *,
    transport: str,
    host: str,
    port: int,
    shared: dict[str, Any] | None,
) -> Any:
    """Build the FastMCP server; ``shared`` carries an app-owned runtime (HTTP)."""
    fastmcp_cls = _require_fastmcp()

    @asynccontextmanager
    async def machina_lifespan(server: Any) -> AsyncIterator[dict[str, Any]]:
        app_runtime = shared.get("runtime") if shared is not None else None
        if app_runtime is not None:
            # Streamable HTTP: the app lifespan connected one runtime for the
            # whole process; this per-request lifespan only hands it out.
            set_sandbox_mode(app_runtime.sandbox_mode)
            yield {"runtime": app_runtime}
            return

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
        kwargs["host"] = host
        kwargs["port"] = port

    server = fastmcp_cls("machina", **kwargs)
    _register_tools(server, config)
    _register_resources(server)
    _register_prompts(server)
    if transport == "streamable-http":
        _register_health_route(server, shared)
    return server


def build_http_app(
    config: MachinaConfig,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> Any:
    """Build the ASGI app for the streamable-http transport.

    The app connects one :class:`~machina.runtime.MachinaRuntime` when it
    starts, shares it with every MCP request (so connectors connect once and
    per-connector state such as write locks is shared), and disconnects it
    when it stops.

    Args:
        config: Parsed Machina configuration.
        host: Bind address recorded in the server settings.
        port: Port recorded in the server settings.

    Returns:
        A Starlette application to run under an ASGI server (e.g. uvicorn).
    """
    shared: dict[str, Any] = {}
    server = _build_server(
        config,
        transport="streamable-http",
        host=host,
        port=port,
        shared=shared,
    )
    app = server.streamable_http_app()
    session_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def runtime_lifespan(app_: Any) -> AsyncIterator[None]:
        from machina.runtime import MachinaRuntime

        runtime = MachinaRuntime.from_config(config)
        set_sandbox_mode(runtime.sandbox_mode)
        await runtime.connect_all()
        shared["runtime"] = runtime
        logger.info(
            "mcp_server_ready",
            transport="streamable-http",
            connectors=list(runtime.connectors.keys()),
            sandbox=runtime.sandbox_mode,
        )
        try:
            async with session_lifespan(app_):
                yield
        finally:
            shared.pop("runtime", None)
            await runtime.disconnect_all()

    app.router.lifespan_context = runtime_lifespan
    return app


def _build_http_kwargs(config: MachinaConfig) -> dict[str, Any]:
    """Build FastMCP constructor kwargs for authenticated HTTP transport."""
    from machina.config.schema import McpConfig
    from machina.mcp.auth import build_verifier

    verifier = build_verifier(config)

    from mcp.server.auth.settings import (  # type: ignore[import-not-found,unused-ignore]
        AuthSettings,
    )
    from mcp.server.transport_security import (  # type: ignore[import-not-found,unused-ignore]
        TransportSecuritySettings,
    )

    mcp_cfg = getattr(config, "mcp", None) or McpConfig()

    auth_settings = AuthSettings(
        issuer_url="https://machina.local",
        resource_server_url="https://machina.local",
        required_scopes=[_MCP_SCOPE],
    )

    transport_security = TransportSecuritySettings(
        allowed_hosts=list(mcp_cfg.allowed_hosts),
        allowed_origins=list(mcp_cfg.allowed_origins),
    )

    return {
        "token_verifier": verifier,
        "auth": auth_settings,
        "transport_security": transport_security,
        "stateless_http": True,
        "json_response": False,
    }


def _collect_capabilities(config: MachinaConfig) -> frozenset[Capability]:
    """Gather the union of all capabilities from enabled connectors.

    Reads class-level capabilities where available. For connectors that
    compute capabilities at instance level (e.g. GenericCmmsConnector),
    falls back to instantiation.
    """
    from machina.runtime import _CONNECTOR_FACTORIES, _import_class

    all_caps: set[Capability] = set()
    for _name, conn_cfg in config.connectors.items():
        if not conn_cfg.enabled:
            continue
        factory_path = _CONNECTOR_FACTORIES.get(conn_cfg.type)
        if factory_path is None:
            continue
        try:
            connector_cls = _import_class(factory_path)
            cls_caps = getattr(connector_cls, "capabilities", None)
            if isinstance(cls_caps, frozenset):
                all_caps.update(cls_caps)
            else:
                connector = connector_cls(**conn_cfg.settings)
                all_caps.update(connector.capabilities)
        except Exception:
            continue
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
# Health endpoint
# ---------------------------------------------------------------------------


def _health_payload(runtime: MachinaRuntime | None, *, authorized: bool) -> dict[str, Any]:
    """Build the ``/health`` body.

    Anyone gets ``{"status": "healthy"}``. An authorized caller additionally
    gets the configured connector names, the sandbox mode and the installed
    ``machina-ai`` version — but only while a runtime is live.
    """
    body: dict[str, Any] = {"status": "healthy"}
    if authorized and runtime is not None:
        from machina import __version__

        body.update(
            {
                "connectors": list(runtime.connectors.keys()),
                "sandbox_mode": runtime.sandbox_mode,
                "version": __version__,
            }
        )
    return body


def _register_health_route(server: Any, shared: dict[str, Any] | None) -> None:
    """Mount ``GET /health`` on the HTTP transport.

    The route itself requires no token (container health checks call it
    bare). The SDK's authentication middleware still runs for every route,
    so a request carrying a valid bearer token arrives with its scopes on
    ``request.auth`` — the detailed payload keys off those, keeping
    ``/health`` and ``/mcp`` on one authorization rule.
    """
    from starlette.responses import JSONResponse  # type: ignore[import-not-found,unused-ignore]

    @server.custom_route("/health", methods=["GET"])  # type: ignore[untyped-decorator,unused-ignore]
    async def health(request: Any) -> Any:
        scopes = getattr(request.scope.get("auth"), "scopes", ()) or ()
        runtime = shared.get("runtime") if shared is not None else None
        return JSONResponse(_health_payload(runtime, authorized=_MCP_SCOPE in scopes))


async def health_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Minimal standalone ASGI health endpoint.

    Kept for callers that mount it themselves; the streamable-http app built
    by :func:`build_http_app` serves ``/health`` on its own.

    Unauthenticated: returns ``{"status": "healthy"}`` only.
    Authenticated (bearer token verified by ``scope["app"]._token_verifier``):
    adds connector names, sandbox mode and the installed package version
    read from ``scope["app"]._runtime_ref``.
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

    runtime = None
    authorized = False
    if auth_header.startswith("Bearer ") and hasattr(scope.get("app"), "_runtime_ref"):
        runtime = scope["app"]._runtime_ref
        verifier = getattr(scope.get("app"), "_token_verifier", None)
        token_value = auth_header[7:]
        if verifier and token_value:
            authorized = await verifier.verify_token(token_value) is not None

    response_body = json.dumps(_health_payload(runtime, authorized=authorized)).encode()
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
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    """Build and run the MCP server (blocking).

    Args:
        config: Parsed Machina configuration.
        transport: ``"stdio"`` or ``"streamable-http"``.
        host: Bind address for HTTP transport (default ``127.0.0.1``; use
            ``0.0.0.0`` to listen on every interface, e.g. in a container).
        port: Port for HTTP transport.

    Raises:
        ValueError: If ``transport`` is not supported.
    """
    if transport == "stdio":
        # stdout carries the JSON-RPC stream, so every log line must go to
        # stderr: the env flag tells configure_logging() to route there.
        os.environ["MACHINA_MCP_STDIO"] = "1"
        _configure_logging(config)
        build_server(config, transport="stdio").run(transport="stdio")
    elif transport == "streamable-http":
        import uvicorn  # type: ignore[import-not-found,unused-ignore]

        _configure_logging(config)
        app = build_http_app(config, host=host, port=port)
        uvicorn.run(app, host=host, port=port, log_level="info")
    else:
        msg = f"Unknown transport: {transport!r}. Use 'stdio' or 'streamable-http'."
        raise ValueError(msg)


def _configure_logging(config: MachinaConfig) -> None:
    """Configure structured logging for the server process (``logging.level``)."""
    from machina.observability.logging import configure_logging

    configure_logging(level=str(config.logging.get("level", "INFO")))
