"""Integration tests for the REST mode of GenericCmmsConnector.

Exercises the full HTTP path via pytest-httpx mock fixtures — no real
network calls. Guards against regressions in the REST layer and confirms
that the schema-mapping feature applied to REST responses works the same
way it does for local JSON mode.

Requires: pytest-httpx (dev dep), httpx (cmms-rest extra).
"""

from __future__ import annotations

import httpx
import pytest

from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.domain.work_order import (
    FailureImpact,
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)
from machina.exceptions import ConnectorAuthError, ConnectorError

BASE_URL = "https://cmms.example.com/api"


@pytest.fixture
def rest_connector() -> GenericCmmsConnector:
    """Fresh REST-mode connector; not yet connected."""
    return GenericCmmsConnector(url=BASE_URL, api_key="test-key")


async def _connect_with_health(httpx_mock, conn: GenericCmmsConnector) -> None:
    """Helper: register the health-check response and connect."""
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE_URL}/health",
        status_code=200,
        json={"status": "ok"},
    )
    await conn.connect()


class TestRestConnection:
    """Connection lifecycle and auth."""

    @pytest.mark.asyncio
    async def test_connect_performs_health_check(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert requests[0].url == f"{BASE_URL}/health"
        assert requests[0].headers["Authorization"] == "Bearer test-key"

    @pytest.mark.asyncio
    async def test_connect_fails_without_api_key(self) -> None:
        conn = GenericCmmsConnector(url=BASE_URL, api_key="")
        with pytest.raises(ConnectorAuthError, match="API key"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_fails_on_non_200_health(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/health",
            status_code=503,
        )
        with pytest.raises(ConnectorError, match="health check failed"):
            await rest_connector.connect()


class TestRestReadAssets:
    """REST asset reads exercise GET /assets and GET /assets/{id}."""

    @pytest.mark.asyncio
    async def test_read_assets_list(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/assets",
            json=[
                {
                    "id": "P-201",
                    "name": "Cooling Water Pump",
                    "type": "rotating_equipment",
                    "criticality": "A",
                    "equipment_class_code": "PU",
                },
                {
                    "id": "COMP-301",
                    "name": "Air Compressor",
                    "type": "rotating_equipment",
                    "criticality": "A",
                    "equipment_class_code": "CO",
                },
            ],
        )
        assets = await rest_connector.read_assets()
        assert len(assets) == 2
        assert {a.id for a in assets} == {"P-201", "COMP-301"}
        # ISO 14224 field round-trips through the REST parser
        p201 = next(a for a in assets if a.id == "P-201")
        assert p201.equipment_class_code == "PU"

    @pytest.mark.asyncio
    async def test_get_asset_by_id(self, httpx_mock, rest_connector: GenericCmmsConnector) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/assets/P-201",
            json={
                "id": "P-201",
                "name": "Cooling Water Pump",
                "type": "rotating_equipment",
                "criticality": "A",
            },
        )
        asset = await rest_connector.get_asset("P-201")
        assert asset is not None
        assert asset.id == "P-201"
        assert asset.name == "Cooling Water Pump"

    @pytest.mark.asyncio
    async def test_read_assets_with_schema_mapping(self, httpx_mock) -> None:
        """A CMMS that calls the asset ID ``asset_id`` and the name
        ``display_name`` should still work via the schema_mapping feature."""
        conn = GenericCmmsConnector(
            url=BASE_URL,
            api_key="test-key",
            schema_mapping={
                "assets": {"asset_id": "id", "display_name": "name"},
            },
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/assets",
            json=[
                {
                    "asset_id": "P-201",
                    "display_name": "Cooling Water Pump",
                    "type": "rotating_equipment",
                    "criticality": "A",
                },
            ],
        )
        assets = await conn.read_assets()
        assert len(assets) == 1
        assert assets[0].id == "P-201"
        assert assets[0].name == "Cooling Water Pump"


class TestRestReadWorkOrders:
    """REST work-order reads exercise GET /work_orders with filters."""

    @pytest.mark.asyncio
    async def test_read_work_orders_sends_filter_params(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders?asset_id=P-201&status=created",
            json=[],
        )
        results = await rest_connector.read_work_orders(asset_id="P-201", status="created")
        assert results == []
        req = httpx_mock.get_requests()[-1]
        assert "asset_id=P-201" in str(req.url)
        assert "status=created" in str(req.url)

    @pytest.mark.asyncio
    async def test_read_work_orders_parses_iso_fields(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        """ISO 14224 WorkOrder fields (failure_impact, failure_cause) should
        round-trip through the REST parser just like local mode."""
        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders",
            json=[
                {
                    "id": "WO-2026-1842",
                    "type": "corrective",
                    "priority": "high",
                    "asset_id": "P-201",
                    "description": "Bearing wear",
                    "failure_mode": "BEAR-WEAR-01",
                    "failure_impact": "critical",
                    "failure_cause": "Expected wear and tear",
                },
            ],
        )
        results = await rest_connector.read_work_orders()
        assert len(results) == 1
        wo = results[0]
        assert wo.id == "WO-2026-1842"
        assert wo.failure_impact == FailureImpact.CRITICAL
        assert wo.failure_cause == "Expected wear and tear"


class TestRestCreateWorkOrder:
    """REST work-order creation posts JSON."""

    @pytest.mark.asyncio
    async def test_create_work_order_posts_json(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        wo_in = WorkOrder(
            id="WO-NEW",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="P-201",
            description="New test WO",
        )
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE_URL}/work_orders",
            status_code=201,
            json={
                "id": "WO-NEW",
                "type": "corrective",
                "priority": "high",
                "asset_id": "P-201",
                "description": "New test WO",
            },
        )
        wo_out = await rest_connector.create_work_order(wo_in)
        assert wo_out.id == "WO-NEW"

        # Verify the POST body was JSON-encoded WO payload
        post_req = [r for r in httpx_mock.get_requests() if r.method == "POST"][-1]
        assert post_req.headers["Content-Type"] == "application/json"
        # POST body should contain the input WO's id
        assert b"WO-NEW" in post_req.content

    @pytest.mark.asyncio
    async def test_create_work_order_raises_on_4xx(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        wo_in = WorkOrder(
            id="WO-BAD",
            type=WorkOrderType.CORRECTIVE,
            asset_id="P-999",
            description="Asset does not exist",
        )
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE_URL}/work_orders",
            status_code=422,
            json={"error": "unknown asset_id"},
        )
        with pytest.raises(httpx.HTTPStatusError):
            await rest_connector.create_work_order(wo_in)


