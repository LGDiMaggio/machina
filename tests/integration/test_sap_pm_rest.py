"""Integration tests for SapPmConnector REST/OData operations.

All HTTP traffic is intercepted by pytest-httpx — no real SAP API calls.
"""

from __future__ import annotations

import json

import httpx
import pytest

from machina.connectors.cmms.auth import BasicAuth, OAuth2ClientCredentials
from machina.connectors.cmms.sap_pm import SapPmConnector
from machina.domain.asset import Asset
from machina.domain.maintenance_plan import MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderStatus, WorkOrderType
from machina.exceptions import ConnectorAuthError, ConnectorError

BASE = "https://sap.example.com/sap/opu/odata/sap"

# Default query params that _odata_get prepends to every request.
_ODATA_PARAMS = {"$top": "100", "$format": "json"}


def _odata_url(service: str, entity_set: str, **extra: str) -> httpx.URL:
    """Build a URL with the default OData query params."""
    params = {**_ODATA_PARAMS, **extra}
    return httpx.URL(f"{BASE}/{service}/{entity_set}", params=params)


def _order(order_id: str, system_status: str) -> dict[str, str]:
    """A MaintenanceOrder entity of equipment 10000001.

    ``SystemStatusText`` is how API_MAINTENANCEORDER reports an order's
    system statuses: one line, each code padded to four characters.
    """
    return {
        "MaintenanceOrder": order_id,
        "MaintenanceOrderType": "PM01",
        "MaintPriority": "3",
        "Equipment": "10000001",
        "SystemStatusText": system_status,
    }


