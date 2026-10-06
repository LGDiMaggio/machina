"""Integration tests for UpKeepConnector REST operations.

All HTTP traffic is intercepted by pytest-httpx — no real UpKeep API calls.
"""

from __future__ import annotations

import pytest

from machina.connectors.cmms.upkeep import UpKeepConnector
from machina.domain.asset import Asset
from machina.domain.maintenance_plan import MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import ConnectorAuthError, ConnectorError

BASE = "https://api.onupkeep.com"


@pytest.fixture
def connector() -> UpKeepConnector:
    return UpKeepConnector(api_key="test-token")


async def _connect(httpx_mock, conn: UpKeepConnector) -> None:
    """Register the auth-check response and connect."""
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/api/v2/users?limit=1",
        status_code=200,
        json={"results": [{"id": "u1"}]},
    )
    await conn.connect()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class TestConnection:
    @pytest.mark.asyncio
    async def test_connect_success(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        assert connector._connected
        req = httpx_mock.get_requests()[0]
        assert req.headers["Session-Token"] == "test-token"

    @pytest.mark.asyncio
    async def test_connect_invalid_key(self, httpx_mock) -> None:
        conn = UpKeepConnector(api_key="bad")
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/users?limit=1",
            status_code=401,
        )
        with pytest.raises(ConnectorAuthError, match="invalid"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_server_error(self, httpx_mock) -> None:
        conn = UpKeepConnector(api_key="tok")
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/users?limit=1",
            status_code=500,
        )
        with pytest.raises(ConnectorError, match="500"):
            await conn.connect()


