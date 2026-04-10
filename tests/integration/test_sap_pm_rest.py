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


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_healthy(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        health = await connector.health_check()
        assert health.status.value == "healthy"
        assert health.details["url"] == BASE


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

    @pytest.mark.asyncio
    async def test_read_assets_auth_failure(self, httpx_mock, connector: SapPmConnector) -> None:
        """401 during paginated GET must raise ConnectorAuthError."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_EQUIPMENT", "Equipment"),
            status_code=401,
        )
        with pytest.raises(ConnectorAuthError, match="authentication failed"):
            await connector.read_assets()

    @pytest.mark.asyncio
    async def test_read_assets_server_error(self, httpx_mock, connector: SapPmConnector) -> None:
        """Non-200, non-401 during paginated GET must raise ConnectorError."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_EQUIPMENT", "Equipment"),
            status_code=500,
        )
        with pytest.raises(ConnectorError, match="HTTP 500"):
            await connector.read_assets()

    @pytest.mark.asyncio
    async def test_read_assets_single_entity_dict(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """OData may return a single entity as a dict (not list). Must be wrapped."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_EQUIPMENT", "Equipment"),
            json={
                "d": {
                    "results": {"Equipment": "E1", "EquipmentName": "Pump"},
                },
            },
        )
        assets = await connector.read_assets()
        assert len(assets) == 1
        assert assets[0].id == "E1"


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
                **{
                    "$filter": "Equipment eq '10000001' and MaintenanceOrderSystemStatus eq 'TECO'"
                },
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

    @pytest.mark.asyncio
    async def test_create_work_order_csrf_fetch_failure(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """A failed CSRF token fetch must raise ConnectorError, not silently POST."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            status_code=403,
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
        with pytest.raises(ConnectorError, match="CSRF token fetch failed"):
            await connector.create_work_order(wo)

    @pytest.mark.asyncio
    async def test_create_work_order_csrf_header_missing(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """A 200 CSRF response without the x-csrf-token header must raise."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            status_code=200,
            json={"d": {"results": []}},
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
        with pytest.raises(ConnectorError, match="x-csrf-token"):
            await connector.create_work_order(wo)

    @pytest.mark.asyncio
    async def test_create_work_order_csrf_401(self, httpx_mock, connector: SapPmConnector) -> None:
        """A 401 on CSRF fetch must raise ConnectorAuthError."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            status_code=401,
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
        with pytest.raises(ConnectorAuthError, match="CSRF token fetch"):
            await connector.create_work_order(wo)

    @pytest.mark.asyncio
    async def test_create_work_order_post_401(self, httpx_mock, connector: SapPmConnector) -> None:
        """A 401 from the POST itself must raise ConnectorAuthError."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            headers={"x-csrf-token": "csrf-valid"},
            json={"d": {"results": []}},
        )
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder",
            status_code=401,
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
        with pytest.raises(ConnectorAuthError, match="authentication failed"):
            await connector.create_work_order(wo)

    @pytest.mark.asyncio
    async def test_create_work_order_post_error(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """A non-2xx, non-401 POST must raise ConnectorError."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            headers={"x-csrf-token": "csrf-valid"},
            json={"d": {"results": []}},
        )
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder",
            status_code=422,
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
        with pytest.raises(ConnectorError, match="create maintenance order failed"):
            await connector.create_work_order(wo)


# ---------------------------------------------------------------------------
# Read spare parts
# ---------------------------------------------------------------------------


class TestReadSpareParts:
    @pytest.mark.asyncio
    async def test_read_spare_parts(self, httpx_mock, connector: SapPmConnector) -> None:
        """Default BOM service is API_BILL_OF_MATERIAL_SRV/BillOfMaterialItem."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_BILL_OF_MATERIAL_SRV", "BillOfMaterialItem"),
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

    @pytest.mark.asyncio
    async def test_read_spare_parts_sku_filter(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """sku filter must use the configured bom_material_field."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_BILL_OF_MATERIAL_SRV",
                "BillOfMaterialItem",
                **{"$filter": "BillOfMaterialComponent eq 'MAT-001'"},
            ),
            json={
                "d": {
                    "results": [
                        {
                            "Material": "MAT-001",
                            "MaterialDescription": "Bearing",
                            "AvailableQuantity": 5,
                        },
                    ],
                },
            },
        )
        parts = await connector.read_spare_parts(sku="MAT-001")
        assert len(parts) == 1
        assert parts[0].sku == "MAT-001"

    @pytest.mark.asyncio
    async def test_read_spare_parts_asset_filter_ignored_by_default(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """Without bom_equipment_field, asset_id is dropped with a warning."""
        await _connect(httpx_mock, connector)
        # No $filter expected: asset_id is ignored, no sku supplied.
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_BILL_OF_MATERIAL_SRV", "BillOfMaterialItem"),
            json={"d": {"results": []}},
        )
        parts = await connector.read_spare_parts(asset_id="10000001")
        assert parts == []

    @pytest.mark.asyncio
    async def test_read_spare_parts_custom_endpoint(self, httpx_mock) -> None:
        """Users can override the BOM service for on-premise / legacy SAP."""
        custom = SapPmConnector(
            url=BASE,
            auth=BasicAuth(username="u", password="p"),
            bom_service="API_EQUIPMENT",
            bom_entity_set="EquipmentBOM",
            bom_material_field="Material",
            bom_equipment_field="Equipment",
        )
        await _connect(httpx_mock, custom)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_EQUIPMENT",
                "EquipmentBOM",
                **{"$filter": "Equipment eq '10000001' and Material eq 'MAT-001'"},
            ),
            json={
                "d": {
                    "results": [
                        {
                            "Material": "MAT-001",
                            "MaterialDescription": "Bearing",
                            "AvailableQuantity": 5,
                        },
                    ],
                },
            },
        )
        parts = await custom.read_spare_parts(asset_id="10000001", sku="MAT-001")
        assert len(parts) == 1


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


