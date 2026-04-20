"""Tests for MCP resources — URI templates, static resources, read behavior."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.config.schema import MachinaConfig
from machina.mcp.resources import BUILTIN_FAILURE_TAXONOMY


class TestResourceRegistration:
    def test_resource_templates_registered(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        templates = server._resource_manager.list_templates()
        template_uris = [str(t.uri_template) for t in templates]
        assert "machina://v1/assets/{asset_id}" in template_uris
        assert "machina://v1/work-orders/{wo_id}" in template_uris

    def test_failure_taxonomy_is_static_resource(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        resources = server._resource_manager.list_resources()
        uris = [str(r.uri) for r in resources]
        assert "machina://v1/failure-taxonomy" in uris

    def test_template_names(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        templates = server._resource_manager.list_templates()
        names = [t.name for t in templates]
        assert "machina_asset" in names
        assert "machina_work_order" in names


class TestFailureTaxonomy:
    @pytest.mark.asyncio
    async def test_taxonomy_served_from_memory(self) -> None:
        """Failure taxonomy works with zero connectors configured."""
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        results = await server.read_resource("machina://v1/failure-taxonomy")
        content = list(results)
        assert len(content) == 1
        data = json.loads(content[0].content)
        assert isinstance(data, list)
        assert len(data) == len(BUILTIN_FAILURE_TAXONOMY)
        codes = [fm["code"] for fm in data]
        assert "BEAR-WEAR-01" in codes
        assert "SEAL-LEAK-01" in codes

    def test_taxonomy_has_required_fields(self) -> None:
        for fm in BUILTIN_FAILURE_TAXONOMY:
            assert "code" in fm
            assert "name" in fm
            assert "category" in fm
            assert "detection_methods" in fm


class TestAssetResource:
    @pytest.mark.asyncio
    async def test_read_existing_asset(self) -> None:
        from machina.domain.asset import Asset, AssetType
        from machina.mcp.server import build_server
        from machina.runtime import MachinaRuntime

        asset = Asset(id="EQ-1", name="Pump 1", type=AssetType.ROTATING_EQUIPMENT)
        mock_conn = MagicMock()
        mock_conn.capabilities = frozenset({"read_assets"})
        mock_conn.get_asset = AsyncMock(return_value=asset)
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()

        runtime = MachinaRuntime(connectors={"cmms": mock_conn})

        config = MachinaConfig()
        server = build_server(config)

        with patch.object(server, "get_context") as mock_ctx:
            mock_ctx.return_value = MagicMock()
            mock_ctx.return_value.request_context.lifespan_context = {"runtime": runtime}

            results = await server.read_resource("machina://v1/assets/EQ-1")
            content = list(results)
            assert len(content) == 1
            data = json.loads(content[0].content)
            assert data["id"] == "EQ-1"
            assert data["name"] == "Pump 1"

    @pytest.mark.asyncio
    async def test_read_nonexistent_asset(self) -> None:
        from machina.mcp.server import build_server
        from machina.runtime import MachinaRuntime

        mock_conn = MagicMock()
        mock_conn.capabilities = frozenset({"read_assets"})
        mock_conn.get_asset = AsyncMock(return_value=None)
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()

        runtime = MachinaRuntime(connectors={"cmms": mock_conn})

        config = MachinaConfig()
        server = build_server(config)

        with patch.object(server, "get_context") as mock_ctx:
            mock_ctx.return_value = MagicMock()
            mock_ctx.return_value.request_context.lifespan_context = {"runtime": runtime}

            results = await server.read_resource("machina://v1/assets/nonexistent")
            content = list(results)
            assert len(content) == 1
            data = json.loads(content[0].content)
            assert "error" in data


class TestWorkOrderResource:
    @pytest.mark.asyncio
    async def test_read_existing_work_order(self) -> None:
        from machina.domain.work_order import WorkOrder, WorkOrderType
        from machina.mcp.server import build_server
        from machina.runtime import MachinaRuntime

        wo = WorkOrder(id="WO-100", type=WorkOrderType.CORRECTIVE, asset_id="EQ-1")
        mock_conn = MagicMock()
        mock_conn.capabilities = frozenset({"read_assets"})
        mock_conn.get_work_order = AsyncMock(return_value=wo)
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()

        runtime = MachinaRuntime(connectors={"cmms": mock_conn})

        config = MachinaConfig()
        server = build_server(config)

        with patch.object(server, "get_context") as mock_ctx:
            mock_ctx.return_value = MagicMock()
            mock_ctx.return_value.request_context.lifespan_context = {"runtime": runtime}

            results = await server.read_resource("machina://v1/work-orders/WO-100")
            content = list(results)
            assert len(content) == 1
            data = json.loads(content[0].content)
            assert data["id"] == "WO-100"