# ---------------------------------------------------------------------------
# Read assets
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_healthy(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        health = await connector.health_check()
        assert health.status.value == "healthy"
        assert health.details["url"] == BASE


class TestReadAssets:
    @pytest.mark.asyncio
    async def test_read_assets_single_page(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/assets?limit=100&offset=0",
            json={
                "results": [
                    {"id": "a1", "name": "Pump A", "category": "Rotating Equipment"},
                    {"id": "a2", "name": "Heater B", "category": "HVAC"},
                ],
            },
        )
        assets = await connector.read_assets()
        assert len(assets) == 2
        assert all(isinstance(a, Asset) for a in assets)
        assert assets[0].id == "a1"
        assert assets[1].name == "Heater B"

    @pytest.mark.asyncio
    async def test_read_assets_pagination(self, httpx_mock, connector: UpKeepConnector) -> None:
        """Two pages: first page full (100 items), second page partial."""
        await _connect(httpx_mock, connector)
        page1 = [{"id": f"a{i}", "name": f"Asset {i}"} for i in range(100)]
        page2 = [{"id": "a100", "name": "Asset 100"}]
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/assets?limit=100&offset=0",
            json={"results": page1},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/assets?limit=100&offset=100",
            json={"results": page2},
        )
        assets = await connector.read_assets()
        assert len(assets) == 101

    @pytest.mark.asyncio
    async def test_read_assets_empty(self, httpx_mock, connector: UpKeepConnector) -> None:
        """A zero-result first page must stop pagination with no further requests."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/assets?limit=100&offset=0",
            json={"results": []},
        )
        assets = await connector.read_assets()
        assert assets == []
        # Exactly 2 requests: the connect health check + the single read_assets page.
        assert len(httpx_mock.get_requests()) == 2


# ---------------------------------------------------------------------------
# Read work orders
# ---------------------------------------------------------------------------


class TestReadWorkOrders:
    @pytest.mark.asyncio
    async def test_read_work_orders(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders?limit=100&offset=0",
            json={
                "results": [
                    {
                        "id": "wo1",
                        "title": "Fix pump",
                        "priority": 2,  # UpKeep 0-3 scale: 2 = HIGH
                        "status": "open",
                        "assetId": "a1",
                        "createdAt": "2025-06-01T10:00:00Z",
                        "updatedAt": "2025-06-01T12:00:00Z",
                    },
                ],
            },
        )
        wos = await connector.read_work_orders()
        assert len(wos) == 1
        assert isinstance(wos[0], WorkOrder)
        assert wos[0].priority == Priority.HIGH

    @pytest.mark.asyncio
    async def test_read_work_orders_filtered(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders?limit=100&offset=0&asset=a1&status=complete",
            json={"results": []},
        )
        wos = await connector.read_work_orders(asset_id="a1", status="complete")
        assert wos == []


# ---------------------------------------------------------------------------
# Create work order
# ---------------------------------------------------------------------------


class TestReadWorkOrdersErrors:
    @pytest.mark.asyncio
    async def test_paginate_auth_failure(self, httpx_mock, connector: UpKeepConnector) -> None:
        """401 during a paginated GET must raise ConnectorAuthError."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders?limit=100&offset=0",
            status_code=401,
        )
        with pytest.raises(ConnectorAuthError):
            await connector.read_work_orders()

    @pytest.mark.asyncio
    async def test_paginate_server_error(self, httpx_mock, connector: UpKeepConnector) -> None:
        """500 during a paginated GET must raise ConnectorError."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders?limit=100&offset=0",
            status_code=500,
        )
        with pytest.raises(ConnectorError, match="GET /api/v2/work-orders failed"):
            await connector.read_work_orders()


class TestCreateWorkOrder:
    @pytest.mark.asyncio
    async def test_create_work_order(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/api/v2/work-orders",
            status_code=201,
            json={
                "result": {
                    "id": "wo-new",
                    "title": "Replace bearing",
                    "priority": 2,  # UpKeep 0-3 scale: 2 = HIGH
                    "status": "open",
                    "assetId": "a1",
                    "createdAt": "2025-07-01T08:00:00Z",
                    "updatedAt": "2025-07-01T08:00:00Z",
                },
            },
        )
        from datetime import UTC, datetime

        wo = WorkOrder(
            id="TEMP",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="a1",
            description="Replace bearing",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        created = await connector.create_work_order(wo)
        assert created.id == "wo-new"
        assert created.description == "Replace bearing"
        req = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        assert req.headers["Session-Token"] == "test-token"
        # HIGH must be sent on the 0-3 UpKeep scale as 2, not 3.
        import json

        payload = json.loads(req.content)
        assert payload["priority"] == 2

    @pytest.mark.asyncio
    async def test_create_work_order_auth_failure(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="POST", url=f"{BASE}/api/v2/work-orders", status_code=401)
        from datetime import UTC, datetime

        wo = WorkOrder(
            id="T",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="a1",
            description="x",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        with pytest.raises(ConnectorAuthError):
            await connector.create_work_order(wo)

    @pytest.mark.asyncio
    async def test_create_work_order_error(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="POST", url=f"{BASE}/api/v2/work-orders", status_code=422)
        from datetime import UTC, datetime

        wo = WorkOrder(
            id="T",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="a1",
            description="x",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        with pytest.raises(ConnectorError, match="create work order failed"):
            await connector.create_work_order(wo)


# ---------------------------------------------------------------------------
# Read spare parts
# ---------------------------------------------------------------------------


class TestReadSpareParts:
    @pytest.mark.asyncio
    async def test_read_spare_parts(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/parts?limit=100&offset=0",
            json={
                "results": [
                    {"id": "p1", "name": "Bearing", "quantity": 10, "cost": 30.0},
                ],
            },
        )
        parts = await connector.read_spare_parts()
        assert len(parts) == 1
        assert isinstance(parts[0], SparePart)
        assert parts[0].sku == "p1"

    @pytest.mark.asyncio
    async def test_read_spare_parts_sku_filter(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """sku must filter in-memory after fetching all parts."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/parts?limit=100&offset=0",
            json={
                "results": [
                    {"id": "p1", "partNumber": "SKF-6205", "name": "Bearing"},
                    {"id": "p2", "partNumber": "SKF-7309", "name": "Angular bearing"},
                ],
            },
        )
        parts = await connector.read_spare_parts(sku="SKF-6205")
        assert len(parts) == 1
        assert parts[0].sku == "SKF-6205"


# ---------------------------------------------------------------------------
# Read maintenance plans
# ---------------------------------------------------------------------------


class TestReadMaintenancePlans:
    @pytest.mark.asyncio
    async def test_read_maintenance_plans(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/preventive-maintenance?limit=100&offset=0",
            json={
                "results": [
                    {
                        "id": "pm-1",
                        "title": "Weekly inspection",
                        "assetId": "a1",
                        "frequencyDays": 7,
                        "status": "active",
                    },
                ],
            },
        )
        plans = await connector.read_maintenance_plans()
        assert len(plans) == 1
        assert isinstance(plans[0], MaintenancePlan)
        assert plans[0].interval.days == 7


# ---------------------------------------------------------------------------
# Get single work order
# ---------------------------------------------------------------------------


