"""Tests for capability-driven tool auto-registration and routing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from machina.config.schema import ConnectorConfig, MachinaConfig
from machina.connectors.capabilities import Capability
from machina.exceptions import ConnectorError
from machina.runtime import MachinaRuntime


class TestAutoRegistration:
    def test_cmms_only_registers_cmms_tools(self) -> None:
        """Plant with SapPm + DocumentStore — only matching tools registered."""
        from machina.mcp.server import build_server

        config = MachinaConfig(
            connectors={
                "cmms": ConnectorConfig(type="generic_cmms", settings={}),
            }
        )
        server = build_server(config)
        tool_names = {t.name for t in server._tool_manager.list_tools()}
        assert "machina_list_assets" in tool_names
        assert "machina_create_work_order" in tool_names
        assert "machina_list_work_orders" in tool_names
        # No IoT connector → no sensor tools
        assert "machina_get_sensor_reading" not in tool_names

    def test_no_connectors_registers_no_tools(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        tool_names = [t.name for t in server._tool_manager.list_tools()]
        assert tool_names == []

    def test_doc_store_registers_search_manuals(self) -> None:
        # DocumentStore needs doc_paths to init; using a mock approach via config
        mock_conn = MagicMock()
        mock_conn.capabilities = frozenset(
            {Capability.SEARCH_DOCUMENTS, Capability.RETRIEVE_SECTION}
        )
        from machina.mcp.tools import get_tools_for_capabilities

        tools = get_tools_for_capabilities(mock_conn.capabilities)
        tool_names = [t.__name__ for t in tools]
        assert "machina_search_manuals" in tool_names


class TestCapabilityToToolMap:
    def test_all_capabilities_have_tools(self) -> None:
        from machina.mcp.tools import CAPABILITY_TO_TOOL

        for cap, tools in CAPABILITY_TO_TOOL.items():
            assert len(tools) >= 1, f"Capability {cap} has no tools"

    def test_dedup_across_capabilities(self) -> None:
        from machina.mcp.tools import get_tools_for_capabilities

        caps = frozenset(
            {Capability.READ_ASSETS, Capability.READ_WORK_ORDERS, Capability.GET_WORK_ORDER}
        )
        tools = get_tools_for_capabilities(caps)
        names = [t.__name__ for t in tools]
        assert len(names) == len(set(names)), "Duplicate tools registered"


class TestPrimaryCmmsRouting:
    def test_primary_flag_respected(self) -> None:
        conn_a = MagicMock()
        conn_a.capabilities = frozenset({Capability.READ_ASSETS})
        conn_b = MagicMock()
        conn_b.capabilities = frozenset({Capability.READ_ASSETS})
        runtime = MachinaRuntime(
            connectors={"sap": conn_a, "maximo": conn_b},
            primary_cmms_name="maximo",
        )
        assert runtime.get_primary_cmms() is conn_b

    def test_fallback_without_primary(self) -> None:
        conn = MagicMock()
        conn.capabilities = frozenset({Capability.READ_ASSETS})
        runtime = MachinaRuntime(connectors={"cmms": conn})
        assert runtime.get_primary_cmms() is conn

    def test_multiple_primary_raises(self) -> None:
        config = MachinaConfig(
            connectors={
                "sap": ConnectorConfig(type="generic_cmms", primary=True, settings={}),
                "maximo": ConnectorConfig(type="generic_cmms", primary=True, settings={}),
            }
        )
        with pytest.raises(ConnectorError, match="Multiple connectors marked primary"):
            MachinaRuntime.from_config(config)


class TestCapabilityRegistrationRegression:
    """Regression guard: walk all in-tree connector capabilities and verify tool coverage."""

    def test_all_cmms_capabilities_covered(self) -> None:
        from machina.mcp.tools import CAPABILITY_TO_TOOL

        cmms_caps = {
            Capability.READ_ASSETS,
            Capability.READ_WORK_ORDERS,
            Capability.GET_WORK_ORDER,
            Capability.CREATE_WORK_ORDER,
            Capability.UPDATE_WORK_ORDER,
            Capability.READ_SPARE_PARTS,
            Capability.READ_MAINTENANCE_PLANS,
        }
        for cap in cmms_caps:
            assert cap in CAPABILITY_TO_TOOL, f"CMMS capability {cap} missing from tool map"

    def test_doc_capabilities_covered(self) -> None:
        from machina.mcp.tools import CAPABILITY_TO_TOOL

        assert Capability.SEARCH_DOCUMENTS in CAPABILITY_TO_TOOL

    def test_iot_capabilities_covered(self) -> None:
        from machina.mcp.tools import CAPABILITY_TO_TOOL

        assert Capability.GET_LATEST_READING in CAPABILITY_TO_TOOL