# ---------------------------------------------------------------------------
# Read maintenance history
# ---------------------------------------------------------------------------


class TestReadMaintenanceHistory:
    @pytest.mark.asyncio
    async def test_read_maintenance_history(self, httpx_mock, connector: SapPmConnector) -> None:
        """History query must filter by Equipment and a compound status clause."""
        await _connect(httpx_mock, connector)
        expected_filter = (
            "Equipment eq '10000001' and "
            "(MaintenanceOrderSystemStatus eq 'CNF' or "
            "MaintenanceOrderSystemStatus eq 'TECO' or "
            "MaintenanceOrderSystemStatus eq 'CLSD')"
        )
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$filter": expected_filter},
            ),
            json={
                "d": {
                    "results": [
                        {
                            "MaintenanceOrder": "4000010",
                            "MaintenanceOrderDesc": "Past repair",
                            "MaintenanceOrderType": "PM01",
                            "MaintPriority": "3",
                            "MaintenanceOrderSystemStatus": "CNF",
                            "Equipment": "10000001",
                            "CreationDate": "2024-12-01",
                            "LastChangeDateTime": "2024-12-05",
                        },
                        {
                            "MaintenanceOrder": "4000011",
                            "MaintenanceOrderDesc": "Closed job",
                            "MaintenanceOrderType": "PM02",
                            "MaintPriority": "3",
                            "MaintenanceOrderSystemStatus": "TECO",
                            "Equipment": "10000001",
                            "CreationDate": "2025-01-10",
                            "LastChangeDateTime": "2025-01-15",
                        },
                    ],
                },
            },
        )
        history = await connector.read_maintenance_history("10000001")
        assert len(history) == 2
        assert all(isinstance(wo, WorkOrder) for wo in history)
        assert history[0].id == "4000010"


# ---------------------------------------------------------------------------
# Lifecycle state transitions
# ---------------------------------------------------------------------------


class TestLifecycleAfterDisconnect:
    @pytest.mark.asyncio
    async def test_read_after_disconnect_raises(
        self, httpx_mock, connector: SapPmConnector
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
        self, httpx_mock, monkeypatch, connector: SapPmConnector
    ) -> None:
        """A 503 on the first GET must be retried and succeed on the second."""

        async def _no_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr("machina.connectors.cmms.retry.asyncio.sleep", _no_sleep)
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_EQUIPMENT", "Equipment"),
            status_code=503,
        )
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_EQUIPMENT", "Equipment"),
            json={"d": {"results": [{"Equipment": "E1", "EquipmentName": "Pump"}]}},
        )
        assets = await connector.read_assets()
        assert len(assets) == 1
        assert assets[0].id == "E1"


# ---------------------------------------------------------------------------
# get_work_order
# ---------------------------------------------------------------------------


class TestGetWorkOrder:
    @pytest.mark.asyncio
    async def test_get_work_order_found(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$top": "1", "$filter": "MaintenanceOrder eq '4000001'"},
            ),
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
        wo = await connector.get_work_order("4000001")
        assert wo is not None
        assert wo.id == "4000001"

    @pytest.mark.asyncio
    async def test_get_work_order_not_found(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$top": "1", "$filter": "MaintenanceOrder eq 'MISSING'"},
            ),
            json={"d": {"results": []}},
        )
        wo = await connector.get_work_order("MISSING")
        assert wo is None


# ---------------------------------------------------------------------------
# update_work_order (CSRF + PATCH in single session)
# ---------------------------------------------------------------------------


class TestUpdateWorkOrder:
    @pytest.mark.asyncio
    async def test_update_work_order_status(self, httpx_mock, connector: SapPmConnector) -> None:
        """PATCH must use CSRF token from the same HTTP session."""
        await _connect(httpx_mock, connector)
        # CSRF token fetch (same session as PATCH)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            headers={"x-csrf-token": "csrf-upd"},
            json={"d": {"results": []}},
        )
        # PATCH response
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder('4000001')",
            status_code=204,
        )
        # Re-fetch after update
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$top": "1", "$filter": "MaintenanceOrder eq '4000001'"},
            ),
            json={
                "d": {
                    "results": [
                        {
                            "MaintenanceOrder": "4000001",
                            "MaintenanceOrderDesc": "Fix leak",
                            "MaintenanceOrderType": "PM01",
                            "MaintPriority": "2",
                            "MaintenanceOrderSystemStatus": "TECO",
                            "Equipment": "10000001",
                            "CreationDate": "2025-06-01",
                            "LastChangeDateTime": "2025-07-01",
                        },
                    ],
                },
            },
        )
        from machina.domain.work_order import WorkOrderStatus

        updated = await connector.update_work_order("4000001", status=WorkOrderStatus.CLOSED)
        assert updated.id == "4000001"
        # Verify the PATCH carried the CSRF token
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        assert patch_req.headers.get("X-CSRF-Token") == "csrf-upd"