class TestGetWorkOrder:
    @pytest.mark.asyncio
    async def test_get_work_order_found(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders/wo1",
            json={
                "result": {
                    "id": "wo1",
                    "title": "Fix pump",
                    "priority": 2,
                    "status": "open",
                    "assetId": "a1",
                    "createdAt": "2025-06-01T10:00:00Z",
                    "updatedAt": "2025-06-01T12:00:00Z",
                },
            },
        )
        wo = await connector.get_work_order("wo1")
        assert wo is not None
        assert wo.id == "wo1"

    @pytest.mark.asyncio
    async def test_get_work_order_not_found(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET", url=f"{BASE}/api/v2/work-orders/missing", status_code=404
        )
        wo = await connector.get_work_order("missing")
        assert wo is None


# ---------------------------------------------------------------------------
# Update work order
# ---------------------------------------------------------------------------


class TestUpdateWorkOrder:
    @pytest.mark.asyncio
    async def test_update_work_order_status(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE}/api/v2/work-orders/wo1",
            status_code=200,
            json={
                "result": {
                    "id": "wo1",
                    "title": "Fix pump",
                    "priority": 2,
                    "status": "complete",
                    "assetId": "a1",
                    "createdAt": "2025-06-01T10:00:00Z",
                    "updatedAt": "2025-07-01T10:00:00Z",
                },
            },
        )
        # Re-fetch after update
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders/wo1",
            json={
                "result": {
                    "id": "wo1",
                    "title": "Fix pump",
                    "priority": 2,
                    "status": "complete",
                    "assetId": "a1",
                    "createdAt": "2025-06-01T10:00:00Z",
                    "updatedAt": "2025-07-01T10:00:00Z",
                },
            },
        )
        from machina.domain.work_order import WorkOrderStatus

        updated = await connector.update_work_order("wo1", status=WorkOrderStatus.CLOSED)
        assert updated.id == "wo1"
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        import json

        payload = json.loads(patch_req.content)
        assert payload["status"] == "complete"  # CLOSED → UpKeep "complete"


# ---------------------------------------------------------------------------
# Get single asset
# ---------------------------------------------------------------------------


class TestGetAsset:
    @pytest.mark.asyncio
    async def test_get_asset_found(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/assets/a1",
            json={"result": {"id": "a1", "name": "Pump A"}},
        )
        asset = await connector.get_asset("a1")
        assert asset is not None
        assert asset.id == "a1"

    @pytest.mark.asyncio
    async def test_get_asset_not_found(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/assets/missing",
            status_code=404,
        )
        asset = await connector.get_asset("missing")
        assert asset is None

    @pytest.mark.asyncio
    async def test_get_asset_server_error(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/assets/err",
            status_code=500,
        )
        with pytest.raises(ConnectorError, match="GET asset failed"):
            await connector.get_asset("err")


# ---------------------------------------------------------------------------
# Read maintenance history
# ---------------------------------------------------------------------------