class TestRequireHttpx:
    """The _require_httpx helper raises a clear error if httpx is missing."""

    def test_require_httpx_returns_httpx_when_available(self) -> None:
        from machina.connectors.cmms.generic import _require_httpx

        httpx_mod = _require_httpx()
        assert httpx_mod is httpx


class TestModernRestCmmsEndToEnd:
    """End-to-end integration test exercising auth + pagination + JMESPath.

    This simulates a modern CMMS REST API that:

    * Uses HTTP Basic authentication instead of Bearer tokens.
    * Wraps each response in a ``{"data": [...], "meta": {...}}`` envelope.
    * Uses offset/limit pagination with custom parameter names
      (``start`` and ``size``) and deeply nested item structures.

    It verifies that the ``GenericCmmsConnector`` can integrate such an API
    without any custom code beyond configuration.
    """

    BASE_URL = "https://modern-cmms.example.com/v2"

    @pytest.mark.asyncio
    async def test_modern_rest_cmms_end_to_end(self, httpx_mock) -> None:
        import base64

        from machina.connectors.cmms import (
            BasicAuth,
            GenericCmmsConnector,
            OffsetLimitPagination,
        )

        # Build the connector with Basic auth, custom pagination, and
        # JMESPath field extraction from a nested response shape.
        conn = GenericCmmsConnector(
            url=self.BASE_URL,
            auth=BasicAuth(username="svc", password="s3cret"),
            pagination=OffsetLimitPagination(
                limit_param="size",
                offset_param="start",
                page_size=2,
                items_path="data",
            ),
            schema_mapping={
                "assets": {
                    "_fields": {
                        "id": "equipment.id",
                        "name": "equipment.display_name",
                        "type": "meta.asset_type",
                        "criticality": "meta.criticality_class",
                        "equipment_class_code": "meta.iso_code",
                    },
                },
            },
        )

        # Health check (single-shot GET, no pagination)
        httpx_mock.add_response(
            method="GET",
            url=f"{self.BASE_URL}/health",
            status_code=200,
            json={"status": "ok"},
        )
        await conn.connect()

        # First page: 2 items (full page) → pagination keeps going
        httpx_mock.add_response(
            method="GET",
            url=f"{self.BASE_URL}/assets?size=2&start=0",
            json={
                "meta": {"total": 3},
                "data": [
                    {
                        "equipment": {"id": "P-201", "display_name": "Cooling Pump"},
                        "meta": {
                            "asset_type": "rotating_equipment",
                            "criticality_class": "A",
                            "iso_code": "PU",
                        },
                    },
                    {
                        "equipment": {
                            "id": "COMP-301",
                            "display_name": "Air Compressor",
                        },
                        "meta": {
                            "asset_type": "rotating_equipment",
                            "criticality_class": "A",
                            "iso_code": "CO",
                        },
                    },
                ],
            },
        )

        # Second page: 1 item (short page) → pagination stops here
        httpx_mock.add_response(
            method="GET",
            url=f"{self.BASE_URL}/assets?size=2&start=2",
            json={
                "meta": {"total": 3},
                "data": [
                    {
                        "equipment": {"id": "HEX-101", "display_name": "Heat Exchanger"},
                        "meta": {
                            "asset_type": "static_equipment",
                            "criticality_class": "B",
                            "iso_code": "HE",
                        },
                    },
                ],
            },
        )

        assets = await conn.read_assets()

        # All three assets were retrieved across both pages
        assert len(assets) == 3
        assert {a.id for a in assets} == {"P-201", "COMP-301", "HEX-101"}

        # Field mapping extracted nested values correctly
        pump = next(a for a in assets if a.id == "P-201")
        assert pump.name == "Cooling Pump"
        assert pump.equipment_class_code == "PU"
        assert pump.criticality.value == "A"

        hex_unit = next(a for a in assets if a.id == "HEX-101")
        assert hex_unit.equipment_class_code == "HE"
        assert hex_unit.criticality.value == "B"

        # Every HTTP request carried the expected Basic auth header
        expected_creds = base64.b64encode(b"svc:s3cret").decode("ascii")
        expected_auth = f"Basic {expected_creds}"
        for req in httpx_mock.get_requests():
            assert req.headers["Authorization"] == expected_auth

        await conn.disconnect()


