"""Integration tests for MaximoConnector REST operations.

All HTTP traffic is intercepted by pytest-httpx — no real Maximo API calls.
"""

from __future__ import annotations

import httpx
import pytest

from machina.connectors.cmms.auth import ApiKeyHeaderAuth, BasicAuth
from machina.connectors.cmms.maximo import MaximoConnector
from machina.domain.asset import Asset, AssetType
from machina.domain.maintenance_plan import MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import ConnectorAuthError, ConnectorError

BASE = "https://maximo.example.com"
OSLC = f"{BASE}/maximo/oslc"

# Default query params that _oslc_get appends to every first request.
_OSLC_PARAMS = {"lean": "1", "oslc.pageSize": "100"}


def _oslc_url(object_structure: str, **extra: str) -> httpx.URL:
    """Build a URL with the default OSLC query params."""
    params = {**_OSLC_PARAMS, **extra}
    return httpx.URL(f"{OSLC}/os/{object_structure}", params=params)


@pytest.fixture
def connector() -> MaximoConnector:
    return MaximoConnector(
        url=BASE,
        auth=ApiKeyHeaderAuth(header_name="apikey", value="test-key"),
    )


async def _connect(httpx_mock, conn: MaximoConnector) -> None:
    """Register the whoami response and connect."""
    httpx_mock.add_response(
        method="GET",
        url=f"{OSLC}/whoami",
        status_code=200,
        json={"userName": "maxadmin", "displayName": "Max Admin"},
    )
    await conn.connect()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class TestConnection:
    @pytest.mark.asyncio
    async def test_connect_success(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        assert connector._connected
        req = httpx_mock.get_requests()[0]
        assert req.headers["apikey"] == "test-key"

    @pytest.mark.asyncio
    async def test_connect_basic_auth(self, httpx_mock) -> None:
        conn = MaximoConnector(
            url=BASE,
            auth=BasicAuth(username="maxadmin", password="secret"),
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{OSLC}/whoami",
            status_code=200,
            json={"userName": "maxadmin"},
        )
        await conn.connect()
        req = httpx_mock.get_requests()[0]
        assert "Authorization" in req.headers

    @pytest.mark.asyncio
    async def test_connect_invalid_credentials(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{OSLC}/whoami",
            status_code=401,
        )
        with pytest.raises(ConnectorAuthError, match="authentication failed"):
            await connector.connect()

    @pytest.mark.asyncio
    async def test_connect_server_error(self, httpx_mock, connector: MaximoConnector) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{OSLC}/whoami",
            status_code=500,
        )
        with pytest.raises(ConnectorError, match="500"):
            await connector.connect()


