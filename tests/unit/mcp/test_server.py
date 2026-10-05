"""Tests for MCP server — build_server, tool registration, deprecation shim."""

from __future__ import annotations

import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from machina.config.schema import MachinaConfig
from machina.connectors.capabilities import Capability

SAMPLE_CMMS = Path(__file__).resolve().parents[3] / "examples" / "sample_data" / "cmms"


class TestBuildServer:
    def test_returns_fastmcp_instance(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        assert server.name == "machina"

    def test_no_tools_without_connectors(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        tool_names = [t.name for t in server._tool_manager.list_tools()]
        assert tool_names == []

    def test_list_assets_tool_registered_with_cmms(self) -> None:
        from machina.config.schema import ConnectorConfig
        from machina.mcp.server import build_server

        config = MachinaConfig(
            connectors={"cmms": ConnectorConfig(type="generic_cmms", settings={})}
        )
        server = build_server(config)
        tool_names = [t.name for t in server._tool_manager.list_tools()]
        assert "machina_list_assets" in tool_names

    def test_no_tool_exposes_ctx_as_an_argument(self) -> None:
        """Regression: tools annotated ``ctx: Any`` published ``ctx`` as a
        required input, so every tools/call from a real client failed
        validation. FastMCP must inject the context instead. Checked on every
        tool — all core tools (every capability) plus the vendor tools."""
        from machina.config.schema import McpConfig
        from machina.mcp.server import build_server
        from machina.mcp.tools import get_tools_for_capabilities
        from machina.mcp.tools_vendor import VENDOR_TOOLS

        server = build_server(MachinaConfig(mcp=McpConfig(enable_vendor_tools=True)))
        core = get_tools_for_capabilities(frozenset(Capability))
        for tool_fn in core:
            server.add_tool(tool_fn)

        tools = server._tool_manager.list_tools()
        assert {tool.name for tool in tools} == {fn.__name__ for fn in [*core, *VENDOR_TOOLS]}
        assert (len(core), len(VENDOR_TOOLS)) == (15, 2)
        for tool in tools:
            assert "ctx" not in tool.parameters.get("properties", {}), tool.name
            assert tool.context_kwarg == "ctx", tool.name

    @pytest.mark.asyncio
    async def test_real_client_calls_tool_without_ctx(self) -> None:
        """The same regression, end to end through an in-memory MCP client
        session: the client sees no ``ctx`` in any tool's input schema, and a
        call carrying only the tool's own arguments reaches the runtime."""
        from mcp.shared.memory import create_connected_server_and_client_session

        from machina.config.schema import ConnectorConfig, McpConfig
        from machina.mcp.server import build_server
        from machina.mcp.tools_vendor import VENDOR_TOOLS

        config = MachinaConfig(
            connectors={
                "cmms": ConnectorConfig(
                    type="generic_cmms", primary=True, settings={"data_dir": str(SAMPLE_CMMS)}
                )
            },
            mcp=McpConfig(enable_vendor_tools=True),
        )

        async with create_connected_server_and_client_session(build_server(config)) as client:
            tools = (await client.list_tools()).tools
            result = await client.call_tool("machina_get_asset", {"asset_id": "P-201"})

        assert {"machina_get_asset", *(fn.__name__ for fn in VENDOR_TOOLS)} <= {
            tool.name for tool in tools
        }
        leaked = [tool.name for tool in tools if "ctx" in tool.inputSchema.get("properties", {})]
        assert leaked == []
        assert result.isError is False, result.content
        assert result.structuredContent is not None
        assert result.structuredContent["id"] == "P-201"


class TestDeprecationShim:
    def test_mcp_server_access_warns_and_raises(self) -> None:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with pytest.raises(AttributeError, match=r"removed in v0\.3"):
                from machina import mcp

                mcp.MCPServer  # noqa: B018
            assert any("deprecated" in str(warning.message).lower() for warning in w)

    def test_unknown_attr_raises(self) -> None:
        from machina import mcp

        with pytest.raises(AttributeError, match="no attribute"):
            mcp.nonexistent_thing  # noqa: B018


class TestListAssetsTool:
    @pytest.mark.asyncio
    async def test_returns_error_without_cmms(self) -> None:
        from machina.mcp.tools import machina_list_assets
        from machina.runtime import MachinaRuntime

        runtime = MachinaRuntime()
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"runtime": runtime}
        result = await machina_list_assets(ctx)
        assert len(result) == 1
        assert "error" in result[0]

    @pytest.mark.asyncio
    async def test_returns_assets(self) -> None:
        from machina.domain.asset import Asset, AssetType
        from machina.mcp.tools import machina_list_assets
        from machina.runtime import MachinaRuntime

        mock_conn = MagicMock()
        mock_conn.capabilities = frozenset({Capability.READ_ASSETS})
        mock_conn.read_assets = AsyncMock(
            return_value=[
                Asset(id="P-001", name="Pump 1", type=AssetType.ROTATING_EQUIPMENT),
                Asset(id="V-001", name="Valve 1", type=AssetType.SAFETY),
            ]
        )

        runtime = MachinaRuntime(connectors={"cmms": mock_conn})
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"runtime": runtime}
        result = await machina_list_assets(ctx)
        assert len(result) == 2
        assert result[0]["id"] == "P-001"
        assert result[0]["type"] == "rotating_equipment"
        assert result[1]["id"] == "V-001"


class TestServe:
    @pytest.fixture(autouse=True)
    def _no_global_logging_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """serve() configures process-wide logging (structlog with cached
        loggers); doing that inside the test process would leak into every
        later test. The real effect is covered by the stdio subprocess test."""
        import machina.mcp.server as mcp_server

        monkeypatch.setattr(mcp_server, "_configure_logging", lambda config: None)

    def test_unknown_transport_raises(self) -> None:
        from machina.mcp.server import serve

        config = MachinaConfig()
        with pytest.raises(ValueError, match="Unknown transport"):
            serve(config, transport="grpc")

    def test_streamable_http_runs_uvicorn_with_host_and_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: serve() passed host/port to FastMCP.run(), which only
        accepts the transport, so the HTTP transport died with a TypeError."""
        import uvicorn

        from machina.mcp.server import serve

        monkeypatch.setenv("MACHINA_MCP_TOKENS_JSON", '{"' + "t" * 64 + '": "tester"}')
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))

        serve(MachinaConfig(), transport="streamable-http", host="127.0.0.1", port=8765)

        assert len(calls) == 1
        assert calls[0]["host"] == "127.0.0.1"
        assert calls[0]["port"] == 8765
        assert callable(calls[0]["app"])

    def test_stdio_runs_fastmcp_stdio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp.server.fastmcp import FastMCP

        from machina.mcp.server import serve

        # serve() exports MACHINA_MCP_STDIO for stdio; let monkeypatch undo it.
        monkeypatch.setenv("MACHINA_MCP_STDIO", "0")
        transports: list[str] = []
        monkeypatch.setattr(
            FastMCP, "run", lambda self, transport="stdio": transports.append(transport)
        )

        serve(MachinaConfig(), transport="stdio")

        assert transports == ["stdio"]

    def test_default_bind_is_loopback(self) -> None:
        import inspect

        from machina.mcp.server import build_http_app, serve

        assert inspect.signature(serve).parameters["host"].default == "127.0.0.1"
        assert inspect.signature(build_http_app).parameters["host"].default == "127.0.0.1"
