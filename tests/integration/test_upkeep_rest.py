"""Integration tests for UpKeepConnector REST operations.

All HTTP traffic is intercepted by pytest-httpx — no real UpKeep API calls.
"""

from __future__ import annotations

import pytest
import structlog.testing

from machina.connectors.base import ConnectorRegistry
from machina.connectors.cmms.upkeep import UpKeepConnector
from machina.domain.asset import Asset
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderStatus, WorkOrderType
from machina.exceptions import ConnectorAuthError, ConnectorError, ConnectorSchemaError
from machina.workflows.builtins.alarm_to_workorder import alarm_to_workorder
from machina.workflows.engine import WorkflowEngine
from machina.workflows.models import Workflow

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

    @pytest.mark.asyncio
    async def test_read_work_orders_maps_documented_statuses(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """UpKeep reports ``onHold`` and ``inProgress``; neither may read as CREATED."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders?limit=100&offset=0",
            json={
                "results": [
                    {"id": "wo1", "title": "Fix pump", "status": "open", "assetId": "a1"},
                    {"id": "wo2", "title": "Fix pump", "status": "onHold", "assetId": "a1"},
                    {"id": "wo3", "title": "Fix pump", "status": "inProgress", "assetId": "a1"},
                    {"id": "wo4", "title": "Fix pump", "status": "complete", "assetId": "a1"},
                ],
            },
        )
        wos = await connector.read_work_orders()
        assert [wo.status for wo in wos] == [
            WorkOrderStatus.CREATED,
            WorkOrderStatus.ASSIGNED,
            WorkOrderStatus.IN_PROGRESS,
            WorkOrderStatus.COMPLETED,
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "upkeep_status"),
        [
            (WorkOrderStatus.CREATED, "open"),
            (WorkOrderStatus.ASSIGNED, "onHold"),
            (WorkOrderStatus.IN_PROGRESS, "inProgress"),
            (WorkOrderStatus.COMPLETED, "complete"),
            (WorkOrderStatus.CLOSED, "complete"),
            (WorkOrderStatus.CANCELLED, "onHold"),
        ],
    )
    async def test_status_filter_sends_documented_value(
        self,
        httpx_mock,
        connector: UpKeepConnector,
        status: WorkOrderStatus,
        upkeep_status: str,
    ) -> None:
        """The ``status`` filter accepts only open, onHold, inProgress and complete."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders?limit=100&offset=0&status={upkeep_status}",
            json={"results": []},
        )
        assert await connector.read_work_orders(status=status) == []


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

    @pytest.mark.asyncio
    async def test_empty_asset_id_reads_the_inventory(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """``asset_id=""`` is no filter: the inventory is paged, no asset is read."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/parts?limit=100&offset=0",
            json={"results": [{"id": "p1", "name": "Bearing"}]},
        )
        parts = await connector.read_spare_parts(asset_id="", sku="")
        assert [p.sku for p in parts] == ["p1"]
        assert _sent(httpx_mock) == [f"{BASE}/api/v2/parts?limit=100&offset=0"]

    @pytest.mark.asyncio
    async def test_an_inventory_part_that_cannot_be_mapped_refuses_the_read(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """No partial inventory, and no vendor value in the error or the logs."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/parts?limit=100&offset=0",
            json={
                "results": [
                    {"id": "p1", "name": "Bearing", "quantity": "5"},
                    {"id": "p2", "name": "Seal", "quantity": "many-SECRET"},
                ]
            },
        )

        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectorSchemaError) as exc_info,
        ):
            await connector.read_spare_parts()

        assert str(exc_info.value) == (
            "UpKeep returned a malformed part record: cannot read its spare parts"
        )
        assert exc_info.value.__cause__ is None
        (refusal,) = [e for e in logs if e["event"] == "upkeep_spare_parts_refused"]
        assert refusal["log_level"] == "warning"
        assert refusal["reason"] == "malformed_part"
        assert refusal["position"] == 1
        assert "asset_id" not in refusal
        assert "SECRET" not in repr(logs)


# ---------------------------------------------------------------------------
# Read spare parts for an asset (GET the asset, then each part it lists)
# ---------------------------------------------------------------------------

ASSET_ID = "fjQXLsOvG7"
ASSET_URL = f"{BASE}/api/v2/assets/{ASSET_ID}"
PART_URL = f"{BASE}/api/v2/parts"
REFUSED = "upkeep_spare_parts_refused"


def _sent(httpx_mock) -> list[str]:
    """The URLs requested after the connect check, in order."""
    return [str(r.url) for r in httpx_mock.get_requests()[1:]]