# ---------------------------------------------------------------------------
# Read assets
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_healthy(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        health = await connector.health_check()
        assert health.status.value == "healthy"
        assert health.details["url"] == BASE


class TestReadAssets:
    @pytest.mark.asyncio
    async def test_read_assets_single_page(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxasset"),
            json={
                "member": [
                    {"assetnum": "PUMP-201", "description": "Centrifugal Pump"},
                    {"assetnum": "COMP-301", "description": "Compressor"},
                ],
                "responseInfo": {},
            },
        )
        assets = await connector.read_assets()
        assert len(assets) == 2
        assert all(isinstance(a, Asset) for a in assets)
        assert assets[0].id == "PUMP-201"

    @pytest.mark.asyncio
    async def test_read_assets_pagination(self, httpx_mock, connector: MaximoConnector) -> None:
        """OSLC pagination via responseInfo.nextPage."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxasset"),
            json={
                "member": [{"assetnum": "A1", "description": "Asset 1"}],
                "responseInfo": {
                    "nextPage": f"{OSLC}/os/mxasset?pageno=2&oslc.pageSize=100",
                },
            },
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{OSLC}/os/mxasset?pageno=2&oslc.pageSize=100",
            json={
                "member": [{"assetnum": "A2", "description": "Asset 2"}],
                "responseInfo": {},
            },
        )
        assets = await connector.read_assets()
        assert len(assets) == 2
        assert assets[1].id == "A2"

    @pytest.mark.asyncio
    async def test_get_asset(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(
                "mxasset", **{"oslc.pageSize": "1", "oslc.where": 'assetnum="PUMP-201"'}
            ),
            json={
                "member": [{"assetnum": "PUMP-201", "description": "Pump"}],
                "responseInfo": {},
            },
        )
        asset = await connector.get_asset("PUMP-201")
        assert asset is not None
        assert asset.id == "PUMP-201"

    @pytest.mark.asyncio
    async def test_get_asset_not_found(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(
                "mxasset", **{"oslc.pageSize": "1", "oslc.where": 'assetnum="NONEXIST"'}
            ),
            json={"member": [], "responseInfo": {}},
        )
        asset = await connector.get_asset("NONEXIST")
        assert asset is None


# ---------------------------------------------------------------------------
# Read work orders
# ---------------------------------------------------------------------------


class TestReadWorkOrders:
    @pytest.mark.asyncio
    async def test_read_work_orders(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo"),
            json={
                "member": [
                    {
                        "wonum": "WO-001",
                        "description": "Fix leak",
                        "wopriority": 2,
                        "status": "INPRG",
                        "worktype": "CM",
                        "assetnum": "PUMP-201",
                        "reportdate": "2025-06-01T10:00:00Z",
                        "changedate": "2025-06-02T08:00:00Z",
                    },
                ],
                "responseInfo": {},
            },
        )
        wos = await connector.read_work_orders()
        assert len(wos) == 1
        wo = wos[0]
        assert isinstance(wo, WorkOrder)
        assert wo.id == "WO-001"
        assert wo.priority == Priority.HIGH

    @pytest.mark.asyncio
    async def test_read_work_orders_filtered(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": 'assetnum="PUMP-201" and status="COMP"'}),
            json={"member": [], "responseInfo": {}},
        )
        wos = await connector.read_work_orders(asset_id="PUMP-201", status="COMP")
        assert wos == []


# ---------------------------------------------------------------------------
# Create work order
# ---------------------------------------------------------------------------


class TestCreateWorkOrder:
    @pytest.mark.asyncio
    async def test_create_work_order(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="POST",
            url=f"{OSLC}/os/mxwo",
            status_code=201,
            json={
                "wonum": "WO-NEW",
                "description": "Replace bearing",
                "wopriority": 2,
                "status": "WAPPR",
                "worktype": "CM",
                "assetnum": "PUMP-201",
                "reportdate": "2025-07-01T08:00:00Z",
                "changedate": "2025-07-01T08:00:00Z",
            },
        )
        from datetime import UTC, datetime

        wo = WorkOrder(
            id="TEMP",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="PUMP-201",
            description="Replace bearing",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        created = await connector.create_work_order(wo)
        assert created.id == "WO-NEW"
        req = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        assert req.headers["apikey"] == "test-key"

    @pytest.mark.asyncio
    async def test_create_work_order_with_assigned_to(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """assigned_to must be sent as the Maximo 'lead' field."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="POST",
            url=f"{OSLC}/os/mxwo",
            status_code=201,
            json={
                "wonum": "WO-LEAD",
                "description": "With lead",
                "wopriority": 3,
                "status": "WAPPR",
                "worktype": "CM",
                "assetnum": "P1",
                "lead": "john.doe",
                "reportdate": "2025-07-01T08:00:00Z",
                "changedate": "2025-07-01T08:00:00Z",
            },
        )
        from datetime import UTC, datetime

        wo = WorkOrder(
            id="TEMP",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.MEDIUM,
            asset_id="P1",
            description="With lead",
            assigned_to="john.doe",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        created = await connector.create_work_order(wo)
        assert created.assigned_to == "john.doe"
        import json

        req = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        payload = json.loads(req.content)
        assert payload["lead"] == "john.doe"

    @pytest.mark.asyncio
    async def test_create_work_order_auth_failure(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="POST", url=f"{OSLC}/os/mxwo", status_code=401)
        from datetime import UTC, datetime

        wo = WorkOrder(
            id="T",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="P1",
            description="x",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        with pytest.raises(ConnectorAuthError):
            await connector.create_work_order(wo)

    @pytest.mark.asyncio
    async def test_create_work_order_error(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="POST", url=f"{OSLC}/os/mxwo", status_code=422)
        from datetime import UTC, datetime

        wo = WorkOrder(
            id="T",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="P1",
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
    async def test_read_spare_parts(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxinventory"),
            json={
                "member": [
                    {
                        "itemnum": "BRG-6205",
                        "description": "Bearing SKF 6205",
                        "curbal": 50,
                        "reorder": 10,
                        "avgcost": 35.0,
                        "location": "WHSE-1",
                        "siteid": "SITE1",
                    },
                ],
                "responseInfo": {},
            },
        )
        parts = await connector.read_spare_parts()
        assert len(parts) == 1
        assert isinstance(parts[0], SparePart)
        assert parts[0].sku == "BRG-6205"
        # Unknown fields preserved in metadata.
        assert parts[0].metadata["siteid"] == "SITE1"

    @pytest.mark.asyncio
    async def test_read_spare_parts_filtered_by_sku(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """sku filter must be translated into an OSLC where clause."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxinventory", **{"oslc.where": 'itemnum="BRG-6205"'}),
            json={
                "member": [
                    {
                        "itemnum": "BRG-6205",
                        "description": "Bearing",
                        "curbal": 5,
                    },
                ],
                "responseInfo": {},
            },
        )
        parts = await connector.read_spare_parts(sku="BRG-6205")
        assert len(parts) == 1
        assert parts[0].sku == "BRG-6205"


# ---------------------------------------------------------------------------
# Read maintenance plans
# ---------------------------------------------------------------------------


class TestReadMaintenancePlans:
    @pytest.mark.asyncio
    async def test_read_maintenance_plans(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxpm"),
            json={
                "member": [
                    {
                        "pmnum": "PM-001",
                        "description": "Monthly inspection",
                        "assetnum": "PUMP-201",
                        "frequency": 30,
                        "status": "ACTIVE",
                    },
                ],
                "responseInfo": {},
            },
        )
        plans = await connector.read_maintenance_plans()
        assert len(plans) == 1
        assert isinstance(plans[0], MaintenancePlan)
        assert plans[0].interval.days == 30


# ---------------------------------------------------------------------------
# Read maintenance history
# ---------------------------------------------------------------------------


class TestReadMaintenanceHistory:
    @pytest.mark.asyncio
    async def test_read_maintenance_history(self, httpx_mock, connector: MaximoConnector) -> None:
        """History query must combine assetnum with completed/closed status."""
        await _connect(httpx_mock, connector)
        expected_where = 'assetnum="PUMP-201" and (status="COMP" or status="CLOSE")'
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": expected_where}),
            json={
                "member": [
                    {
                        "wonum": "WO-H1",
                        "description": "Past repair",
                        "wopriority": 3,
                        "status": "COMP",
                        "worktype": "CM",
                        "assetnum": "PUMP-201",
                        "reportdate": "2024-10-01T10:00:00Z",
                        "changedate": "2024-10-03T10:00:00Z",
                    },
                ],
                "responseInfo": {},
            },
        )
        history = await connector.read_maintenance_history("PUMP-201")
        assert len(history) == 1
        assert history[0].id == "WO-H1"


# ---------------------------------------------------------------------------
# Lifecycle state transitions
# ---------------------------------------------------------------------------


class TestLifecycleAfterDisconnect:
    @pytest.mark.asyncio
    async def test_read_after_disconnect_raises(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        await connector.disconnect()
        with pytest.raises(ConnectorError, match="Not connected"):
            await connector.read_assets()


# ---------------------------------------------------------------------------
# Retry behaviour (429 → 200 via shared helper)
# ---------------------------------------------------------------------------


class TestGetWorkOrder:
    @pytest.mark.asyncio
    async def test_get_work_order_found(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.pageSize": "1", "oslc.where": 'wonum="WO-001"'}),
            json={
                "member": [
                    {
                        "wonum": "WO-001",
                        "description": "Fix leak",
                        "wopriority": 2,
                        "status": "INPRG",
                        "worktype": "CM",
                        "assetnum": "P1",
                        "reportdate": "2025-06-01T10:00:00Z",
                        "changedate": "2025-06-02T08:00:00Z",
                    },
                ],
                "responseInfo": {},
            },
        )
        wo = await connector.get_work_order("WO-001")
        assert wo is not None
        assert wo.id == "WO-001"

    @pytest.mark.asyncio
    async def test_get_work_order_not_found(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.pageSize": "1", "oslc.where": 'wonum="MISS"'}),
            json={"member": [], "responseInfo": {}},
        )
        wo = await connector.get_work_order("MISS")
        assert wo is None


class TestUpdateWorkOrder:
    @pytest.mark.asyncio
    async def test_update_work_order_status(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="PATCH",
            url=f"{OSLC}/os/mxwo/WO-001",
            status_code=204,
        )
        # Re-fetch after update
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.pageSize": "1", "oslc.where": 'wonum="WO-001"'}),
            json={
                "member": [
                    {
                        "wonum": "WO-001",
                        "description": "Fix leak",
                        "wopriority": 2,
                        "status": "CLOSE",
                        "worktype": "CM",
                        "assetnum": "P1",
                        "reportdate": "2025-06-01T10:00:00Z",
                        "changedate": "2025-07-01T10:00:00Z",
                    },
                ],
                "responseInfo": {},
            },
        )
        from machina.domain.work_order import WorkOrderStatus

        updated = await connector.update_work_order("WO-001", status=WorkOrderStatus.CLOSED)
        assert updated.id == "WO-001"
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        import json

        payload = json.loads(patch_req.content)
        assert payload["status"] == "CLOSE"


class TestAssetTypeMap:
    @pytest.mark.asyncio
    async def test_read_assets_honours_custom_type_map(self, httpx_mock) -> None:
        """User-supplied asset_type_map must drive AssetType classification."""
        custom = MaximoConnector(
            url=BASE,
            auth=ApiKeyHeaderAuth(header_name="apikey", value="k"),
            asset_type_map={
                "VESSELS": AssetType.STATIC_EQUIPMENT,
                "PUMPS": AssetType.ROTATING_EQUIPMENT,
            },
        )
        await _connect(httpx_mock, custom)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxasset"),
            json={
                "member": [
                    {
                        "assetnum": "V1",
                        "description": "Storage tank",
                        "classstructureid": "VESSELS",
                    },
                    {
                        "assetnum": "P1",
                        "description": "Feed pump",
                        "classstructureid": "PUMPS",
                    },
                ],
                "responseInfo": {},
            },
        )
        assets = await custom.read_assets()
        assert len(assets) == 2
        by_id = {a.id: a for a in assets}
        assert by_id["V1"].type == AssetType.STATIC_EQUIPMENT
        assert by_id["P1"].type == AssetType.ROTATING_EQUIPMENT


class TestRetryBehaviour:
    @pytest.mark.asyncio
    async def test_read_assets_retries_on_429(
        self, httpx_mock, monkeypatch, connector: MaximoConnector
    ) -> None:
        """A 429 with Retry-After must be retried and succeed on the next try."""

        async def _no_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr("machina.connectors.cmms.retry.asyncio.sleep", _no_sleep)
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxasset"),
            status_code=429,
            headers={"Retry-After": "1"},
        )
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxasset"),
            json={
                "member": [{"assetnum": "A1", "description": "Pump"}],
                "responseInfo": {},
            },
        )
        assets = await connector.read_assets()
        assert len(assets) == 1
        assert assets[0].id == "A1"