# ---------------------------------------------------------------------------
# New REST methods: get, update, close, cancel work orders & maintenance plans
# ---------------------------------------------------------------------------


@pytest.fixture
def rest_connector_with_endpoints() -> GenericCmmsConnector:
    """REST-mode connector with all optional endpoints configured."""
    return GenericCmmsConnector(
        url=BASE_URL,
        api_key="test-key",
        endpoints={
            "get_work_order": {"path": "work_orders/{id}", "method": "GET"},
            "update_work_order": {"path": "work_orders/{id}", "method": "PATCH"},
            "read_maintenance_plans": {"path": "maintenance_plans"},
        },
    )


class TestRestGetWorkOrder:
    """REST get_work_order exercises GET /work_orders/{id}."""

    @pytest.mark.asyncio
    async def test_get_work_order_found(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={
                "id": "WO-001",
                "type": "corrective",
                "priority": "high",
                "asset_id": "P-201",
                "description": "Bearing replacement",
            },
        )
        wo = await conn.get_work_order("WO-001")
        assert wo is not None
        assert wo.id == "WO-001"

    @pytest.mark.asyncio
    async def test_get_work_order_not_found(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/NONEXISTENT",
            status_code=404,
        )
        wo = await conn.get_work_order("NONEXISTENT")
        assert wo is None