def _asset_body(**result: object) -> dict[str, object]:
    """Response of "Get a Specific Asset", GET /api/v2/assets/<ID>.

    Taken from https://developers.onupkeep.com/, without the file objects and
    ``customFields``. ``parts`` holds the IDs of the parts the asset lists.
    Keyword arguments replace fields of ``result``.
    """
    record: dict[str, object] = {
        "id": ASSET_ID,
        "serial": "S5614B09118",
        "serialNumber": "SER-100",
        "model": "Deluxe 5500",
        "name": "The Ultra Power Generator",
        "barcode": "S5614B09118",
        "location": "OPkIOYYZ0F",
        "status": "active",
        "category": "Generators",
        "parentAsset": "0lcpd45AKe",
        "subAssets": ["z40xOYM9yd"],
        "parts": ["ydOS2PdR3a"],
        "files": ["sKrlT2f3mf", "Vgwf41LLg1"],
        "formItems": ["Ia536KS4RZ"],
        "updatedAt": "2017-11-16T15:01:01",
        "createdAt": "2017-11-16T15:01:01",
    }
    record.update(result)
    return {"success": True, "result": record}


def _part_body(part_id: object, **result: object) -> dict[str, object]:
    """Response of "Get a Specific Part", GET /api/v2/parts/<ID>, for ``part_id``.

    Taken from https://developers.onupkeep.com/, without the image object and
    ``customFieldsPart``. Keyword arguments replace fields of ``result``.
    """
    record: dict[str, object] = {
        "id": part_id,
        "name": "Flux Capacitor",
        "description": "This is what makes time travel possible.",
        "cost": "88.00",
        "quantity": "5",
        "minimumQuantity": "1",
        "partId": "654a1b433dc9596ef9af2a1f",
        "partName": "PT-1",
        "serial": "40161502",
        "area": "Center console",
        "category": "Filter",
        "location": "jmHFWGVgd1",
        "createdByUser": "pCmWGVgfW",
        "files": ["MUfUCa0JBo", "FGyvo6raqZ"],
        "createdAt": "2017-10-23T23:51:58.097Z",
        "updatedAt": "2017-10-23T23:51:58.097Z",
    }
    record.update(result)
    return {"success": True, "result": record}


def _mock_asset(httpx_mock, body: object = None, *, status_code: int = 200) -> None:
    httpx_mock.add_response(method="GET", url=ASSET_URL, status_code=status_code, json=body)


def _mock_part(httpx_mock, part_id: str, body: object = None, *, status_code: int = 200) -> None:
    if body is None and status_code == 200:
        body = _part_body(part_id)
    httpx_mock.add_response(
        method="GET", url=f"{PART_URL}/{part_id}", status_code=status_code, json=body
    )


def _refusal(logs: list[dict[str, object]], reason: str) -> dict[str, object]:
    """The one refusal warning of the read, checked for its fixed fields."""
    (entry,) = [e for e in logs if e["event"] == REFUSED]
    assert entry["log_level"] == "warning"
    assert entry["connector"] == "UpKeepConnector"
    assert entry["operation"] == "read_spare_parts"
    assert entry["asset_id"] == ASSET_ID
    assert entry["reason"] == reason
    return entry


def _assert_value_free(
    message: str, logs: list[dict[str, object]], *part_ids: str, sku: str = ""
) -> None:
    """The error names no caller value, part ID or URL; the logs no part ID, SKU or URL."""
    for value in (ASSET_ID, *part_ids, BASE, "/api/v2/"):
        assert value not in message
    for entry in logs:
        text = repr(entry)
        for value in (*part_ids, BASE, "/api/v2/"):
            assert value not in text
        if sku:
            assert sku not in text