# One order per lifecycle state, as SAP reports them.
_ORDERS = [
    _order("4000001", "CRTD MANC NMAT"),  # created
    _order("4000002", "REL  MANC PRC  SETC"),  # assigned (released)
    _order("4000003", "REL  PCNF PRC  SETC"),  # in progress (partially confirmed)
    _order("4000004", "REL  CNF  PRC  SETC"),  # completed (confirmed)
    _order("4000005", "TECO CNF  PRC  SETC"),  # closed (technically completed)
    _order("4000006", "CLSD CNF  PRC  SETC"),  # closed (business completed)
    _order("4000007", "CRTD DLFL MANC"),  # cancelled (deletion flag)
]


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
                            "SystemStatusText": "REL  PRC  SETC",
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
        assert wo.status == WorkOrderStatus.ASSIGNED

    @pytest.mark.parametrize(
        ("status", "expected_ids"),
        [
            pytest.param(WorkOrderStatus.CLOSED, ["4000005", "4000006"], id="enum"),
            pytest.param("closed", ["4000005", "4000006"], id="enum-value"),
            pytest.param("In_Progress", ["4000003"], id="enum-value-any-case"),
            pytest.param(WorkOrderStatus.CANCELLED, ["4000007"], id="cancelled"),
            pytest.param("REL", ["4000002", "4000003", "4000004"], id="sap-code"),
            pytest.param("teco", ["4000005"], id="sap-code-any-case"),
            pytest.param("MANC", ["4000001", "4000002", "4000007"], id="sap-code-secondary"),
        ],
    )
    @pytest.mark.asyncio
    async def test_status_is_applied_to_the_equipment_read(
        self, httpx_mock, connector: SapPmConnector, status, expected_ids: list[str]
    ) -> None:
        """SystemStatusText is not filterable (SAP KBA 3614100): the request
        filters by Equipment only, and ``status`` selects among the orders read."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$filter": "Equipment eq '10000001'"},
            ),
            json={"d": {"results": _ORDERS}},
        )
        wos = await connector.read_work_orders(asset_id="10000001", status=status)
        assert [wo.id for wo in wos] == expected_ids

    @pytest.mark.asyncio
    async def test_status_without_asset_sends_no_filter(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_MAINTENANCEORDER", "MaintenanceOrder"),
            json={"d": {"results": _ORDERS}},
        )
        wos = await connector.read_work_orders(status=WorkOrderStatus.COMPLETED)
        assert [wo.id for wo in wos] == ["4000004"]

    @pytest.mark.asyncio
    async def test_status_applies_across_pages(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """Orders on every page are matched, not just the first page."""
        await _connect(httpx_mock, connector)
        page1 = [_order(f"40{i:05d}", "CRTD MANC") for i in range(99)] + [
            _order("4100001", "REL  PRC")
        ]
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_MAINTENANCEORDER", "MaintenanceOrder"),
            json={"d": {"results": page1}},
        )
        httpx_mock.add_response(
            method="GET",
            url=_odata_url("API_MAINTENANCEORDER", "MaintenanceOrder", **{"$skip": "100"}),
            json={"d": {"results": [_order("4100002", "REL  PCNF PRC")]}},
        )
        wos = await connector.read_work_orders(status="REL")
        assert [wo.id for wo in wos] == ["4100001", "4100002"]


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
                    "SystemStatusText": "CRTD MANC NMAT",
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
        assert created.status == WorkOrderStatus.CREATED
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
        """Without bom_equipment_field and no sku, the unbounded BOM read is refused.

        asset_id cannot be filtered server-side and there is no sku to narrow
        the query, so issuing the read would page the entire BOM into memory
        (U9 OOM guard). The connector returns ``[]`` and issues NO request — the
        only mocked response here is the connect handshake from ``_connect``.
        """
        await _connect(httpx_mock, connector)
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
        """History reads the equipment's orders and keeps the completed and closed ones.

        SAP cannot filter on SystemStatusText, so the only server-side clause
        is the Equipment one.
        """
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$filter": "Equipment eq '10000001'"},
            ),
            json={"d": {"results": _ORDERS}},
        )
        history = await connector.read_maintenance_history("10000001")
        assert all(isinstance(wo, WorkOrder) for wo in history)
        assert [(wo.id, wo.status) for wo in history] == [
            ("4000004", WorkOrderStatus.COMPLETED),
            ("4000005", WorkOrderStatus.CLOSED),
            ("4000006", WorkOrderStatus.CLOSED),
        ]

    @pytest.mark.asyncio
    async def test_read_maintenance_history_pages_keep_the_filter(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """A full page triggers a ``$skip`` request that keeps the Equipment filter."""
        await _connect(httpx_mock, connector)
        page1 = [_order(f"40{i:05d}", "REL  CNF  PRC  SETC") for i in range(100)]
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$filter": "Equipment eq '10000001'"},
            ),
            json={"d": {"results": page1}},
        )
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$filter": "Equipment eq '10000001'", "$skip": "100"},
            ),
            json={"d": {"results": [_order("4100000", "TECO CNF  PRC  SETC")]}},
        )
        history = await connector.read_maintenance_history("10000001")
        assert len(history) == 101
        assert history[-1].id == "4100000"

    @pytest.mark.asyncio
    async def test_read_maintenance_history_auth_failure(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", status_code=401)
        with pytest.raises(ConnectorAuthError, match="authentication failed"):
            await connector.read_maintenance_history("10000001")

    @pytest.mark.asyncio
    async def test_read_maintenance_history_server_error(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", status_code=500)
        with pytest.raises(ConnectorError, match="HTTP 500"):
            await connector.read_maintenance_history("10000001")

    @pytest.mark.asyncio
    async def test_read_maintenance_history_requires_connect(
        self, connector: SapPmConnector
    ) -> None:
        with pytest.raises(ConnectorError, match="Not connected"):
            await connector.read_maintenance_history("10000001")

    @pytest.mark.parametrize("asset_id", ["", None])
    @pytest.mark.asyncio
    async def test_read_maintenance_history_refuses_a_missing_asset_id(
        self, httpx_mock, connector: SapPmConnector, asset_id
    ) -> None:
        """Without an Equipment filter the read would fetch every order."""
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="requires an asset_id"):
            await connector.read_maintenance_history(asset_id)
        assert len(httpx_mock.get_requests()) == 1  # only the connect handshake


# ---------------------------------------------------------------------------
# OData string literals built from caller-supplied IDs
# ---------------------------------------------------------------------------


class TestODataLiteralEscaping:
    """IDs reach the connector from LLM / MCP-client input. Each one must stay a
    single OData string literal: an embedded ``'`` is doubled (``''``), so it can
    neither break the ``$filter`` nor add clauses to it."""

    @pytest.mark.parametrize(
        ("read", "expected_filter"),
        [
            pytest.param(
                lambda c: c.get_asset("O'NEIL-1"),
                "Equipment eq 'O''NEIL-1'",
                id="get_asset",
            ),
            pytest.param(
                lambda c: c.get_work_order("4000'1"),
                "MaintenanceOrder eq '4000''1'",
                id="get_work_order",
            ),
            pytest.param(
                # The status is applied to the orders read; it never reaches SAP.
                lambda c: c.read_work_orders(asset_id="P'1", status="TE'CO"),
                "Equipment eq 'P''1'",
                id="read_work_orders",
            ),
            pytest.param(
                lambda c: c.read_maintenance_history("x' or Equipment ne 'y"),
                "Equipment eq 'x'' or Equipment ne ''y'",
                id="read_maintenance_history",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_quote_in_id_is_doubled(
        self, httpx_mock, connector: SapPmConnector, read, expected_filter: str
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", json={"d": {"results": []}})
        await read(connector)
        assert httpx_mock.get_requests()[-1].url.params["$filter"] == expected_filter

    @pytest.mark.asyncio
    async def test_spare_part_filter_values_are_escaped(self, httpx_mock) -> None:
        custom = SapPmConnector(
            url=BASE,
            auth=BasicAuth(username="u", password="p"),
            bom_material_field="Material",
            bom_equipment_field="Equipment",
        )
        await _connect(httpx_mock, custom)
        httpx_mock.add_response(method="GET", json={"d": {"results": []}})
        await custom.read_spare_parts(asset_id="E'1", sku="M'1")
        sent = httpx_mock.get_requests()[-1].url.params["$filter"]
        assert sent == "Equipment eq 'E''1' and Material eq 'M''1'"

    @pytest.mark.asyncio
    async def test_numeric_id_is_formatted_as_before(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """A workflow event can carry an ID as a number; it is sent as its digits."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", json={"d": {"results": []}})
        await connector.get_asset(10000001)
        sent = httpx_mock.get_requests()[-1].url.params["$filter"]
        assert sent == "Equipment eq '10000001'"

    @pytest.mark.asyncio
    async def test_update_key_stays_inside_its_path_segment(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """A key carrying ``')/../`` must not climb out of ``MaintenanceOrder(...)``."""
        hostile = "1')/../../API_EQUIPMENT/Equipment('X"
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            headers={"x-csrf-token": "csrf-tok"},
            json={"d": {"results": []}},
        )
        httpx_mock.add_response(method="PATCH", status_code=204)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{
                    "$top": "1",
                    "$filter": "MaintenanceOrder eq '1'')/../../API_EQUIPMENT/Equipment(''X'",
                },
            ),
            json={"d": {"results": [{"MaintenanceOrder": hostile}]}},
        )
        await connector.update_work_order(hostile, description="x")
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        assert patch_req.url.raw_path == (
            b"/sap/opu/odata/sap/API_MAINTENANCEORDER/MaintenanceOrder("
            b"'1''%29%2F..%2F..%2FAPI_EQUIPMENT%2FEquipment%28''X')"
        )

    @pytest.mark.asyncio
    async def test_status_function_import_parameter_is_one_literal(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """The order reaches the function import as one ``MaintenanceOrder``
        literal: ``'`` is doubled and ``&`` cannot start another parameter."""
        hostile = "4000'1&MaintenanceOrder='2"
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            headers={"x-csrf-token": "csrf-tok"},
            json={"d": {"results": []}},
        )
        httpx_mock.add_response(method="POST", json={"d": {"IsInvalid": False}})
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$top": "1", "$filter": "MaintenanceOrder eq '4000''1&MaintenanceOrder=''2'"},
            ),
            json={"d": {"results": [_order(hostile, "TECO CNF")]}},
        )
        await connector.update_work_order(hostile, status=WorkOrderStatus.CLOSED)
        post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        assert post.url.path.endswith("/API_MAINTENANCEORDER/SetMaintOrdToTechCompleted")
        assert post.url.params.multi_items() == [
            ("MaintenanceOrder", "'4000''1&MaintenanceOrder=''2'")
        ]


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
                            "SystemStatusText": "REL  PRC  SETC",
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
        assert wo.status == WorkOrderStatus.ASSIGNED

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
    """The order has no writable status property: a status is set through the
    API_MAINTENANCEORDER function import for it, never PATCHed."""

    @staticmethod
    def _mock_csrf(httpx_mock, token: str = "csrf-upd") -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            headers={"x-csrf-token": token},
            json={"d": {"results": []}},
        )

    @staticmethod
    def _mock_action(httpx_mock, function_import: str, **kwargs) -> None:
        httpx_mock.add_response(
            method="POST",
            url=httpx.URL(
                f"{BASE}/API_MAINTENANCEORDER/{function_import}",
                params={"MaintenanceOrder": "'4000001'"},
            ),
            **(kwargs or {"json": {"d": {"IsInvalid": False}}}),
        )

    @staticmethod
    def _mock_refetch(httpx_mock, system_status: str) -> None:
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$top": "1", "$filter": "MaintenanceOrder eq '4000001'"},
            ),
            json={"d": {"results": [_order("4000001", system_status)]}},
        )

    @pytest.mark.parametrize(
        ("status", "function_import", "system_status"),
        [
            (WorkOrderStatus.ASSIGNED, "ReleaseMaintenanceOrder", "REL  MANC PRC  SETC"),
            (WorkOrderStatus.CLOSED, "SetMaintOrdToTechCompleted", "TECO CNF  PRC  SETC"),
            (WorkOrderStatus.CANCELLED, "SetMaintOrdStsToMrkdForDeltn", "CRTD DLFL MANC"),
        ],
    )
    @pytest.mark.asyncio
    async def test_status_is_set_through_its_function_import(
        self, httpx_mock, connector: SapPmConnector, status, function_import, system_status
    ) -> None:
        await _connect(httpx_mock, connector)
        self._mock_csrf(httpx_mock)
        self._mock_action(httpx_mock, function_import)
        self._mock_refetch(httpx_mock, system_status)
        updated = await connector.update_work_order("4000001", status=status)
        assert updated.status == status
        requests = httpx_mock.get_requests()
        assert not any(r.method == "PATCH" for r in requests)
        post = next(r for r in requests if r.method == "POST")
        assert post.headers["X-CSRF-Token"] == "csrf-upd"
        assert post.content == b""  # the parameter travels in the query string
        assert "Content-Type" not in post.headers

    @pytest.mark.parametrize(
        "status",
        [WorkOrderStatus.CREATED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.COMPLETED],
    )
    @pytest.mark.asyncio
    async def test_status_sap_sets_itself_is_refused_before_any_request(
        self, httpx_mock, connector: SapPmConnector, status
    ) -> None:
        """CRTD, PCNF and CNF have no function import; the field update is not
        sent either, so nothing is half-applied."""
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="SAP sets it itself"):
            await connector.update_work_order("4000001", status=status, description="x")
        assert len(httpx_mock.get_requests()) == 1  # only the connect handshake

    @pytest.mark.asyncio
    async def test_fields_are_patched_before_the_status_is_set(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        self._mock_csrf(httpx_mock, "csrf-1")
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder('4000001')",
            status_code=204,
        )
        self._mock_csrf(httpx_mock, "csrf-2")
        self._mock_action(httpx_mock, "SetMaintOrdToTechCompleted")
        self._mock_refetch(httpx_mock, "TECO CNF  PRC  SETC")
        updated = await connector.update_work_order(
            "4000001", status=WorkOrderStatus.CLOSED, description="Bearing replaced"
        )
        writes = [r for r in httpx_mock.get_requests() if r.method in ("PATCH", "POST")]
        assert [r.method for r in writes] == ["PATCH", "POST"]
        assert json.loads(writes[0].content) == {"MaintenanceOrderDesc": "Bearing replaced"}
        assert writes[0].headers["X-CSRF-Token"] == "csrf-1"
        assert writes[1].headers["X-CSRF-Token"] == "csrf-2"
        assert updated.status == WorkOrderStatus.CLOSED

    @pytest.mark.asyncio
    async def test_close_work_order_rejected_by_sap_raises(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        self._mock_csrf(httpx_mock)
        self._mock_action(httpx_mock, "SetMaintOrdToTechCompleted", status_code=400)
        with pytest.raises(ConnectorError, match="SetMaintOrdToTechCompleted failed: HTTP 400"):
            await connector.close_work_order("4000001")

    @pytest.mark.asyncio
    async def test_cancel_work_order_auth_failure(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        self._mock_csrf(httpx_mock)
        self._mock_action(httpx_mock, "SetMaintOrdStsToMrkdForDeltn", status_code=401)
        with pytest.raises(ConnectorAuthError, match="authentication failed"):
            await connector.cancel_work_order("4000001")
