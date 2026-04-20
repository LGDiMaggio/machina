"""Tests for MCP server — build_server, tool registration, deprecation shim."""

from __future__ import annotations

import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest

from machina.config.schema import MachinaConfig
from machina.connectors.capabilities import Capability


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
    def test_unknown_transport_raises(self) -> None:
        from machina.mcp.server import serve

        config = MachinaConfig()
        with pytest.raises(ValueError, match="Unknown transport"):
            serve(config, transport="grpc")