class TestReadMaintenanceHistory:
    @pytest.mark.asyncio
    async def test_read_maintenance_history(self, httpx_mock, connector: UpKeepConnector) -> None:
        """History query must propagate asset_id + status=complete."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders?limit=100&offset=0&asset=a1&status=complete",
            json={
                "results": [
                    {
                        "id": "wo-h1",
                        "title": "Past fix",
                        "priority": 2,
                        "status": "complete",
                        "assetId": "a1",
                        "createdAt": "2024-12-01T10:00:00Z",
                        "updatedAt": "2024-12-05T10:00:00Z",
                    },
                ],
            },
        )
        history = await connector.read_maintenance_history("a1")
        assert len(history) == 1
        assert history[0].id == "wo-h1"

    @pytest.mark.asyncio
    async def test_read_maintenance_history_pages_keep_the_filter(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """A full page triggers the next offset with asset + status kept."""
        await _connect(httpx_mock, connector)
        page1 = [{"id": f"wo{i}", "status": "complete", "assetId": "a1"} for i in range(100)]
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders?limit=100&offset=0&asset=a1&status=complete",
            json={"results": page1},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders?limit=100&offset=100&asset=a1&status=complete",
            json={"results": [{"id": "wo100", "status": "complete", "assetId": "a1"}]},
        )
        history = await connector.read_maintenance_history("a1")
        assert len(history) == 101
        assert history[-1].id == "wo100"

    @pytest.mark.asyncio
    async def test_read_maintenance_history_auth_failure(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", status_code=401)
        with pytest.raises(ConnectorAuthError, match="invalid"):
            await connector.read_maintenance_history("a1")

    @pytest.mark.asyncio
    async def test_read_maintenance_history_server_error(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", status_code=500)
        with pytest.raises(ConnectorError, match="GET /api/v2/work-orders failed"):
            await connector.read_maintenance_history("a1")

    @pytest.mark.asyncio
    async def test_read_maintenance_history_requires_connect(
        self, connector: UpKeepConnector
    ) -> None:
        with pytest.raises(ConnectorError, match="Not connected"):
            await connector.read_maintenance_history("a1")

    @pytest.mark.parametrize("asset_id", ["", None])
    @pytest.mark.asyncio
    async def test_read_maintenance_history_refuses_a_missing_asset_id(
        self, httpx_mock, connector: UpKeepConnector, asset_id
    ) -> None:
        """Without an asset the query would drop the filter and return every
        completed work order in the account."""
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="requires an asset_id"):
            await connector.read_maintenance_history(asset_id)
        assert len(httpx_mock.get_requests()) == 1  # only the connect handshake


# ---------------------------------------------------------------------------
# IDs placed in the URL path
# ---------------------------------------------------------------------------


class TestIdPathSegments:
    """IDs reach the connector from LLM / MCP-client input and go into the URL
    path. Each must stay one path segment: ``/``, ``?`` and ``#`` are
    percent-encoded, and the dot segments HTTP clients normalize away are
    refused, so an ID cannot reach another endpoint or append a query."""

    @pytest.mark.asyncio
    async def test_get_asset_id_cannot_climb_to_another_endpoint(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", status_code=404)
        assert await connector.get_asset("../users") is None
        assert httpx_mock.get_requests()[-1].url.raw_path == b"/api/v2/assets/..%2Fusers"

    @pytest.mark.asyncio
    async def test_get_work_order_id_cannot_append_a_query(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", status_code=404)
        assert await connector.get_work_order("wo1?limit=1#x") is None
        assert (
            httpx_mock.get_requests()[-1].url.raw_path
            == b"/api/v2/work-orders/wo1%3Flimit%3D1%23x"
        )

    @pytest.mark.asyncio
    async def test_update_id_stays_inside_its_path_segment(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        hostile = "../assets/a1"
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="PATCH", json={"result": {"id": hostile}})
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders/..%2Fassets%2Fa1",
            json={"result": {"id": hostile, "status": "open"}},
        )
        await connector.update_work_order(hostile, description="x")
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        assert patch_req.url.raw_path == b"/api/v2/work-orders/..%2Fassets%2Fa1"

    @pytest.mark.asyncio
    async def test_numeric_id_is_formatted_as_before(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """A workflow event can carry an ID as a number; it is sent as its digits."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", status_code=404)
        assert await connector.get_asset(123) is None
        assert httpx_mock.get_requests()[-1].url.raw_path == b"/api/v2/assets/123"

    @pytest.mark.parametrize("record_id", ["", ".", ".."])
    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(lambda c, i: c.get_asset(i), id="get_asset"),
            pytest.param(lambda c, i: c.get_work_order(i), id="get_work_order"),
            pytest.param(
                lambda c, i: c.update_work_order(i, description="x"), id="update_work_order"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_dot_segment_or_empty_id_is_refused(
        self, httpx_mock, connector: UpKeepConnector, call, record_id: str
    ) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="Invalid record ID"):
            await call(connector, record_id)
        assert len(httpx_mock.get_requests()) == 1  # only the connect handshake


# ---------------------------------------------------------------------------
# Lifecycle state transitions
# ---------------------------------------------------------------------------


class TestLifecycleAfterDisconnect:
    @pytest.mark.asyncio
    async def test_read_after_disconnect_raises(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        await connector.disconnect()
        with pytest.raises(ConnectorError, match="Not connected"):
            await connector.read_assets()


# ---------------------------------------------------------------------------
# Retry behaviour (503 → 200 via shared helper)
# ---------------------------------------------------------------------------


class TestRetryBehaviour:
    @pytest.mark.asyncio
    async def test_read_assets_retries_on_503(
        self, httpx_mock, monkeypatch, connector: UpKeepConnector
    ) -> None:
        """A 503 on the first GET must be retried and succeed on the second."""

        async def _no_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr("machina.connectors.cmms.retry.asyncio.sleep", _no_sleep)
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/assets?limit=100&offset=0",
            status_code=503,
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/assets?limit=100&offset=0",
            json={"results": [{"id": "a1", "name": "Pump"}]},
        )
        assets = await connector.read_assets()
        assert len(assets) == 1
        assert assets[0].id == "a1"