class TestReadSparePartsByAsset:
    """``asset_id`` reads the asset's ``parts`` list, then each listed part.

    UpKeep's ``/api/v2/parts`` has no asset filter; the asset record lists the
    IDs of its parts. The read costs 1 + k requests for k listed parts and is
    refused whole when any part of it cannot be read.
    """

    @pytest.mark.asyncio
    async def test_reads_each_listed_part(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, _asset_body(parts=["ydOS2PdR3a", "Qm7Tz2XbLp"]))
        _mock_part(httpx_mock, "ydOS2PdR3a")
        _mock_part(httpx_mock, "Qm7Tz2XbLp", _part_body("Qm7Tz2XbLp", name="Oil filter"))

        parts = await connector.read_spare_parts(asset_id=ASSET_ID)

        assert _sent(httpx_mock) == [
            ASSET_URL,
            f"{PART_URL}/ydOS2PdR3a",
            f"{PART_URL}/Qm7Tz2XbLp",
        ]
        assert all(isinstance(p, SparePart) for p in parts)
        # The documented part carries neither partNumber nor barcode: its id is the SKU.
        assert [p.sku for p in parts] == ["ydOS2PdR3a", "Qm7Tz2XbLp"]
        assert [p.name for p in parts] == ["Flux Capacitor", "Oil filter"]
        assert parts[0].stock_quantity == 5
        assert parts[0].unit_cost == 88.0
        assert parts[0].warehouse_location == "Center console"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("listed", ["missing", None, []], ids=["missing", "null", "empty"])
    async def test_asset_without_parts_returns_empty_after_one_request(
        self, httpx_mock, connector: UpKeepConnector, listed: object
    ) -> None:
        """An asset that lists no parts has none: a legitimate empty result."""
        await _connect(httpx_mock, connector)
        body = _asset_body(parts=listed)
        if listed == "missing":
            del body["result"]["parts"]
        _mock_asset(httpx_mock, body)

        assert await connector.read_spare_parts(asset_id=ASSET_ID) == []
        assert _sent(httpx_mock) == [ASSET_URL]

    @pytest.mark.asyncio
    async def test_unknown_asset_is_refused(self, httpx_mock, connector: UpKeepConnector) -> None:
        """A 404 for the asset is not "no parts": the read is refused."""
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, status_code=404)

        with structlog.testing.capture_logs() as logs, pytest.raises(ConnectorError) as exc_info:
            await connector.read_spare_parts(asset_id=ASSET_ID)

        assert type(exc_info.value) is ConnectorError
        assert "asset not found" in str(exc_info.value)
        assert _sent(httpx_mock) == [ASSET_URL]
        _refusal(logs, "asset_not_found")
        _assert_value_free(str(exc_info.value), logs)

    @pytest.mark.asyncio
    async def test_listed_part_not_found_refuses_the_whole_read(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """A listed part UpKeep cannot return ends the read: no partial list."""
        await _connect(httpx_mock, connector)
        listed = ["ydOS2PdR3a", "Qm7Tz2XbLp", "Hc4Wv8NsJd"]
        _mock_asset(httpx_mock, _asset_body(parts=listed))
        _mock_part(httpx_mock, "ydOS2PdR3a")
        _mock_part(httpx_mock, "Qm7Tz2XbLp", status_code=404)

        with structlog.testing.capture_logs() as logs, pytest.raises(ConnectorError) as exc_info:
            await connector.read_spare_parts(asset_id=ASSET_ID)

        assert type(exc_info.value) is ConnectorError
        assert "HTTP 404" in str(exc_info.value)
        assert _sent(httpx_mock) == [
            ASSET_URL,
            f"{PART_URL}/ydOS2PdR3a",
            f"{PART_URL}/Qm7Tz2XbLp",
        ]
        assert _refusal(logs, "listed_part_not_found")["position"] == 1
        _assert_value_free(str(exc_info.value), logs, *listed)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param([ASSET_ID], id="body-array"),
            pytest.param({"success": True, "result": None}, id="result-null"),
            pytest.param({"success": True, "result": ["ydOS2PdR3a"]}, id="result-array"),
            pytest.param(_asset_body(parts="ydOS2PdR3a"), id="parts-string"),
            pytest.param(_asset_body(parts={"id": "ydOS2PdR3a"}), id="parts-object"),
            pytest.param(_asset_body(parts=["ydOS2PdR3a", 12345]), id="entry-number"),
            pytest.param(_asset_body(parts=["ydOS2PdR3a", None]), id="entry-null"),
            pytest.param(_asset_body(parts=["ydOS2PdR3a", ""]), id="entry-empty"),
            pytest.param(
                _asset_body(parts=[{"id": "ydOS2PdR3a", "name": "Flux Capacitor"}]),
                id="entry-object",
            ),
            pytest.param(_asset_body(parts=["ydOS2PdR3a", ".."]), id="entry-dot-segment"),
            pytest.param(
                _asset_body(parts=["ydOS2PdR3a", f"../assets/{ASSET_ID}"]),
                id="entry-traversal",
            ),
            # Not this asset's record: it cannot say the asset lists no parts.
            pytest.param({}, id="empty-object"),
            pytest.param({"success": True}, id="no-result"),
            pytest.param(_asset_body(id="Zq8RtW3mKe", parts=[]), id="another-asset"),
            pytest.param(_asset_body(id=None, parts=[]), id="id-null"),
        ],
    )
    async def test_malformed_asset_record_is_refused_before_any_part_request(
        self, httpx_mock, connector: UpKeepConnector, body: object
    ) -> None:
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, body)

        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectorSchemaError) as exc_info,
        ):
            await connector.read_spare_parts(asset_id=ASSET_ID)

        assert _sent(httpx_mock) == [ASSET_URL]
        _refusal(logs, "malformed_asset")
        _assert_value_free(str(exc_info.value), logs, "ydOS2PdR3a")

    @pytest.mark.asyncio
    async def test_a_refusal_with_a_sku_names_neither_the_sku_nor_the_parts(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        listed = ["ydOS2PdR3a", "Qm7Tz2XbLp"]
        _mock_asset(httpx_mock, _asset_body(parts=listed))
        _mock_part(httpx_mock, "ydOS2PdR3a")
        _mock_part(httpx_mock, "Qm7Tz2XbLp", status_code=404)

        with structlog.testing.capture_logs() as logs, pytest.raises(ConnectorError) as exc_info:
            await connector.read_spare_parts(asset_id=ASSET_ID, sku="SKU-SECRET-6205")

        _refusal(logs, "listed_part_not_found")
        assert "SKU-SECRET-6205" not in str(exc_info.value)
        _assert_value_free(str(exc_info.value), logs, *listed, sku="SKU-SECRET-6205")

    @pytest.mark.asyncio
    async def test_numeric_asset_id_is_matched_as_its_text(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """A workflow event or model JSON can carry an ID as a number."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET", url=f"{BASE}/api/v2/assets/11430", json=_asset_body(id="11430", parts=[])
        )
        assert await connector.read_spare_parts(asset_id=11430) == []  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_non_json_asset_body_is_refused(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", url=ASSET_URL, text="<html>maintenance</html>")

        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectorSchemaError) as exc_info,
        ):
            await connector.read_spare_parts(asset_id=ASSET_ID)

        assert _sent(httpx_mock) == [ASSET_URL]
        _refusal(logs, "malformed_asset")
        _assert_value_free(str(exc_info.value), logs)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(["ydOS2PdR3a"], id="body-array"),
            pytest.param({"success": True, "result": None}, id="result-null"),
            pytest.param({"success": True, "result": ["ydOS2PdR3a"]}, id="result-array"),
            pytest.param({"success": True, "result": {"name": "Flux Capacitor"}}, id="no-id"),
            pytest.param(_part_body(12345), id="id-number"),
        ],
    )
    async def test_malformed_part_refuses_the_whole_read(
        self, httpx_mock, connector: UpKeepConnector, body: object
    ) -> None:
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, _asset_body(parts=["ydOS2PdR3a", "Qm7Tz2XbLp"]))
        _mock_part(httpx_mock, "ydOS2PdR3a", body)

        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectorSchemaError) as exc_info,
        ):
            await connector.read_spare_parts(asset_id=ASSET_ID)

        assert _sent(httpx_mock) == [ASSET_URL, f"{PART_URL}/ydOS2PdR3a"]
        assert _refusal(logs, "malformed_part")["position"] == 0
        _assert_value_free(str(exc_info.value), logs, "ydOS2PdR3a", "Qm7Tz2XbLp")

    @pytest.mark.asyncio
    async def test_another_parts_record_refuses_the_whole_read(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """A part request answered with another part's record is an inconsistent association."""
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, _asset_body(parts=["ydOS2PdR3a", "Qm7Tz2XbLp"]))
        _mock_part(httpx_mock, "ydOS2PdR3a", _part_body("Hc4Wv8NsJd"))

        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectorSchemaError) as exc_info,
        ):
            await connector.read_spare_parts(asset_id=ASSET_ID)

        assert _sent(httpx_mock) == [ASSET_URL, f"{PART_URL}/ydOS2PdR3a"]
        assert _refusal(logs, "part_id_mismatch")["position"] == 0
        _assert_value_free(str(exc_info.value), logs, "ydOS2PdR3a", "Qm7Tz2XbLp", "Hc4Wv8NsJd")

    @pytest.mark.asyncio
    async def test_a_part_listed_twice_is_fetched_and_returned_once(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, _asset_body(parts=["ydOS2PdR3a", "Qm7Tz2XbLp", "ydOS2PdR3a"]))
        _mock_part(httpx_mock, "ydOS2PdR3a")
        _mock_part(httpx_mock, "Qm7Tz2XbLp")

        parts = await connector.read_spare_parts(asset_id=ASSET_ID)

        assert [p.sku for p in parts] == ["ydOS2PdR3a", "Qm7Tz2XbLp"]
        assert _sent(httpx_mock) == [
            ASSET_URL,
            f"{PART_URL}/ydOS2PdR3a",
            f"{PART_URL}/Qm7Tz2XbLp",
        ]

    @pytest.mark.asyncio
    async def test_sku_narrows_the_assets_parts(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """Both filters hold: the listed part with that SKU, and only it."""
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, _asset_body(parts=["ydOS2PdR3a", "Qm7Tz2XbLp"]))
        _mock_part(httpx_mock, "ydOS2PdR3a")
        _mock_part(httpx_mock, "Qm7Tz2XbLp", _part_body("Qm7Tz2XbLp", name="Oil filter"))

        parts = await connector.read_spare_parts(asset_id=ASSET_ID, sku="Qm7Tz2XbLp")

        assert [(p.sku, p.name) for p in parts] == [("Qm7Tz2XbLp", "Oil filter")]

    @pytest.mark.asyncio
    async def test_sku_not_among_the_assets_parts_returns_empty(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """A SKU the asset does not list matches none of its parts; the inventory is not read."""
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, _asset_body(parts=["ydOS2PdR3a", "Qm7Tz2XbLp"]))
        _mock_part(httpx_mock, "ydOS2PdR3a")
        _mock_part(httpx_mock, "Qm7Tz2XbLp")

        assert await connector.read_spare_parts(asset_id=ASSET_ID, sku="SKF-6205") == []
        assert _sent(httpx_mock) == [
            ASSET_URL,
            f"{PART_URL}/ydOS2PdR3a",
            f"{PART_URL}/Qm7Tz2XbLp",
        ]

    @pytest.mark.asyncio
    async def test_asset_server_error(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, status_code=500)

        with pytest.raises(ConnectorError, match=r"^UpKeep GET asset failed: HTTP 500$"):
            await connector.read_spare_parts(asset_id=ASSET_ID)
        assert _sent(httpx_mock) == [ASSET_URL]

    @pytest.mark.asyncio
    async def test_part_server_error_refuses_the_whole_read(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, _asset_body(parts=["ydOS2PdR3a", "Qm7Tz2XbLp"]))
        _mock_part(httpx_mock, "ydOS2PdR3a", status_code=500)

        with pytest.raises(ConnectorError, match=r"^UpKeep GET part failed: HTTP 500$"):
            await connector.read_spare_parts(asset_id=ASSET_ID)
        assert _sent(httpx_mock) == [ASSET_URL, f"{PART_URL}/ydOS2PdR3a"]

    @pytest.mark.asyncio
    async def test_asset_auth_failure(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, status_code=401)

        with pytest.raises(ConnectorAuthError, match="invalid"):
            await connector.read_spare_parts(asset_id=ASSET_ID)

    @pytest.mark.asyncio
    async def test_part_auth_failure(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, _asset_body(parts=["ydOS2PdR3a"]))
        _mock_part(httpx_mock, "ydOS2PdR3a", status_code=401)

        with pytest.raises(ConnectorAuthError, match="invalid"):
            await connector.read_spare_parts(asset_id=ASSET_ID)

    @pytest.mark.asyncio
    async def test_ids_are_percent_encoded_path_segments(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """The caller's asset ID and UpKeep's part IDs each stay one path segment."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/assets/a1%2Fparts%3Fx",
            json=_asset_body(id="a1/parts?x", parts=["p 1/x#y"]),
        )
        httpx_mock.add_response(
            method="GET", url=f"{PART_URL}/p%201%2Fx%23y", json=_part_body("p 1/x#y")
        )

        parts = await connector.read_spare_parts(asset_id="a1/parts?x")

        assert [p.sku for p in parts] == ["p 1/x#y"]
        assert _sent(httpx_mock) == [
            f"{BASE}/api/v2/assets/a1%2Fparts%3Fx",
            f"{PART_URL}/p%201%2Fx%23y",
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset_id", ["..", "../parts", "a1/../.."])
    async def test_dot_segment_asset_id_is_refused_before_any_request(
        self, httpx_mock, connector: UpKeepConnector, asset_id: str
    ) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="Invalid record ID"):
            await connector.read_spare_parts(asset_id=asset_id)
        assert _sent(httpx_mock) == []

    @pytest.mark.asyncio
    async def test_more_than_100_parts_is_refused_before_any_part_request(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """The read stays bounded: an asset listing 101 parts would cost 102 requests."""
        await _connect(httpx_mock, connector)
        listed = [f"part{i:03d}" for i in range(101)]
        _mock_asset(httpx_mock, _asset_body(parts=listed))

        with structlog.testing.capture_logs() as logs, pytest.raises(ConnectorError) as exc_info:
            await connector.read_spare_parts(asset_id=ASSET_ID)

        assert "more than 100 parts" in str(exc_info.value)
        assert _sent(httpx_mock) == [ASSET_URL]
        assert _refusal(logs, "too_many_asset_parts")["part_count"] == 101
        _assert_value_free(str(exc_info.value), logs, *listed)

    @pytest.mark.asyncio
    async def test_100_distinct_parts_are_read(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """The limit counts distinct parts: 101 entries naming 100 parts are read."""
        await _connect(httpx_mock, connector)
        distinct = [f"part{i:03d}" for i in range(100)]
        _mock_asset(httpx_mock, _asset_body(parts=[*distinct, distinct[0]]))
        for part_id in distinct:
            _mock_part(httpx_mock, part_id)

        parts = await connector.read_spare_parts(asset_id=ASSET_ID)

        assert [p.sku for p in parts] == distinct
        assert len(_sent(httpx_mock)) == 101

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "asset_id", [None, "  ", [ASSET_ID], True], ids=["unresolved", "blank", "list", "bool"]
    )
    async def test_asset_filter_that_is_not_an_id_is_refused_before_any_request(
        self, httpx_mock, connector: UpKeepConnector, asset_id: object
    ) -> None:
        """``None`` (an unresolved workflow placeholder) is not "no filter"."""
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError) as exc_info:
            await connector.read_spare_parts(asset_id=asset_id)  # type: ignore[arg-type]
        assert type(exc_info.value) is ConnectorError
        assert str(exc_info.value).startswith("read_spare_parts: the asset_id filter is not an ID")
        assert _sent(httpx_mock) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fields",
        [
            {"quantity": "N/A-SECRET"},
            {"cost": "eighty-SECRET"},
            {"quantity": "-3"},
            {"quantity": None},
        ],
        ids=["quantity-text", "cost-text", "quantity-negative", "quantity-null"],
    )
    async def test_a_part_whose_fields_cannot_be_mapped_refuses_the_whole_read(
        self, httpx_mock, connector: UpKeepConnector, fields: dict[str, object]
    ) -> None:
        """The mapping error is raised as a typed refusal that repeats no vendor value."""
        await _connect(httpx_mock, connector)
        listed = ["ydOS2PdR3a", "Qm7Tz2XbLp", "Hc4Wv8NsJd"]
        _mock_asset(httpx_mock, _asset_body(parts=listed))
        _mock_part(httpx_mock, "ydOS2PdR3a")
        _mock_part(httpx_mock, "Qm7Tz2XbLp", _part_body("Qm7Tz2XbLp", **fields))

        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectorSchemaError) as exc_info,
        ):
            await connector.read_spare_parts(asset_id=ASSET_ID)

        assert str(exc_info.value) == (
            "UpKeep returned a malformed record for a part the asset lists: "
            "its spare parts cannot be read in full, so none are returned"
        )
        # The mapper's own message quotes the vendor value: it must not ride along.
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__ is True
        assert _sent(httpx_mock) == [ASSET_URL, f"{PART_URL}/ydOS2PdR3a", f"{PART_URL}/Qm7Tz2XbLp"]
        refusal = _refusal(logs, "malformed_part")
        assert refusal["position"] == 1
        assert refusal["error_type"] in {"ValueError", "TypeError", "ValidationError"}
        _assert_value_free(str(exc_info.value), logs, *listed)
        assert "SECRET" not in repr(logs)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", ["connect", "read-timeout"])
    async def test_a_transport_failure_after_a_part_was_read_returns_nothing(
        self, httpx_mock, monkeypatch, connector: UpKeepConnector, error: str
    ) -> None:
        """A part request that never gets an answer fails the whole read.

        The transport error propagates as it does from the connector's other
        reads, after the retries, and the parts already read are not returned
        as the asset's list. The retry warnings name no part.
        """
        import httpx

        async def _no_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr("machina.connectors.cmms.retry.asyncio.sleep", _no_sleep)
        exc_type = httpx.ConnectError if error == "connect" else httpx.ReadTimeout
        await _connect(httpx_mock, connector)
        listed = ["ydOS2PdR3a", "Qm7Tz2XbLp", "Hc4Wv8NsJd"]
        _mock_asset(httpx_mock, _asset_body(parts=listed))
        _mock_part(httpx_mock, "ydOS2PdR3a")
        for _ in range(4):  # the request and its three retries
            httpx_mock.add_exception(
                exc_type("connection lost"), method="GET", url=f"{PART_URL}/Qm7Tz2XbLp"
            )

        with structlog.testing.capture_logs() as logs, pytest.raises(exc_type):
            await connector.read_spare_parts(asset_id=ASSET_ID)

        assert _sent(httpx_mock) == [
            ASSET_URL,
            f"{PART_URL}/ydOS2PdR3a",
            *[f"{PART_URL}/Qm7Tz2XbLp"] * 4,
        ]
        retries = [e for e in logs if e["event"] == "http_network_retry"]
        assert len(retries) == 3
        # The endpoint is named; the part and the configured host are not.
        assert {e["url"] for e in retries} == {"/api/v2/parts/<listed part>"}
        for value in (*listed, BASE):
            assert value not in repr(logs)

    @pytest.mark.asyncio
    async def test_a_rate_limited_part_request_logs_no_part(
        self, httpx_mock, monkeypatch, connector: UpKeepConnector
    ) -> None:
        async def _no_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr("machina.connectors.cmms.retry.asyncio.sleep", _no_sleep)
        await _connect(httpx_mock, connector)
        _mock_asset(httpx_mock, _asset_body(parts=["ydOS2PdR3a"]))
        httpx_mock.add_response(method="GET", url=f"{PART_URL}/ydOS2PdR3a", status_code=429)
        _mock_part(httpx_mock, "ydOS2PdR3a")

        with structlog.testing.capture_logs() as logs:
            parts = await connector.read_spare_parts(asset_id=ASSET_ID)

        assert [p.sku for p in parts] == ["ydOS2PdR3a"]
        (retry,) = [e for e in logs if e["event"] == "http_rate_limit_retry"]
        assert retry["url"] == "/api/v2/parts/<listed part>"
        for value in ("ydOS2PdR3a", BASE):
            assert value not in repr(logs)


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
        updated = await connector.update_work_order("wo1", status=WorkOrderStatus.CLOSED)
        assert updated.id == "wo1"
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        import json

        payload = json.loads(patch_req.content)
        assert payload["status"] == "complete"  # CLOSED → UpKeep "complete"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "upkeep_status", "read_back"),
        [
            (WorkOrderStatus.ASSIGNED, "onHold", WorkOrderStatus.ASSIGNED),
            (WorkOrderStatus.IN_PROGRESS, "inProgress", WorkOrderStatus.IN_PROGRESS),
            # UpKeep has no cancelled state: the order is put on hold and reads back so.
            (WorkOrderStatus.CANCELLED, "onHold", WorkOrderStatus.ASSIGNED),
        ],
    )
    async def test_update_work_order_sends_documented_status(
        self,
        httpx_mock,
        connector: UpKeepConnector,
        status: WorkOrderStatus,
        upkeep_status: str,
        read_back: WorkOrderStatus,
    ) -> None:
        """The PATCH body's ``status`` accepts only open, onHold, inProgress and complete."""
        await _connect(httpx_mock, connector)
        result = {"id": "wo1", "title": "Fix pump", "status": upkeep_status, "assetId": "a1"}
        httpx_mock.add_response(
            method="PATCH", url=f"{BASE}/api/v2/work-orders/wo1", json={"result": result}
        )
        httpx_mock.add_response(
            method="GET", url=f"{BASE}/api/v2/work-orders/wo1", json={"result": result}
        )
        updated = await connector.update_work_order("wo1", status=status)
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        import json

        assert json.loads(patch_req.content) == {"status": upkeep_status}
        assert updated.status == read_back


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
# Caller-supplied IDs stay one URL path segment
# ---------------------------------------------------------------------------


class TestIdsStayOnePathSegment:
    """IDs reach these paths from LLM and MCP tool input.

    Each response is mocked only at the encoded URL, so an ID that leaves its
    segment finds no match. An ID with a ``.`` or ``..`` part is refused
    before any request: a proxy that decodes ``%2F`` before normalizing the
    path would resolve it into another resource.
    """

    @pytest.mark.asyncio
    async def test_get_asset(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET", url=f"{BASE}/api/v2/assets/a1%2Fwork-orders", status_code=404
        )
        assert await connector.get_asset("a1/work-orders") is None

    @pytest.mark.asyncio
    async def test_get_work_order(self, httpx_mock, connector: UpKeepConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET", url=f"{BASE}/api/v2/work-orders/wo1%3Fstatus%3Dopen%23x", status_code=404
        )
        assert await connector.get_work_order("wo1?status=open#x") is None

    @pytest.mark.asyncio
    async def test_update_work_order(self, httpx_mock, connector: UpKeepConnector) -> None:
        """Interpolated as-is, this ID would PATCH ``/api/v2/work-orders/wo1/assets``."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="PATCH", url=f"{BASE}/api/v2/work-orders/wo1%2Fassets", status_code=404
        )
        with pytest.raises(ConnectorError, match="HTTP 404"):
            await connector.update_work_order("wo1/assets", description="x")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("record_id", ["..", "../assets/a1", "wo1/../../assets/a1"])
    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(lambda c, v: c.get_asset(v), id="get_asset"),
            pytest.param(lambda c, v: c.get_work_order(v), id="get_work_order"),
            pytest.param(
                lambda c, v: c.update_work_order(v, description="x"), id="update_work_order"
            ),
        ],
    )
    async def test_dot_segment_id_is_refused_before_any_request(
        self, httpx_mock, connector: UpKeepConnector, call, record_id: str
    ) -> None:
        await _connect(httpx_mock, connector)
        sent = len(httpx_mock.get_requests())
        with pytest.raises(ConnectorError, match="Invalid record ID"):
            await call(connector, record_id)
        assert len(httpx_mock.get_requests()) == sent


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
    @pytest.mark.parametrize("asset_id", ["", "  ", None], ids=["empty", "blank", "unresolved"])
    async def test_read_maintenance_history_refuses_a_missing_asset_id(
        self, httpx_mock, connector: UpKeepConnector, asset_id: str | None
    ) -> None:
        """No asset ID is an error, neither the whole account nor "no history".

        ``read_work_orders`` treats an empty ``asset_id`` as "no filter", so
        passing one through returned every completed work order as one asset's
        history; returning ``[]`` instead would read as "never repaired". MCP
        can send ``""``; a workflow trigger without ``asset_id`` resolves to
        ``None``.
        """
        await _connect(httpx_mock, connector)
        with pytest.raises(
            ConnectorError, match=r"^read_maintenance_history requires an asset_id$"
        ):
            await connector.read_maintenance_history(asset_id)  # type: ignore[arg-type]
        assert len(httpx_mock.get_requests()) == 1  # the connect check only

    @pytest.mark.asyncio
    async def test_numeric_asset_id_keeps_the_asset_filter(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """``0`` is an ID, not "no asset": ``read_work_orders`` drops a falsy filter."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders?limit=100&offset=0&asset=0&status=complete",
            json={"results": [{"id": "wo-h0", "status": "complete", "assetId": "0"}]},
        )
        history = await connector.read_maintenance_history(0)  # type: ignore[arg-type]
        assert [wo.id for wo in history] == ["wo-h0"]

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
        """A failed read raises; it is not reported as an empty history."""
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

    @pytest.mark.asyncio
    async def test_alarm_without_asset_fails_the_history_step(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """An alarm with no ``asset_id`` skips the history step with its error.

        The step's SKIP policy keeps the workflow going, but the step records
        why it has no output instead of passing on an empty history.
        """
        await _connect(httpx_mock, connector)
        registry = ConnectorRegistry()
        registry.register("upkeep", connector)
        history_step = next(s for s in alarm_to_workorder.steps if s.name == "check_history")

        result = await WorkflowEngine(registry=registry).execute(
            Workflow(name="history only", steps=[history_step]),
            {"alarm_id": "AL-1"},
        )

        (step_result,) = result.step_results
        assert step_result.skipped
        assert step_result.output is None
        assert step_result.error is not None
        assert "read_maintenance_history requires an asset_id" in step_result.error
        assert len(httpx_mock.get_requests()) == 1  # the connect check only

    @pytest.mark.asyncio
    async def test_alarm_workflow_history_step_reaches_upkeep(
        self, httpx_mock, connector: UpKeepConnector
    ) -> None:
        """The built-in workflow's history step must run against UpKeep.

        The engine finds the step's connector by capability. With
        ``read_maintenance_history`` undeclared it found none, and the step's
        SKIP policy hid the miss.
        """
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/v2/work-orders?limit=100&offset=0&asset=a1&status=complete",
            json={
                "results": [
                    {"id": "wo-h1", "title": "Past fix", "status": "complete", "asset": "a1"},
                ],
            },
        )
        registry = ConnectorRegistry()
        registry.register("upkeep", connector)
        history_step = next(s for s in alarm_to_workorder.steps if s.name == "check_history")

        result = await WorkflowEngine(registry=registry).execute(
            Workflow(name="history only", steps=[history_step]),
            {"asset_id": "a1"},
        )

        (step_result,) = result.step_results
        assert not step_result.skipped
        assert [wo.id for wo in step_result.output] == ["wo-h1"]


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
