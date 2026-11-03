"""Integration tests for SapPmConnector REST/OData operations.

All HTTP traffic is intercepted by pytest-httpx — no real SAP API calls.
"""

from __future__ import annotations

import json

import httpx
import pytest
from structlog.testing import capture_logs

from machina.connectors.base import ConnectorRegistry
from machina.connectors.cmms.auth import BasicAuth, OAuth2ClientCredentials
from machina.connectors.cmms.sap_pm import SapPmConnector
from machina.domain.asset import Asset
from machina.domain.maintenance_plan import MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderStatus, WorkOrderType
from machina.exceptions import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorUnsupportedFilterError,
)
from machina.workflows.builtins.alarm_to_workorder import alarm_to_workorder
from machina.workflows.engine import WorkflowEngine
from machina.workflows.models import Workflow

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
    system statuses: one line of codes (SAP's example: ``CRTD MACM ORAP PRC``).
    Some lines here use two spaces after a three-letter code; the connector
    splits on any whitespace.
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

    @pytest.mark.asyncio
    async def test_connect_warns_that_asset_filtered_spare_part_reads_are_refused(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """Without bom_equipment_field, connect warns once that asset filters are refused.

        A caller may treat each later refusal as expected and log it quietly,
        so this warning is where the configuration gap surfaces.
        """
        with capture_logs() as logs:
            await _connect(httpx_mock, connector)
        warnings = [e for e in logs if e["event"] == "bom_equipment_field_unconfigured"]
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning["log_level"] == "warning"
        assert warning["connector"] == "SapPmConnector"
        assert warning["operation"] == "connect"
        assert warning["bom_entity_set"] == "BillOfMaterialItem"
        assert "refused with ConnectorUnsupportedFilterError" in warning["message"]
        assert BASE not in str(warning)


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

    @pytest.mark.parametrize("sku", ["", "MAT-001"])
    @pytest.mark.asyncio
    async def test_read_spare_parts_asset_filter_refused_by_default(
        self, httpx_mock, connector: SapPmConnector, sku: str
    ) -> None:
        """Without bom_equipment_field an asset filter is refused, before any request.

        asset_id cannot be filtered server-side. Reading without it would page
        the entire BOM into memory (U9 OOM guard) or, with a sku, pass a bare sku
        match off as one of the asset's parts; answering ``[]`` would read as
        "this asset has no spare parts". The only request is the connect
        handshake from ``_connect``.
        """
        await _connect(httpx_mock, connector)
        with pytest.raises(
            ConnectorUnsupportedFilterError,
            match="cannot filter spare parts by asset: bom_equipment_field is not configured",
        ):
            await connector.read_spare_parts(asset_id="10000001", sku=sku)
        requests = httpx_mock.get_requests()
        assert [(r.method, r.url.path) for r in requests] == [
            ("GET", "/sap/opu/odata/sap/API_EQUIPMENT/$metadata")
        ]

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
    async def test_alarm_workflow_history_step_reaches_sap_pm(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """The built-in workflow's history step must run against SAP PM.

        The engine finds the step's connector by capability. With
        ``read_maintenance_history`` undeclared it found none, and the step's
        SKIP policy hid the miss.
        """
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$filter": "Equipment eq '10000001'"},
            ),
            json={
                "d": {
                    "results": [
                        _order("4000010", "REL  CNF  PRC  SETC"),
                        _order("4000011", "REL  PRC"),
                    ],
                },
            },
        )
        registry = ConnectorRegistry()
        registry.register("sap_pm", connector)
        history_step = next(s for s in alarm_to_workorder.steps if s.name == "check_history")

        result = await WorkflowEngine(registry=registry).execute(
            Workflow(name="history only", steps=[history_step]),
            {"asset_id": "10000001"},
        )

        (step_result,) = result.step_results
        assert not step_result.skipped
        assert [wo.id for wo in step_result.output] == ["4000010"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset_id", [10000001, 0], ids=["number", "zero"])
    async def test_numeric_asset_id_is_sent_as_its_digits(
        self, httpx_mock, connector: SapPmConnector, asset_id: int
    ) -> None:
        """Tool arguments parsed from model JSON can carry an equipment number as a
        number. 0 is an ID too: read as no filter, it returned every equipment's
        orders as its history."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$filter": f"Equipment eq '{asset_id}'"},
            ),
            json={"d": {"results": [_order("4000010", "TECO CNF")]}},
        )
        history = await connector.read_maintenance_history(asset_id)  # type: ignore[arg-type]
        assert [wo.id for wo in history] == ["4000010"]

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
        """A failed read raises; it is not reported as an empty history."""
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

    @pytest.mark.parametrize("asset_id", ["", "  ", None], ids=["empty", "blank", "unresolved"])
    @pytest.mark.asyncio
    async def test_read_maintenance_history_refuses_a_missing_asset_id(
        self, httpx_mock, connector: SapPmConnector, asset_id: str | None
    ) -> None:
        """Without an Equipment filter the read would fetch every order, and
        ``None`` would be sent as ``Equipment eq 'None'``."""
        await _connect(httpx_mock, connector)
        with pytest.raises(
            ConnectorError, match=r"^read_maintenance_history requires an asset_id$"
        ):
            await connector.read_maintenance_history(asset_id)  # type: ignore[arg-type]
        assert len(httpx_mock.get_requests()) == 1  # only the connect handshake


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


_ETAG = "W/\"datetimeoffset'2026-10-01T08%3A00%3A00.0000000Z'\""


class TestUpdateWorkOrder:
    """The order has no writable status property: a status is set through the
    API_MAINTENANCEORDER function import for it, never PATCHed. SAP documents
    that the import needs the order's ETag in If-Match."""

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
    def _mock_read(httpx_mock, system_status: str, etag: str | None = _ETAG) -> None:
        """One read of order 4000001: before the status import (its ETag) or after it."""
        order: dict[str, object] = dict(_order("4000001", system_status))
        if etag is not None:
            order["__metadata"] = {"etag": etag}
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$top": "1", "$filter": "MaintenanceOrder eq '4000001'"},
            ),
            json={"d": {"results": [order]}},
        )

    @pytest.mark.parametrize(
        ("status", "function_import", "before", "system_status"),
        [
            (
                WorkOrderStatus.ASSIGNED,
                "ReleaseMaintenanceOrder",
                "CRTD MANC NMAT",
                "REL MANC PRC",
            ),
            (WorkOrderStatus.CLOSED, "SetMaintOrdToTechCompleted", "REL CNF PRC", "TECO CNF PRC"),
            (WorkOrderStatus.CANCELLED, "SetMaintOrdStsToMrkdForDeltn", "CRTD MANC", "CRTD DLFL"),
        ],
    )
    @pytest.mark.asyncio
    async def test_status_is_set_through_its_function_import(
        self,
        httpx_mock,
        connector: SapPmConnector,
        status,
        function_import,
        before: str,
        system_status: str,
    ) -> None:
        await _connect(httpx_mock, connector)
        self._mock_read(httpx_mock, before)
        self._mock_csrf(httpx_mock)
        self._mock_action(httpx_mock, function_import)
        self._mock_read(httpx_mock, system_status)
        updated = await connector.update_work_order("4000001", status=status)
        assert updated.status == status
        requests = httpx_mock.get_requests()
        assert not any(r.method == "PATCH" for r in requests)
        post = next(r for r in requests if r.method == "POST")
        assert post.headers["X-CSRF-Token"] == "csrf-upd"
        assert post.headers["If-Match"] == _ETAG
        assert post.content == b""  # the parameter travels in the query string
        assert "Content-Type" not in post.headers

    @pytest.mark.asyncio
    async def test_an_order_without_an_etag_is_refused_before_the_import(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """SAP requires If-Match on the import; without the ETag it is not sent."""
        await _connect(httpx_mock, connector)
        self._mock_read(httpx_mock, "REL  MANC", etag=None)
        with pytest.raises(ConnectorError, match="no ETag for work order 4000001"):
            await connector.update_work_order("4000001", status=WorkOrderStatus.CLOSED)
        assert not any(r.method == "POST" for r in httpx_mock.get_requests())

    @pytest.mark.asyncio
    async def test_a_missing_order_is_refused_before_any_write(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$top": "1", "$filter": "MaintenanceOrder eq '4000001'"},
            ),
            json={"d": {"results": []}},
        )
        with pytest.raises(ConnectorError, match="Work order 4000001 not found"):
            await connector.update_work_order("4000001", status=WorkOrderStatus.CLOSED)
        assert {r.method for r in httpx_mock.get_requests()} == {"GET"}

    @pytest.mark.asyncio
    async def test_an_order_changed_since_it_was_read_is_reported(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        self._mock_read(httpx_mock, "REL  MANC")
        self._mock_csrf(httpx_mock)
        self._mock_action(httpx_mock, "SetMaintOrdToTechCompleted", status_code=412)
        with pytest.raises(ConnectorError, match="HTTP 412, work order 4000001 changed"):
            await connector.update_work_order("4000001", status=WorkOrderStatus.CLOSED)

    @pytest.mark.asyncio
    async def test_a_status_sap_did_not_apply_is_an_error(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """SAP answered the import with 200, but the order does not show TECO."""
        await _connect(httpx_mock, connector)
        self._mock_read(httpx_mock, "REL  CNF  PRC")
        self._mock_csrf(httpx_mock)
        self._mock_action(httpx_mock, "SetMaintOrdToTechCompleted")
        self._mock_read(httpx_mock, "REL  CNF  PRC")
        with (
            capture_logs() as logs,
            pytest.raises(ConnectorError, match=r"not 'TECO': the status change was not applied"),
        ):
            await connector.update_work_order("4000001", status=WorkOrderStatus.CLOSED)
        (event,) = [e for e in logs if e["event"] == "work_order_status_not_applied"]
        assert event["actual_status"] == "REL  CNF  PRC"

    @pytest.mark.asyncio
    async def test_a_full_status_line_without_the_code_is_reported_as_unknown(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """SystemStatusText holds 40 characters of the highest-priority statuses;
        when it is full, a missing TECO may only have been left out."""
        full = "REL  CNF  PRC  SETC MANC NMAT GMPS ORAP"  # 39 characters
        await _connect(httpx_mock, connector)
        self._mock_read(httpx_mock, "REL  CNF  PRC")
        self._mock_csrf(httpx_mock)
        self._mock_action(httpx_mock, "SetMaintOrdToTechCompleted")
        self._mock_read(httpx_mock, full)
        with (
            capture_logs() as logs,
            pytest.raises(ConnectorError, match="whether it was applied cannot be read back"),
        ):
            await connector.update_work_order("4000001", status=WorkOrderStatus.CLOSED)
        assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
            "work_order_status_unconfirmed"
        ]

    @pytest.mark.parametrize(
        ("status", "before"),
        [
            pytest.param(WorkOrderStatus.CLOSED, "TECO CNF PRC", id="technically-completed"),
            # Closed for business is closed: no technical completion is sent.
            pytest.param(WorkOrderStatus.CLOSED, "CLSD CNF PRC", id="business-completed"),
            pytest.param(WorkOrderStatus.CANCELLED, "CRTD DLFL", id="deletion-flag"),
            pytest.param(WorkOrderStatus.ASSIGNED, "REL PCNF PRC", id="released"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_status_the_order_already_has_is_not_sent_again(
        self, httpx_mock, connector: SapPmConnector, status, before: str
    ) -> None:
        """A repeated close (after a lost response, say) must not fail on SAP
        refusing to complete a completed order."""
        await _connect(httpx_mock, connector)
        self._mock_read(httpx_mock, before)
        self._mock_read(httpx_mock, before)
        updated = await connector.update_work_order("4000001", status=status)
        assert {r.method for r in httpx_mock.get_requests()} == {"GET"}
        assert updated.id == "4000001"

    @pytest.mark.asyncio
    async def test_a_status_failure_after_the_patch_says_the_patch_was_applied(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        self._mock_csrf(httpx_mock, "csrf-1")
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder('4000001')",
            status_code=204,
        )
        self._mock_read(httpx_mock, "CRTD MANC")
        self._mock_csrf(httpx_mock, "csrf-2")
        self._mock_action(httpx_mock, "SetMaintOrdToTechCompleted", status_code=400)
        with pytest.raises(
            ConnectorError,
            match=r"HTTP 400\. The assigned_to and description update sent before it was "
            r"applied; only the status was not set",
        ):
            await connector.update_work_order(
                "4000001", status=WorkOrderStatus.CLOSED, assigned_to="TECH", description="x"
            )

    @pytest.mark.asyncio
    async def test_an_auth_failure_after_the_patch_stays_an_auth_error(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        self._mock_csrf(httpx_mock, "csrf-1")
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder('4000001')",
            status_code=204,
        )
        self._mock_read(httpx_mock, "CRTD MANC")
        self._mock_csrf(httpx_mock, "csrf-2")
        self._mock_action(httpx_mock, "ReleaseMaintenanceOrder", status_code=401)
        with pytest.raises(ConnectorAuthError, match="description update sent before it"):
            await connector.update_work_order(
                "4000001", status=WorkOrderStatus.ASSIGNED, description="x"
            )

    @pytest.mark.parametrize(
        "reread",
        [
            pytest.param({"status_code": 500}, id="read-fails"),
            pytest.param({"json": {"d": {"results": []}}}, id="order-gone"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_failed_read_back_says_the_update_was_applied(
        self, httpx_mock, connector: SapPmConnector, reread: dict
    ) -> None:
        """Reported as a failed update, it would be sent again."""
        await _connect(httpx_mock, connector)
        self._mock_read(httpx_mock, "CRTD MANC")
        self._mock_csrf(httpx_mock)
        self._mock_action(httpx_mock, "ReleaseMaintenanceOrder")
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$top": "1", "$filter": "MaintenanceOrder eq '4000001'"},
            ),
            is_reusable=True,
            **reread,
        )
        with (
            capture_logs() as logs,
            pytest.raises(ConnectorError, match="do not repeat the update"),
        ):
            await connector.update_work_order("4000001", status=WorkOrderStatus.ASSIGNED)
        assert "work_order_update_not_read_back" in [e["event"] for e in logs]

    @pytest.mark.asyncio
    async def test_a_patch_rejected_for_auth_raises_before_the_status(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        self._mock_csrf(httpx_mock)
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder('4000001')",
            status_code=401,
        )
        with pytest.raises(ConnectorAuthError, match="authentication failed"):
            await connector.update_work_order(
                "4000001", status=WorkOrderStatus.CLOSED, description="x"
            )
        assert not any(r.method == "POST" for r in httpx_mock.get_requests())

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
        # The PATCH changed the order, so its ETag is read after it.
        self._mock_read(httpx_mock, "REL  CNF  PRC  SETC", etag='W/"after-patch"')
        self._mock_csrf(httpx_mock, "csrf-2")
        self._mock_action(httpx_mock, "SetMaintOrdToTechCompleted")
        self._mock_read(httpx_mock, "TECO CNF  PRC  SETC")
        updated = await connector.update_work_order(
            "4000001", status=WorkOrderStatus.CLOSED, description="Bearing replaced"
        )
        writes = [r for r in httpx_mock.get_requests() if r.method in ("PATCH", "POST")]
        assert [r.method for r in writes] == ["PATCH", "POST"]
        assert json.loads(writes[0].content) == {"MaintenanceOrderDesc": "Bearing replaced"}
        assert writes[0].headers["X-CSRF-Token"] == "csrf-1"
        assert "If-Match" not in writes[0].headers
        assert writes[1].headers["X-CSRF-Token"] == "csrf-2"
        assert writes[1].headers["If-Match"] == 'W/"after-patch"'
        assert updated.status == WorkOrderStatus.CLOSED

    @pytest.mark.asyncio
    async def test_close_work_order_rejected_by_sap_raises(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        self._mock_read(httpx_mock, "REL  MANC")
        self._mock_csrf(httpx_mock)
        self._mock_action(httpx_mock, "SetMaintOrdToTechCompleted", status_code=400)
        with pytest.raises(ConnectorError, match="SetMaintOrdToTechCompleted failed: HTTP 400"):
            await connector.close_work_order("4000001")

    @pytest.mark.asyncio
    async def test_cancel_work_order_auth_failure(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        self._mock_read(httpx_mock, "CRTD MANC")
        self._mock_csrf(httpx_mock)
        self._mock_action(httpx_mock, "SetMaintOrdStsToMrkdForDeltn", status_code=401)
        with pytest.raises(ConnectorAuthError, match="authentication failed"):
            await connector.cancel_work_order("4000001")


# ---------------------------------------------------------------------------
# Caller-supplied values stay inside their OData string literal
# ---------------------------------------------------------------------------

# Closes the literal and appends a clause every row satisfies: interpolated
# as-is, ``Equipment eq '<value>'`` becomes an unbounded read.
_BREAKOUT = "X' or Equipment ne '"
# The same value as an OData string literal, its single quotes doubled.
_BREAKOUT_LITERAL = "'X'' or Equipment ne '''"


class TestFilterValuesStayLiteral:
    """IDs, SKUs and raw statuses reach these filters from LLM and MCP tool input.

    Each response is mocked only at the escaped URL, so an unescaped request
    finds no match and fails the test.
    """

    @pytest.mark.asyncio
    async def test_get_asset(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_EQUIPMENT",
                "Equipment",
                **{"$top": "1", "$filter": f"Equipment eq {_BREAKOUT_LITERAL}"},
            ),
            json={"d": {"results": []}},
        )
        assert await connector.get_asset(_BREAKOUT) is None

    @pytest.mark.asyncio
    async def test_numeric_id_is_quoted_like_its_string(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """Tool arguments parsed from model JSON can carry an equipment number as a number."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_EQUIPMENT",
                "Equipment",
                **{"$top": "1", "$filter": "Equipment eq '10000001'"},
            ),
            json={"d": {"results": []}},
        )
        assert await connector.get_asset(10000001) is None

    @pytest.mark.asyncio
    async def test_percent_encoded_quote_stays_data(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """A pre-encoded ``%27`` reaches the gateway as three characters, not a quote."""
        await _connect(httpx_mock, connector)
        smuggled = "x%27 or 1 eq 1 or Equipment eq %27y"
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_EQUIPMENT",
                "Equipment",
                **{"$top": "1", "$filter": f"Equipment eq '{smuggled}'"},
            ),
            json={"d": {"results": []}},
        )
        assert await connector.get_asset(smuggled) is None
        # httpx encodes the "%" itself, so one round of decoding yields "%27" again.
        assert "%2527" in str(httpx_mock.get_requests()[-1].url)

    @pytest.mark.asyncio
    async def test_read_work_orders(self, httpx_mock, connector: SapPmConnector) -> None:
        """The status is applied to the orders read; it never reaches SAP."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$filter": f"Equipment eq {_BREAKOUT_LITERAL}"},
            ),
            json={"d": {"results": []}},
        )
        assert await connector.read_work_orders(asset_id=_BREAKOUT, status=_BREAKOUT) == []

    @pytest.mark.asyncio
    async def test_get_work_order(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$top": "1", "$filter": f"MaintenanceOrder eq {_BREAKOUT_LITERAL}"},
            ),
            json={"d": {"results": []}},
        )
        assert await connector.get_work_order(_BREAKOUT) is None

    @pytest.mark.asyncio
    async def test_read_spare_parts(self, httpx_mock) -> None:
        conn = SapPmConnector(
            url=BASE,
            auth=BasicAuth(username="u", password="p"),
            bom_equipment_field="Equipment",
        )
        await _connect(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_BILL_OF_MATERIAL_SRV",
                "BillOfMaterialItem",
                **{
                    "$filter": f"Equipment eq {_BREAKOUT_LITERAL} and "
                    f"BillOfMaterialComponent eq {_BREAKOUT_LITERAL}"
                },
            ),
            json={"d": {"results": []}},
        )
        assert await conn.read_spare_parts(asset_id=_BREAKOUT, sku=_BREAKOUT) == []

    @pytest.mark.asyncio
    async def test_read_maintenance_history(self, httpx_mock, connector: SapPmConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_odata_url(
                "API_MAINTENANCEORDER",
                "MaintenanceOrder",
                **{"$filter": f"Equipment eq {_BREAKOUT_LITERAL}"},
            ),
            json={"d": {"results": []}},
        )
        assert await connector.read_maintenance_history(_BREAKOUT) == []

    @pytest.mark.asyncio
    async def test_update_work_order_key_predicate(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """The key can neither close its literal nor leave its path segment.

        Interpolated as-is, this ID makes the URL path
        ``MaintenanceOrder('1')/API_EQUIPMENT/Equipment('10000001')``.
        """
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            headers={"x-csrf-token": "csrf-upd"},
            json={"d": {"results": []}},
        )
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder("
            "'1''%29%2FAPI_EQUIPMENT%2FEquipment%28''10000001')",
            status_code=404,
        )
        with pytest.raises(ConnectorError, match="HTTP 404"):
            await connector.update_work_order(
                "1')/API_EQUIPMENT/Equipment('10000001", description="x"
            )

    @pytest.mark.asyncio
    async def test_update_work_order_refuses_a_dot_segment_key_before_any_request(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """Interpolated as-is, this ID makes the URL path
        ``MaintenanceOrder('1')/../../API_EQUIPMENT/Equipment('10000001')``,
        which the HTTP client normalizes into a PATCH on an equipment record;
        percent-encoded, a proxy that decodes ``%2F`` could still do so.
        """
        await _connect(httpx_mock, connector)
        sent = len(httpx_mock.get_requests())
        with pytest.raises(ConnectorError, match="Invalid record ID"):
            await connector.update_work_order(
                "1')/../../API_EQUIPMENT/Equipment('10000001", description="x"
            )
        assert len(httpx_mock.get_requests()) == sent

    @pytest.mark.asyncio
    async def test_status_function_import_parameter_is_one_literal(
        self, httpx_mock, connector: SapPmConnector
    ) -> None:
        """The order reaches the function import as one ``MaintenanceOrder``
        literal: ``'`` is doubled and ``&`` cannot start another parameter."""
        hostile = "4000'1&MaintenanceOrder='2"
        read_url = _odata_url(
            "API_MAINTENANCEORDER",
            "MaintenanceOrder",
            **{"$top": "1", "$filter": "MaintenanceOrder eq '4000''1&MaintenanceOrder=''2'"},
        )
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=read_url,
            json={
                "d": {"results": [{**_order(hostile, "REL  MANC"), "__metadata": {"etag": _ETAG}}]}
            },
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
            headers={"x-csrf-token": "csrf-tok"},
            json={"d": {"results": []}},
        )
        httpx_mock.add_response(method="POST", json={"d": {"IsInvalid": False}})
        httpx_mock.add_response(
            method="GET",
            url=read_url,
            json={"d": {"results": [_order(hostile, "TECO CNF")]}},
        )
        await connector.update_work_order(hostile, status=WorkOrderStatus.CLOSED)
        post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        assert post.url.path.endswith("/API_MAINTENANCEORDER/SetMaintOrdToTechCompleted")
        assert post.url.params.multi_items() == [
            ("MaintenanceOrder", "'4000''1&MaintenanceOrder=''2'")
        ]