class TestRestUpdateWorkOrder:
    """REST update_work_order exercises PATCH /work_orders/{id}."""

    @pytest.mark.asyncio
    async def test_update_work_order_sends_patch(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        # PATCH response
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={"ok": True},
        )
        # Re-fetch after update
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={
                "id": "WO-001",
                "type": "corrective",
                "priority": "high",
                "asset_id": "P-201",
                "description": "Updated",
                "status": "assigned",
            },
        )
        updated = await conn.update_work_order(
            "WO-001", status=WorkOrderStatus.ASSIGNED, description="Updated"
        )
        assert updated.id == "WO-001"
        # Verify PATCH body contained the right fields
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        import json

        body = json.loads(patch_req.content)
        assert body["status"] == "assigned"
        assert body["description"] == "Updated"

    @pytest.mark.asyncio
    async def test_update_work_order_with_field_map(self, httpx_mock) -> None:
        """When field_map is configured, outgoing keys are remapped."""
        conn = GenericCmmsConnector(
            url=BASE_URL,
            api_key="test-key",
            endpoints={
                "get_work_order": {"path": "work_orders/{id}", "method": "GET"},
                "update_work_order": {
                    "path": "work_orders/{id}",
                    "method": "PATCH",
                    "field_map": {
                        "status": "wo_status",
                        "description": "wo_desc",
                    },
                },
            },
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={"ok": True},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={
                "id": "WO-001",
                "type": "corrective",
                "asset_id": "P-201",
                "description": "Mapped",
            },
        )
        await conn.update_work_order(
            "WO-001", status=WorkOrderStatus.ASSIGNED, description="Mapped"
        )
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        import json

        body = json.loads(patch_req.content)
        assert "wo_status" in body
        assert "wo_desc" in body
        assert "status" not in body


class TestRestCloseAndCancelWorkOrder:
    """close_work_order and cancel_work_order delegate to update."""

    @pytest.mark.asyncio
    async def test_close_delegates_to_update(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={"ok": True},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={
                "id": "WO-001",
                "type": "corrective",
                "asset_id": "P-201",
                "status": "closed",
            },
        )
        result = await conn.close_work_order("WO-001")
        assert result.id == "WO-001"
        # Verify the PATCH sent status=closed
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        import json

        body = json.loads(patch_req.content)
        assert body["status"] == "closed"

    @pytest.mark.asyncio
    async def test_cancel_delegates_to_update(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={"ok": True},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={
                "id": "WO-001",
                "type": "corrective",
                "asset_id": "P-201",
                "status": "cancelled",
            },
        )
        result = await conn.cancel_work_order("WO-001")
        assert result.id == "WO-001"


class TestRestReadMaintenancePlans:
    """REST read_maintenance_plans exercises GET /maintenance_plans."""

    @pytest.mark.asyncio
    async def test_read_maintenance_plans(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/maintenance_plans",
            json=[
                {
                    "id": "MP-001",
                    "asset_id": "P-201",
                    "name": "Quarterly Bearing Inspection",
                    "interval": {"months": 3},
                    "tasks": ["Check vibration levels"],
                    "active": True,
                },
            ],
        )
        plans = await conn.read_maintenance_plans()
        assert len(plans) == 1
        assert plans[0].id == "MP-001"
        assert plans[0].interval.months == 3


class TestRestGracefulDegradation:
    """Unconfigured endpoints raise ConnectorError with actionable message."""

    @pytest.mark.asyncio
    async def test_get_work_order_not_configured(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        with pytest.raises(ConnectorError, match="not configured"):
            await rest_connector.get_work_order("WO-001")

    @pytest.mark.asyncio
    async def test_update_work_order_not_configured(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        with pytest.raises(ConnectorError, match="not configured"):
            await rest_connector.update_work_order(
                "WO-001", description="Nope"
            )

    @pytest.mark.asyncio
    async def test_read_maintenance_plans_not_configured(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        with pytest.raises(ConnectorError, match="not configured"):
            await rest_connector.read_maintenance_plans()
