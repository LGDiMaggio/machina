"""Tests for vendor-specific MCP tools."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from machina.mcp.tools_vendor import VENDOR_TOOLS


class TestVendorToolsList:
    def test_vendor_tools_registered(self) -> None:
        names = [t.__name__ for t in VENDOR_TOOLS]
        assert "sap_pm_raw_iw38_notification" in names
        assert "maximo_raw_attribute_update" in names

    def test_vendor_tools_count(self) -> None:
        assert len(VENDOR_TOOLS) == 2


class TestVendorToolsNotRegisteredByDefault:
    def test_build_server_without_vendor_tools(self) -> None:
        from machina.config.schema import MachinaConfig
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        tool_names = [t.name for t in server._tool_manager.list_tools()]
        assert "sap_pm_raw_iw38_notification" not in tool_names
        assert "maximo_raw_attribute_update" not in tool_names

    def test_build_server_with_vendor_tools_enabled(self) -> None:
        from machina.config.schema import MachinaConfig, McpConfig
        from machina.mcp.server import build_server

        config = MachinaConfig(mcp=McpConfig(enable_vendor_tools=True))
        server = build_server(config)
        tool_names = [t.name for t in server._tool_manager.list_tools()]
        assert "sap_pm_raw_iw38_notification" in tool_names
        assert "maximo_raw_attribute_update" in tool_names


class TestSapRawNotificationNoConnector:
    @pytest.mark.asyncio
    async def test_returns_error_without_sap(self) -> None:
        from machina.mcp.tools_vendor import sap_pm_raw_iw38_notification
        from machina.runtime import MachinaRuntime

        runtime = MachinaRuntime()
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"runtime": runtime}
        result = await sap_pm_raw_iw38_notification(ctx, equipment_id="EQ-1", description="test")
        assert "error" in result


class TestMaximoRawUpdateNoConnector:
    @pytest.mark.asyncio
    async def test_returns_error_without_maximo(self) -> None:
        from machina.mcp.tools_vendor import maximo_raw_attribute_update
        from machina.runtime import MachinaRuntime

        runtime = MachinaRuntime()
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"runtime": runtime}
        result = await maximo_raw_attribute_update(ctx, resource_type="mxwo", resource_id="WO-1")
        assert "error" in result
