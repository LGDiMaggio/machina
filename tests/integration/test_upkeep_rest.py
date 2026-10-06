"""Integration tests for UpKeepConnector REST operations.

All HTTP traffic is intercepted by pytest-httpx — no real UpKeep API calls.
"""

from __future__ import annotations

import pytest

from machina.connectors.cmms.upkeep import UpKeepConnector
from machina.domain.asset import Asset
from machina.domain.maintenance_plan import Interval, MaintenancePlan
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


PM_URL = f"{BASE}/api/v2/pm?includes=schedules&pageSize=100"


class TestReadMaintenancePlans:
    @pytest.mark.asyncio
    async def test_read_maintenance_plans(self, httpx_mock, connector: UpKeepConnector) -> None:
        """The "Get all PMs" response (developers.onupkeep.com/#get-all-pms).

        The documented template is soft-deleted; its deletedAt/deletedBy
        markers are left out so it reads as a live one.
        """
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{PM_URL}&page=1",
            json={
                "data": [
                    {
                        "__v": 0,
                        "_id": "6568e5bf7a771cd3d83b337c",
                        "category": "Inspection",
                        "createFirstWO": True,
                        "createdAt": "2023-11-30T19:42:55.984Z",
                        "createdBy": "N5lbFIv8LW",
                        "estimatedTime": 100,
                        "files": ["trfyrp53kw"],
                        "images": [],
                        "legacyPMId": "t69EvI5kMq",
                        "mainDescription": "Oil change #FP-ZFP-1",
                        "name": "Oil change #FP-ZFP-1",
                        "note": "Oil change for asset #FP-ZFP-1",
                        "partInventories": [],
                        "priority": 3,
                        "requiresSignature": False,
                        "role": "JtdU6sKOwD",
                        "schedules": [
                            {
                                "asset": "BcmoxOY4mE",
                                "assignee": "Fu8PEA9P9G",
                                "bySetPosition": [],
                                "cadenceFreq": "DAILY",
                                "cadenceInterval": 3,
                                "cadenceType": "manual",
                                "endDate": "2024-10-09T08:42:24.000Z",
                                "excludedates": [],
                                "id": "65672e0c90176209662a4fc2",
                                "includeDates": [],
                                "isBasedOnCompletion": False,
                                "location": "c78MVjkbvh",
                                "monthdays": [],
                                "nextDueDate": "2023-12-11T08:42:24.000Z",
                                "nextTriggerDate": "2023-12-10T08:42:24.000Z",
                                "pmTemplate": "65672e0c90176209662a4fc1",
                                "repeatFrequency": "MONTHLY",
                                "repeatInterval": 7,
                                "role": "PUlegnw3ml",
                                "scheduleType": "EVERY_N_MONTHS",
                                "startDate": "2023-12-10T08:42:24.000Z",
                                "supportUsers": ["IDb8mbMV9l"],
                                "team": "SFejxVqERT",
                                "timeZone": "Asia/Kolkata",
                                "weekdays": [],
                            },
                            {
                                "asset": "BcmoxOY4mE",
                                "assignee": "Fu8PEA9P9G",
                                "bySetPosition": [],
                                "endDate": "2024-10-09T16:23:35.000Z",
                                "excludedates": [],
                                "id": "65672e0c90176209662a4fc4",
                                "includeDates": [],
                                "location": "c78MVjkbvh",
                                "meter": "vXMGBoLMJv",
                                "meterConditionValue": 10000,
                                "meterDueFrequency": "weeks",
                                "meterDueInterval": 1,
                                "monthdays": [],
                                "nextMeterReading": 45750,
                                "pmTemplate": "65672e0c90176209662a4fc1",
                                "role": "PUlegnw3ml",
                                "startDate": "2023-12-10T16:23:35.000Z",
                                "supportUsers": ["IDb8mbMV9l"],
                                "team": "SFejxVqERT",
                                "timeZone": "Asia/Kolkata",
                                "weekdays": [],
                            },
                        ],
                        "tasks": [],
                        "updatedAt": "2023-11-30T19:43:30.230Z",
                    }
                ],
                "pagination": {
                    "currentPage": 1,
                    "currentPageSize": 25,
                    "nextPage": None,
                    "previousPage": None,
                    "totalCount": 2,
                    "totalPages": 1,
                },
                "success": True,
            },
        )
        plans = await connector.read_maintenance_plans()
        assert all(isinstance(p, MaintenancePlan) for p in plans)
        assert [p.id for p in plans] == ["65672e0c90176209662a4fc2", "65672e0c90176209662a4fc4"]
        assert {p.asset_id for p in plans} == {"BcmoxOY4mE"}
        assert {p.name for p in plans} == {"Oil change #FP-ZFP-1"}
        assert plans[0].interval == Interval(months=7)
        assert plans[1].interval == Interval()  # meter-triggered
        assert plans[0].estimated_duration_hours == 100

    @pytest.mark.asyncio
    async def test_read_maintenance_plans_pagination(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """Pages are numbered from 1; a page shorter than pageSize is the last."""
        await _connect(httpx_mock, connector)

        def template(i: int) -> dict[str, object]:
            schedule = {
                "id": f"s{i}",
                "asset": "a1",
                "repeatFrequency": "WEEKLY",
                "repeatInterval": 1,
            }
            return {"_id": f"t{i}", "name": f"PM {i}", "schedules": [schedule], "tasks": []}

        httpx_mock.add_response(
            method="GET",
            url=f"{PM_URL}&page=1",
            json={"data": [template(i) for i in range(100)]},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{PM_URL}&page=2",
            json={"data": [template(100)]},
        )
        plans = await connector.read_maintenance_plans()
        assert len(plans) == 101
        assert plans[-1].id == "s100"

    @pytest.mark.asyncio
    async def test_read_maintenance_plans_auth_failure(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", url=f"{PM_URL}&page=1", status_code=401)
        with pytest.raises(ConnectorAuthError):
            await connector.read_maintenance_plans()

    @pytest.mark.asyncio
    async def test_read_maintenance_plans_server_error(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", url=f"{PM_URL}&page=1", status_code=500)
        with pytest.raises(ConnectorError, match="GET /api/v2/pm failed"):
            await connector.read_maintenance_plans()


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
