"""Integration tests for SapPmConnector REST/OData operations.

All HTTP traffic is intercepted by pytest-httpx — no real SAP API calls.
"""

from __future__ import annotations

import httpx
import pytest

from machina.connectors.cmms.auth import BasicAuth, OAuth2ClientCredentials
from machina.connectors.cmms.sap_pm import SapPmConnector
from machina.domain.asset import Asset
from machina.domain.maintenance_plan import MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import ConnectorAuthError, ConnectorError

BASE = "https://sap.example.com/sap/opu/odata/sap"

# Default query params that _odata_get prepends to every request.
_ODATA_PARAMS = {"$top": "100", "$format": "json"}


def _odata_url(service: str, entity_set: str, **extra: str) -> httpx.URL:
    """Build a URL with the default OData query params."""
    params = {**_ODATA_PARAMS, **extra}
    return httpx.URL(f"{BASE}/{service}/{entity_set}", params=params)


@pytest.fixture
def connector() -> SapPmConnector:
    return SapPmConnector(
        url=BASE,
        auth=BasicAuth(username="sapuser", password="secret"),
        sap_client="100",
    )


async def _connect(httpx_mock, conn: SapPmConnector) -> None:
    """Register the $metadata response and connect."""
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/API_EQUIPMENT/$metadata",
        status_code=200,
        text="<edmx:Edmx/>",
    )
    await conn.connect()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class TestConnection:
    @pytest.mark.asyncio
    async def test_connect_basic_auth(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        assert connector._connected
        req = httpx_mock.get_requests()[0]
        assert "Authorization" in req.headers
        assert req.headers["sap-client"] == "100"

    @pytest.mark.asyncio
    async def test_connect_oauth2(self, httpx_mock) -> None:
        conn = SapPmConnector(
            url=BASE,
            auth=OAuth2ClientCredentials(
                token_url="https://sap.example.com/oauth/token",
                client_id="cid",
                client_secret="csecret",
            ),
        )
        # Token endpoint
        httpx_mock.add_response(
            method="POST",
            url="https://sap.example.com/oauth/token",
            json={"access_token": "tok123", "token_type": "bearer"},
        )
        # Metadata check
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_EQUIPMENT/$metadata",
            status_code=200,
            text="<edmx:Edmx/>",
        )
        await conn.connect()
        assert conn._connected
        # Metadata request should carry the Bearer token
        metadata_req = next(r for r in httpx_mock.get_requests() if r.method == "GET")
        assert metadata_req.headers["Authorization"] == "Bearer tok123"

    @pytest.mark.asyncio
    async def test_connect_auth_failure(self, httpx_mock, connector: SapPmConnector) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_EQUIPMENT/$metadata",
            status_code=401,
        )
        with pytest.raises(ConnectorAuthError, match="authentication failed"):
            await connector.connect()

    @pytest.mark.asyncio
    async def test_connect_server_error(self, httpx_mock, connector: SapPmConnector) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_EQUIPMENT/$metadata",
            status_code=500,
        )
        with pytest.raises(ConnectorError, match="500"):
            await connector.connect()


# ---------------------------------------------------------------------------
# Read assets (OData v2 format)
# ---------------------------------------------------------------------------


