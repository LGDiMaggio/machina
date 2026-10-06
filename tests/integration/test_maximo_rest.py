"""Integration tests for MaximoConnector REST operations.

All HTTP traffic is intercepted by pytest-httpx — no real Maximo API calls.
"""

from __future__ import annotations

import json

import httpx
import pytest

from machina.connectors.cmms.auth import ApiKeyHeaderAuth, BasicAuth
from machina.connectors.cmms.maximo import MaximoConnector
from machina.domain.asset import Asset, AssetType
from machina.domain.maintenance_plan import MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderStatus, WorkOrderType
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
        """History query must combine assetnum with completed/closed status.

        oslc.where has no ``or`` and no parentheses — ``and`` is its only
        boolean operator — so the status alternatives go through ``in``.
        """
        await _connect(httpx_mock, connector)
        expected_where = 'assetnum="PUMP-201" and status in ["COMP","CLOSE"]'
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

    @pytest.mark.asyncio
    async def test_read_maintenance_history_follows_next_page(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        next_page = f"{OSLC}/os/mxwo?pageno=2&oslc.pageSize=100"
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(
                "mxwo", **{"oslc.where": 'assetnum="PUMP-201" and status in ["COMP","CLOSE"]'}
            ),
            json={
                "member": [{"wonum": "WO-H1", "status": "COMP"}],
                "responseInfo": {"nextPage": next_page},
            },
        )
        httpx_mock.add_response(
            method="GET",
            url=next_page,
            json={"member": [{"wonum": "WO-H2", "status": "CLOSE"}], "responseInfo": {}},
        )
        history = await connector.read_maintenance_history("PUMP-201")
        assert [wo.id for wo in history] == ["WO-H1", "WO-H2"]

    @pytest.mark.asyncio
    async def test_read_maintenance_history_follows_a_next_page_link_object(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """Maximo's JSON API can give ``nextPage`` as ``{"href": ...}``."""
        await _connect(httpx_mock, connector)
        next_page = f"{OSLC}/os/mxwo?pageno=2&oslc.pageSize=100"
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(
                "mxwo", **{"oslc.where": 'assetnum="PUMP-201" and status in ["COMP","CLOSE"]'}
            ),
            json={
                "member": [{"wonum": "WO-H1", "status": "COMP"}],
                "responseInfo": {"nextPage": {"href": next_page}},
            },
        )
        httpx_mock.add_response(
            method="GET",
            url=next_page,
            json={"member": [{"wonum": "WO-H2", "status": "CLOSE"}], "responseInfo": {}},
        )
        history = await connector.read_maintenance_history("PUMP-201")
        assert [wo.id for wo in history] == ["WO-H1", "WO-H2"]

    @pytest.mark.parametrize("asset_id", ["", None])
    @pytest.mark.asyncio
    async def test_read_maintenance_history_refuses_a_missing_asset_id(
        self, httpx_mock, connector: MaximoConnector, asset_id
    ) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="requires an asset_id"):
            await connector.read_maintenance_history(asset_id)
        assert len(httpx_mock.get_requests()) == 1  # only the connect handshake

    @pytest.mark.asyncio
    async def test_read_maintenance_history_auth_failure(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", status_code=401)
        with pytest.raises(ConnectorAuthError, match="authentication failed"):
            await connector.read_maintenance_history("PUMP-201")

    @pytest.mark.asyncio
    async def test_read_maintenance_history_server_error(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", status_code=500)
        with pytest.raises(ConnectorError, match="HTTP 500"):
            await connector.read_maintenance_history("PUMP-201")

    @pytest.mark.asyncio
    async def test_read_maintenance_history_requires_connect(
        self, connector: MaximoConnector
    ) -> None:
        with pytest.raises(ConnectorError, match="Not connected"):
            await connector.read_maintenance_history("PUMP-201")


# ---------------------------------------------------------------------------
# oslc.where values built from caller-supplied IDs
# ---------------------------------------------------------------------------


class TestOslcWhereValues:
    """IDs and codes reach the connector from LLM / MCP-client input. Inside a
    quoted oslc.where value, ``"`` and ``\\`` are string syntax, ``%`` makes the
    match a LIKE, ``*`` means "any non-null value", and Maximo's QBE framework
    reads ``,`` as OR and ``= ! < > ~`` as operators — so a value carrying one is
    refused before any request."""

    @pytest.mark.parametrize(
        "read",
        [
            pytest.param(lambda c, v: c.get_asset(v), id="get_asset"),
            pytest.param(lambda c, v: c.get_work_order(v), id="get_work_order"),
            pytest.param(lambda c, v: c.read_work_orders(asset_id=v), id="read_work_orders-asset"),
            pytest.param(lambda c, v: c.read_work_orders(status=v), id="read_work_orders-status"),
            pytest.param(lambda c, v: c.read_spare_parts(sku=v), id="read_spare_parts"),
            pytest.param(
                lambda c, v: c.read_maintenance_history(v), id="read_maintenance_history"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_every_where_value_is_checked(
        self, httpx_mock, connector: MaximoConnector, read
    ) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match=r"oslc\.where"):
            await read(connector, 'PUMP-201" and assetnum!="*')
        assert len(httpx_mock.get_requests()) == 1  # only the connect handshake

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param('PUMP-201"', id="double-quote"),
            pytest.param("PUMP-201\\", id="backslash"),
            pytest.param("PUMP%", id="like-wildcard"),
            pytest.param("*", id="not-null-star"),
            pytest.param("PUMP-201,PUMP-202", id="qbe-comma"),
            pytest.param("!=PUMP-201", id="qbe-not-equal"),
            pytest.param("=PUMP-201", id="qbe-equal"),
            pytest.param(">0", id="qbe-greater"),
            pytest.param("<9", id="qbe-less"),
            pytest.param("~NULL~", id="qbe-null-token"),
        ],
    )
    @pytest.mark.asyncio
    async def test_oslc_significant_character_is_refused(
        self, httpx_mock, connector: MaximoConnector, value: str
    ) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match=r"oslc\.where"):
            await connector.read_maintenance_history(value)
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_ordinary_id_punctuation_passes_through(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", json={"member": [], "responseInfo": {}})
        await connector.get_asset("O'NEIL-1/A.2_B #3")
        sent = httpx_mock.get_requests()[-1].url.params["oslc.where"]
        assert sent == 'assetnum="O\'NEIL-1/A.2_B #3"'

    @pytest.mark.asyncio
    async def test_empty_value_is_refused(self, httpx_mock, connector: MaximoConnector) -> None:
        """An empty quoted value would put no restriction on the attribute."""
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="empty"):
            await connector.get_asset("")
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_numeric_id_is_formatted_as_before(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """A workflow event can carry an ID as a number; it is sent as its digits."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(method="GET", json={"member": [], "responseInfo": {}})
        await connector.get_asset(201)
        assert httpx_mock.get_requests()[-1].url.params["oslc.where"] == 'assetnum="201"'


# ---------------------------------------------------------------------------
# Updates: look the order up by wonum, POST to its URI
# ---------------------------------------------------------------------------

# Maximo addresses a record by a rest id derived from its primary key: here "_"
# plus the base64 of "WO-001/BEDFORD" (wonum/siteid), with "=" written as "-".
_REST_ID = "_V08tMDAxL0JFREZPUkQ-"
# Maximo reports a URI on the host it sees itself on, often an internal name.
_INTERNAL_URI = f"http://maximo-internal:9080/maximo/oslc/os/mxwo/{_REST_ID}"


def _add_lookup(httpx_mock, wonum: str, *members: dict[str, str]) -> None:
    """Register the answer to the URI lookup an update sends before writing."""
    httpx_mock.add_response(
        method="GET",
        url=_oslc_url(
            "mxwo",
            **{"oslc.pageSize": "2", "oslc.select": "siteid", "oslc.where": f'wonum="{wonum}"'},
        ),
        json={"member": list(members), "responseInfo": {}},
    )


def _add_reread(httpx_mock, status: str) -> None:
    """Register the read-back of WO-001 that follows a successful update."""
    httpx_mock.add_response(
        method="GET",
        url=_oslc_url("mxwo", **{"oslc.pageSize": "1", "oslc.where": 'wonum="WO-001"'}),
        json={
            "member": [
                {
                    "wonum": "WO-001",
                    "description": "Fix leak",
                    "wopriority": 2,
                    "status": status,
                    "worktype": "CM",
                    "assetnum": "P1",
                    "reportdate": "2025-06-01T10:00:00Z",
                    "changedate": "2025-07-01T10:00:00Z",
                },
            ],
            "responseInfo": {},
        },
    )


class TestUpdatePathId:
    """An update never puts the ``wonum`` in a URL path: it is a quoted
    oslc.where value in the lookup, and the POST URL takes the rest id from the
    URI Maximo returns, which must stay one path segment."""

    @pytest.mark.asyncio
    async def test_wonum_reaches_only_the_where_clause(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        hostile = "WO-1/../../mxasset/X"
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, hostile, {"href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", status_code=204)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.pageSize": "1", "oslc.where": f'wonum="{hostile}"'}),
            json={"member": [{"wonum": hostile}], "responseInfo": {}},
        )
        await connector.update_work_order(hostile, description="x")
        post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        assert post.url.raw_path == f"/maximo/oslc/os/mxwo/{_REST_ID}?lean=1".encode()

    @pytest.mark.asyncio
    async def test_rest_id_stays_inside_its_path_segment(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """An encoded ``/`` in the rest id is sent encoded, not as a path separator."""
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"href": f"{OSLC}/os/mxwo/_A%2F..%2Fmxasset%2FX"})
        httpx_mock.add_response(method="POST", status_code=204)
        _add_reread(httpx_mock, "WAPPR")
        await connector.update_work_order("WO-001", description="x")
        post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        assert post.url.raw_path == b"/maximo/oslc/os/mxwo/_A%2F..%2Fmxasset%2FX?lean=1"

    @pytest.mark.parametrize(
        ("member", "reason"),
        [
            pytest.param({"siteid": "BEDFORD"}, "no work-order URI", id="no-href"),
            pytest.param(
                {"href": f"{OSLC}/os/mxasset/{_REST_ID}"}, "no work-order URI", id="other-object"
            ),
            pytest.param({"href": f"{OSLC}/os/mxwo/"}, "Invalid record ID", id="empty-rest-id"),
            pytest.param({"href": f"{OSLC}/os/mxwo/.."}, "Invalid record ID", id="dot-segment"),
            pytest.param(
                {"href": f"{OSLC}/os/mxwo/%2E%2E"}, "Invalid record ID", id="encoded-dot-segment"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_unexpected_uri_is_refused_before_writing(
        self, httpx_mock, connector: MaximoConnector, member: dict[str, str], reason: str
    ) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", member)
        with pytest.raises(ConnectorError, match=reason):
            await connector.update_work_order("WO-001", description="x")
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET"]  # no POST

    @pytest.mark.parametrize(
        ("work_order_id", "reason"),
        [
            pytest.param("WO-1,WO-2", r"oslc\.where", id="qbe-comma"),
            pytest.param("", "empty", id="empty"),
        ],
    )
    @pytest.mark.asyncio
    async def test_unsafe_wonum_is_refused_before_any_request(
        self, httpx_mock, connector: MaximoConnector, work_order_id: str, reason: str
    ) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match=reason):
            await connector.update_work_order(work_order_id, description="x")
        assert [r.method for r in httpx_mock.get_requests()] == ["GET"]  # connect only


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
    """IBM's REST API guide: an update is a POST to the URI Maximo returned for
    the record, with ``x-method-override: PATCH``; ``patchtype: MERGE`` keeps the
    child objects the payload does not list."""

    @pytest.mark.parametrize("post_status", [204, 200])
    @pytest.mark.asyncio
    async def test_update_posts_to_the_work_order_uri(
        self, httpx_mock, connector: MaximoConnector, post_status: int
    ) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"siteid": "BEDFORD", "href": _INTERNAL_URI})
        httpx_mock.add_response(
            method="POST",
            url=httpx.URL(f"{OSLC}/os/mxwo/{_REST_ID}", params={"lean": "1"}),
            status_code=post_status,
        )
        _add_reread(httpx_mock, "CLOSE")
        updated = await connector.update_work_order("WO-001", status=WorkOrderStatus.CLOSED)
        assert updated.id == "WO-001"
        assert updated.status == WorkOrderStatus.CLOSED
        requests = httpx_mock.get_requests()
        # connect, URI lookup, update, read-back — never the bare PATCH verb
        assert [r.method for r in requests] == ["GET", "GET", "POST", "GET"]
        post = requests[2]
        # Sent to the configured URL, not to the internal host in Maximo's URI.
        assert post.url.host == "maximo.example.com"
        assert post.headers["x-method-override"] == "PATCH"
        assert post.headers["patchtype"] == "MERGE"
        assert post.headers["content-type"] == "application/json"
        assert post.headers["apikey"] == "test-key"
        assert json.loads(post.content) == {"status": "CLOSE"}

    @pytest.mark.parametrize(
        ("transition", "maximo_status"),
        [
            pytest.param(lambda c: c.close_work_order("WO-001"), "CLOSE", id="close"),
            pytest.param(lambda c: c.cancel_work_order("WO-001"), "CAN", id="cancel"),
        ],
    )
    @pytest.mark.asyncio
    async def test_close_and_cancel_send_the_same_update(
        self, httpx_mock, connector: MaximoConnector, transition, maximo_status: str
    ) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", status_code=204)
        _add_reread(httpx_mock, maximo_status)
        await transition(connector)
        post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        assert post.url.path == f"/maximo/oslc/os/mxwo/{_REST_ID}"
        assert post.headers["x-method-override"] == "PATCH"
        assert json.loads(post.content) == {"status": maximo_status}

    @pytest.mark.asyncio
    async def test_update_of_an_unknown_wonum_writes_nothing(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "MISS")
        with pytest.raises(ConnectorError, match="Work order MISS not found"):
            await connector.update_work_order("MISS", description="x")
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET"]  # no POST

    @pytest.mark.asyncio
    async def test_update_of_a_wonum_in_several_sites_writes_nothing(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """A wonum is unique only within a site; picking one could update the wrong order."""
        await _connect(httpx_mock, connector)
        _add_lookup(
            httpx_mock,
            "1001",
            {"siteid": "TEXAS", "href": f"{OSLC}/os/mxwo/_MTAwMS9URVhBUw--"},
            {"siteid": "BEDFORD", "href": f"{OSLC}/os/mxwo/_MTAwMS9CRURGT1JE"},
        )
        with pytest.raises(ConnectorError, match=r"more than one site \(BEDFORD, TEXAS\)"):
            await connector.update_work_order("1001", description="x")
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET"]  # no POST

    @pytest.mark.parametrize(
        ("post_status", "error", "match"),
        [
            pytest.param(400, ConnectorError, "update work order failed: HTTP 400", id="rejected"),
            pytest.param(401, ConnectorAuthError, "authentication failed", id="auth"),
        ],
    )
    @pytest.mark.asyncio
    async def test_refused_update_raises_without_a_read_back(
        self,
        httpx_mock,
        connector: MaximoConnector,
        post_status: int,
        error: type[Exception],
        match: str,
    ) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", status_code=post_status)
        with pytest.raises(error, match=match):
            await connector.update_work_order("WO-001", description="x")
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET", "POST"]


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