class TestReadAssets:
    @pytest.mark.asyncio
    async def test_read_assets_odata_v2(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_EQUIPMENT", "Equipment"),
            json={
                "d": {
                    "results": [
                        {
                            "Equipment": "10000001",
                            "EquipmentName": "Centrifugal Pump",
                            "EquipmentCategory": "M",
                            "FunctionalLocation": "PLANT-A",
                            "ABCIndicator": "A",
                        },
                    ],
                },
            },
        )
        assets = await connector.read_assets()
        assert len(assets) == 1
        assert isinstance(assets[0], Asset)
        assert assets[0].id == "10000001"
        assert assets[0].name == "Centrifugal Pump"

    @pytest.mark.asyncio
    async def test_read_assets_odata_v4(self, httpx_mock, connector: SapPmConnector) -> None:
        """OData v4 uses `value` instead of `d.results`."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_EQUIPMENT", "Equipment"),
            json={
                "value": [
                    {"Equipment": "20000001", "EquipmentName": "Compressor"},
                ],
            },
        )
        assets = await connector.read_assets()
        assert len(assets) == 1
        assert assets[0].id == "20000001"

    @pytest.mark.asyncio
    async def test_read_assets_server_driven_pagination(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """OData v2 __next link pagination."""
        await _connect(httpx_mock, connector)
        next_url = f"{BASE}/API_EQUIPMENT/Equipment?$skiptoken=100"
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_EQUIPMENT", "Equipment"),
            json={
                "d": {
                    "results": [{"Equipment": "A1", "EquipmentName": "Asset 1"}],
                    "__next": next_url,
                },
            },
        )
        httpx_mock.add_response(
            method="GET",
            url=next_url,
            json={
                "d": {
                    "results": [{"Equipment": "A2", "EquipmentName": "Asset 2"}],
                },
            },
        )
        assets = await connector.read_assets()
        assert len(assets) == 2

    @pytest.mark.asyncio
    async def test_get_asset(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_EQUIPMENT",
                "Equipment",
                **{"$top": "1", "$filter": "Equipment eq '10000001'"},
            ),
            json={
                "d": {
                    "results": [
                        {"Equipment": "10000001", "EquipmentName": "Pump"},
                    ],
                },
            },
        )
        asset = await connector.get_asset("10000001")
        assert asset is not None
        assert asset.id == "10000001"

    @pytest.mark.asyncio
    async def test_get_asset_not_found(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_EQUIPMENT",
                "Equipment",
                **{"$top": "1", "$filter": "Equipment eq 'NONEXIST'"},
            ),
            json={"d": {"results": []}},
        )
        asset = await connector.get_asset("NONEXIST")
        assert asset is None


# ---------------------------------------------------------------------------
# Read work orders
# ---------------------------------------------------------------------------


class TestReadWorkOrders:
    @pytest.mark.asyncio
    async def test_read_work_orders(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_MAINTENANCEORDER", "MaintenanceOrder"),
            json={
                "d": {
                    "results": [
                        {
                            "MaintenanceOrder": "4000001",
                            "MaintenanceOrderDesc": "Fix leak",
                            "MaintenanceOrderType": "PM01",
                            "MaintPriority": "2",
                            "MaintenanceOrderSystemStatus": "REL",
                            "Equipment": "10000001",
                            "CreationDate": "2025-06-01",
                            "LastChangeDateTime": "2025-06-02",
                        },
                    ],
                },
            },
        )
        wos = await connector.read_work_orders()
        assert len(wos) == 1
        wo = wos[0]
        assert isinstance(wo, WorkOrder)
        assert wo.id == "4000001"
        assert wo.priority == Priority.HIGH

    @pytest.mark.asyncio
    async def test_read_work_orders_filtered(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$filter": "Equipment eq '10000001' and MaintenanceOrderSystemStatus eq 'TECO'"},
            ),
            json={"d": {"results": []}},
        )
        wos = await connector.read_work_orders(asset_id="10000001", status="TECO")
        assert wos == []


# ---------------------------------------------------------------------------
# Create work order
# ---------------------------------------------------------------------------


class TestCreateWorkOrder:
    @pytest.mark.asyncio
    async def test_create_work_order(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        # CSRF token fetch
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            headers={"x-csrf-token": "csrf-abc123"},
            json={"d": {"results": []}},
        )
        # POST
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder",
            status_code=201,
            json={
                "d": {
                    "MaintenanceOrder": "4000099",
                    "MaintenanceOrderDesc": "Replace bearing",
                    "MaintenanceOrderType": "PM01",
                    "MaintPriority": "2",
                    "MaintenanceOrderSystemStatus": "CRTD",
                    "Equipment": "10000001",
                    "CreationDate": "2025-07-01",
                    "LastChangeDateTime": "2025-07-01",
                },
            },
        )
        from datetime import UTC, datetime

        wo = WorkOrder(
            id="TEMP",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="10000001",
            description="Replace bearing",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        created = await connector.create_work_order(wo)
        assert created.id == "4000099"
        post_req = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        assert post_req.headers.get("X-CSRF-Token") == "csrf-abc123"
        assert post_req.headers.get("sap-client") == "100"


# ---------------------------------------------------------------------------
# Read spare parts
# ---------------------------------------------------------------------------


class TestReadSpareParts:
    @pytest.mark.asyncio
    async def test_read_spare_parts(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_EQUIPMENT", "EquipmentBOM"),
            json={
                "d": {
                    "results": [
                        {
                            "Material": "MAT-001",
                            "MaterialDescription": "Bearing SKF 6205",
                            "AvailableQuantity": 50,
                            "StandardPrice": 35.0,
                            "StorageLocation": "SL01",
                        },
                    ],
                },
            },
        )
        parts = await connector.read_spare_parts()
        assert len(parts) == 1
        assert isinstance(parts[0], SparePart)
        assert parts[0].sku == "MAT-001"


# ---------------------------------------------------------------------------
# Read maintenance plans
# ---------------------------------------------------------------------------


class TestReadMaintenancePlans:
    @pytest.mark.asyncio
    async def test_read_maintenance_plans(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_MAINTENANCEPLAN", "MaintenancePlan"),
            json={
                "d": {
                    "results": [
                        {
                            "MaintenancePlan": "MP-001",
                            "MaintenancePlanDesc": "Monthly check",
                            "Equipment": "10000001",
                            "MaintenancePlanCycleValue": 30,
                            "MaintenancePlanCycleUnit": "DAY",
                            "MaintenancePlanStatus": "ACTV",
                        },
                    ],
                },
            },
        )
        plans = await connector.read_maintenance_plans()
        assert len(plans) == 1
        assert isinstance(plans[0], MaintenancePlan)
        assert plans[0].interval.days == 30
