"""Integration tests for MaximoConnector REST operations.

All HTTP traffic is intercepted by pytest-httpx — no real Maximo API calls.
"""

from __future__ import annotations

import json

import httpx
import pytest
import structlog.testing

from machina.connectors.base import ConnectorRegistry
from machina.connectors.cmms.auth import ApiKeyHeaderAuth, BasicAuth
from machina.connectors.cmms.maximo import MaximoConnector
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.maintenance_plan import MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderStatus, WorkOrderType
from machina.exceptions import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorSchemaError,
    ConnectorUnsupportedFilterError,
)
from machina.workflows.builtins.alarm_to_workorder import alarm_to_workorder
from machina.workflows.engine import WorkflowEngine
from machina.workflows.models import Workflow

BASE = "https://maximo.example.com"
OSLC = f"{BASE}/maximo/oslc"

# Default query params that _oslc_get appends to every first request.
_OSLC_PARAMS = {"lean": "1", "oslc.pageSize": "100"}

# The attributes each read selects. Without oslc.select, Maximo answers a
# collection query with each member's href only (IBM REST API guide,
# "Selecting"). Written out here, not imported, so a change to a selection
# fails these tests.
_SELECT = {
    "mxasset": (
        "assetnum,siteid,description,location,manufacturer,serialnum,priority,parent,"
        "classstructureid,assettype"
    ),
    "mxwo": (
        "wonum,siteid,description,status,wopriority,worktype,assetnum,lead,"
        "assignedownergroup,failurecode,problemcode,reportdate,changedate"
    ),
    "mxinventory": (
        "itemnum,siteid,location,minlevel,item.description,invbalances{curbal},"
        "invcost{avgcost,lastcost}"
    ),
    "mxpm": "pmnum,siteid,assetnum,description,frequency,frequnit,status",
}


def _oslc_url(object_structure: str, **extra: str) -> httpx.URL:
    """Build a URL with the default OSLC query params and the read's oslc.select."""
    params = {**_OSLC_PARAMS, "oslc.select": _SELECT[object_structure], **extra}
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
        """OSLC pagination via ``responseInfo.nextPage.href``.

        The shape IBM's REST clients read (``ResourceSet.nextPage()`` in IBM's
        ``maximorestclient``). This fixture used to give ``nextPage`` as a bare
        URL, the shape the connector expected, so it passed while every real
        multi-page read failed.
        """
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxasset"),
            json={
                "member": [{"assetnum": "A1", "description": "Asset 1"}],
                "responseInfo": {
                    "nextPage": {"href": f"{OSLC}/os/mxasset?pageno=2&oslc.pageSize=100"},
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
    async def test_read_assets_follows_a_bare_next_page_url(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """A ``nextPage`` given as the URL itself is followed too."""
        await _connect(httpx_mock, connector)
        next_url = f"{OSLC}/os/mxasset?pageno=2&oslc.pageSize=100"
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxasset"),
            json={"member": [{"assetnum": "A1"}], "responseInfo": {"nextPage": next_url}},
        )
        httpx_mock.add_response(
            method="GET",
            url=next_url,
            json={"member": [{"assetnum": "A2"}], "responseInfo": {}},
        )
        assets = await connector.read_assets()
        assert [a.id for a in assets] == ["A1", "A2"]

    @pytest.mark.parametrize(
        ("lookup", "object_structure", "where", "record"),
        [
            pytest.param(
                lambda c: c.get_asset("PUMP-201"),
                "mxasset",
                'assetnum="PUMP-201"',
                {"assetnum": "PUMP-201", "siteid": "S1"},
                id="get_asset",
            ),
            pytest.param(
                lambda c: c.get_work_order("WO-1"),
                "mxwo",
                'wonum="WO-1"',
                {"wonum": "WO-1", "siteid": "S1"},
                id="get_work_order",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_lookups_read_only_the_first_page(
        self, httpx_mock, connector: MaximoConnector, lookup, object_structure, where, record
    ) -> None:
        """A key that exists on several sites pages one match at a time; a lookup
        uses the first, so it does not follow the link."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(object_structure, **{"oslc.pageSize": "1", "oslc.where": where}),
            json={
                "member": [record],
                "responseInfo": {
                    "nextPage": {"href": f"{OSLC}/os/{object_structure}?pageno=2"},
                },
            },
        )
        assert await lookup(connector) is not None
        assert len(httpx_mock.get_requests()) == 2  # connect + the first page

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
                        "frequnit": "DAYS",
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

        oslc.where has no ``or`` and no grouping parentheses, so the two
        statuses are listed through ``in``.
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
    async def test_alarm_workflow_history_step_reaches_maximo(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """The built-in workflow's history step must run against Maximo.

        The engine finds the step's connector by capability. With
        ``read_maintenance_history`` undeclared it found none, and the step's
        SKIP policy hid the miss.
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
                        "status": "COMP",
                        "assetnum": "PUMP-201",
                    },
                ],
                "responseInfo": {},
            },
        )
        registry = ConnectorRegistry()
        registry.register("maximo", connector)
        history_step = next(s for s in alarm_to_workorder.steps if s.name == "check_history")

        result = await WorkflowEngine(registry=registry).execute(
            Workflow(name="history only", steps=[history_step]),
            {"asset_id": "PUMP-201"},
        )

        (step_result,) = result.step_results
        assert not step_result.skipped
        assert [wo.id for wo in step_result.output] == ["WO-H1"]

    @pytest.mark.asyncio
    async def test_read_maintenance_history_follows_the_next_page_href(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """Regression: a second history page was never read.

        Maximo gives the link as ``responseInfo.nextPage.href`` (IBM's own REST
        clients read that field). The connector used ``nextPage`` itself as the
        URL, so the object reached httpx and the read failed on its second
        request. The link already carries the query, so nothing is appended.
        """
        await _connect(httpx_mock, connector)
        next_href = str(_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE, "pageno": "2"}))
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE}),
            json={
                "member": [{"wonum": "WO-H1", "status": "COMP"}],
                "responseInfo": {
                    "href": str(_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE})),
                    "nextPage": {"href": next_href},
                    "pagenum": 1,
                },
            },
        )
        httpx_mock.add_response(
            method="GET",
            url=next_href,
            json={
                "member": [{"wonum": "WO-H2", "status": "CLOSE"}],
                "responseInfo": {"pagenum": 2},
            },
        )
        history = await connector.read_maintenance_history("PUMP-201")
        assert [wo.id for wo in history] == ["WO-H1", "WO-H2"]
        assert httpx_mock.get_requests()[-1].url == httpx.URL(next_href)

    @pytest.mark.parametrize(
        "next_page",
        [
            {},
            {"href": ""},
            {"href": "  "},
            {"href": None},
            {"href": 7},
            "",
            7,
            [],
            {"href": f"{OSLC}/os/mxwo?pageno=2\x00"},
            {"href": f"{OSLC}/os/mxwo?pageno=2&pad=" + "a" * 65_536},
            # 65,514 characters: under httpx's limit as given, over it once resolved.
            {"href": "?pageno=2&pad=" + "a" * 65_500},
        ],
        ids=[
            "no-href",
            "empty-href",
            "blank-href",
            "null-href",
            "number-href",
            "empty",
            "number",
            "list",
            "control-character",
            "too-long",
            "too-long-once-resolved",
        ],
    )
    @pytest.mark.asyncio
    async def test_read_maintenance_history_refuses_a_next_page_without_a_url(
        self, httpx_mock, connector: MaximoConnector, next_page: object
    ) -> None:
        """A next page that cannot be followed is an error, not the last page.

        Stopping there would return the first page as the asset's whole history.
        """
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE}),
            json={
                "member": [{"wonum": "WO-H1", "status": "COMP"}],
                "responseInfo": {"nextPage": next_page},
            },
        )
        with pytest.raises(ConnectorError, match=r"^Maximo returned a nextPage link without"):
            await connector.read_maintenance_history("PUMP-201")
        assert len(httpx_mock.get_requests()) == 2  # connect + the first page

    @pytest.mark.parametrize(
        "href",
        [
            "https://attacker.example.net/maximo/oslc/os/mxwo?pageno=2",
            "http://maximo.example.com/maximo/oslc/os/mxwo?pageno=2",
            "https://maximo.example.com:9443/maximo/oslc/os/mxwo?pageno=2",
            "//attacker.example.net/maximo/oslc/os/mxwo?pageno=2",
            "https://maximo.example.com:99999/maximo/oslc/os/mxwo?pageno=2",
            "http://maximo.example.com:443/maximo/oslc/os/mxwo?pageno=2",
            "https://svc:secret@maximo.example.com/maximo/oslc/os/mxwo?pageno=2",
            "https://maximo.example.com:0/maximo/oslc/os/mxwo?pageno=2",
        ],
        ids=[
            "other-host",
            "plain-http",
            "other-port",
            "scheme-relative",
            "invalid-port",
            "scheme-only",
            "user-info",
            "port-zero",
        ],
    )
    @pytest.mark.asyncio
    async def test_next_page_on_another_origin_is_refused(
        self, httpx_mock, connector: MaximoConnector, href: str
    ) -> None:
        """Every page carries the API key, so the link must stay on the connector's
        scheme, host and port. Maximo builds links from its own configured
        address, which behind a proxy can be an internal host or plain HTTP."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE}),
            json={
                "member": [{"wonum": "WO-H1", "status": "COMP"}],
                "responseInfo": {"nextPage": {"href": href}},
            },
        )
        with pytest.raises(ConnectorError, match="another scheme, host or port") as exc_info:
            await connector.read_maintenance_history("PUMP-201")
        for part in ("attacker", "9443", "secret", "pageno"):
            assert part not in str(exc_info.value)
        assert len(httpx_mock.get_requests()) == 2  # connect + the first page

    @pytest.mark.parametrize(
        "href",
        [
            "/maximo/oslc/os/mxwo?pageno=2&oslc.pageSize=100",
            "?pageno=2&oslc.pageSize=100",
            "mxwo?pageno=2&oslc.pageSize=100",
            "HTTPS://MAXIMO.example.com:443/maximo/oslc/os/mxwo?pageno=2&oslc.pageSize=100",
        ],
        ids=["root-relative", "query-only", "path-relative", "explicit-default-port"],
    )
    @pytest.mark.asyncio
    async def test_a_same_origin_next_page_link_is_followed(
        self, httpx_mock, connector: MaximoConnector, href: str
    ) -> None:
        """Relative links resolve against the current page; scheme and host case
        and an explicit default port do not change the origin."""
        await _connect(httpx_mock, connector)
        next_url = f"{OSLC}/os/mxwo?pageno=2&oslc.pageSize=100"
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE}),
            json={
                "member": [{"wonum": "WO-H1", "status": "COMP"}],
                "responseInfo": {"nextPage": {"href": href}},
            },
        )
        httpx_mock.add_response(
            method="GET",
            url=next_url,
            json={"member": [{"wonum": "WO-H2", "status": "CLOSE"}]},
        )
        history = await connector.read_maintenance_history("PUMP-201")
        assert [wo.id for wo in history] == ["WO-H1", "WO-H2"]
        assert httpx_mock.get_requests()[-1].url == httpx.URL(next_url)

    @pytest.mark.asyncio
    async def test_a_repeated_next_page_link_is_refused(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """A server that links a page to itself would otherwise be read forever."""
        await _connect(httpx_mock, connector)
        page2 = f"{OSLC}/os/mxwo?pageno=2&oslc.pageSize=100"
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE}),
            json={
                "member": [{"wonum": "WO-H1", "status": "COMP"}],
                "responseInfo": {"nextPage": {"href": page2}},
            },
        )
        httpx_mock.add_response(
            method="GET",
            url=page2,
            json={
                "member": [{"wonum": "WO-H2", "status": "CLOSE"}],
                "responseInfo": {"nextPage": {"href": page2}},
            },
        )
        with pytest.raises(ConnectorError, match="already returned"):
            await connector.read_maintenance_history("PUMP-201")
        assert len(httpx_mock.get_requests()) == 3  # connect + page 1 + page 2 once

    @pytest.mark.asyncio
    async def test_rows_past_the_safety_cap_are_refused(
        self, httpx_mock, connector: MaximoConnector, monkeypatch
    ) -> None:
        """Paging bounds each request, not the read: past the cap it raises."""
        monkeypatch.setattr(MaximoConnector, "_MAX_OSLC_ROWS", 150)  # also allows 2 pages
        await _connect(httpx_mock, connector)
        page2 = f"{OSLC}/os/mxwo?pageno=2&oslc.pageSize=100"
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE}),
            json={
                "member": [{"wonum": f"WO-{i}"} for i in range(100)],
                "responseInfo": {"nextPage": {"href": page2}},
            },
        )
        httpx_mock.add_response(
            method="GET",
            url=page2,
            json={"member": [{"wonum": f"WO-{i}"} for i in range(100, 151)]},
        )
        with pytest.raises(ConnectorError, match="150-row safety cap"):
            await connector.read_maintenance_history("PUMP-201")
        assert len(httpx_mock.get_requests()) == 3  # connect + 2 pages

    @pytest.mark.asyncio
    async def test_pages_past_the_safety_cap_are_refused(
        self, httpx_mock, connector: MaximoConnector, monkeypatch
    ) -> None:
        """Empty pages that keep linking on never reach the row cap."""
        monkeypatch.setattr(MaximoConnector, "_MAX_OSLC_ROWS", 100)  # 100 // 100 + 1 = 2 pages
        await _connect(httpx_mock, connector)
        page2 = f"{OSLC}/os/mxwo?pageno=2&oslc.pageSize=100"
        page3 = f"{OSLC}/os/mxwo?pageno=3&oslc.pageSize=100"
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE}),
            json={"member": [], "responseInfo": {"nextPage": {"href": page2}}},
        )
        httpx_mock.add_response(
            method="GET",
            url=page2,
            json={"member": [], "responseInfo": {"nextPage": {"href": page3}}},
        )
        with pytest.raises(ConnectorError, match="2-page safety cap"):
            await connector.read_maintenance_history("PUMP-201")
        assert len(httpx_mock.get_requests()) == 3  # connect + 2 pages; page 3 never sent

    @pytest.mark.asyncio
    async def test_a_null_response_info_ends_the_read(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE}),
            json={"member": [{"wonum": "WO-H1"}], "responseInfo": None},
        )
        history = await connector.read_maintenance_history("PUMP-201")
        assert [wo.id for wo in history] == ["WO-H1"]

    @pytest.mark.asyncio
    async def test_a_refused_link_is_logged_by_scheme_and_host_only(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """The operator learns which address Maximo advertised; not its path or query."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE}),
            json={
                "member": [{"wonum": "WO-H1"}],
                "responseInfo": {
                    "nextPage": {"href": "http://maximo-app01:9080/maximo/oslc/os/mxwo?pageno=2"}
                },
            },
        )
        with structlog.testing.capture_logs() as logs, pytest.raises(ConnectorError):
            await connector.read_maintenance_history("PUMP-201")
        (refused,) = [e for e in logs if e["event"] == "maximo_next_page_refused"]
        assert refused["link_scheme"] == "http"
        assert refused["link_host"] == "maximo-app01"
        assert refused["operation"] == "oslc_get"
        assert "pageno" not in repr(refused)

    @pytest.mark.asyncio
    async def test_a_null_next_page_ends_the_read(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.where": _HISTORY_WHERE}),
            json={"member": [{"wonum": "WO-H1"}], "responseInfo": {"nextPage": None}},
        )
        history = await connector.read_maintenance_history("PUMP-201")
        assert [wo.id for wo in history] == ["WO-H1"]

    @pytest.mark.parametrize("asset_id", ["", "  ", None], ids=["empty", "blank", "unresolved"])
    @pytest.mark.asyncio
    async def test_read_maintenance_history_refuses_a_missing_asset_id(
        self, httpx_mock, connector: MaximoConnector, asset_id: str | None
    ) -> None:
        """A missing ID is an error: ``None`` would be matched as ``"None"``."""
        await _connect(httpx_mock, connector)
        with pytest.raises(
            ConnectorError, match=r"^read_maintenance_history requires an asset_id$"
        ):
            await connector.read_maintenance_history(asset_id)  # type: ignore[arg-type]
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
        """A failed read raises; it is not reported as an empty history."""
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


_HISTORY_WHERE = 'assetnum="PUMP-201" and status in ["COMP","CLOSE"]'


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


# ---------------------------------------------------------------------------
# Updates: look the order up by wonum, POST to its URI, read it back from there
# ---------------------------------------------------------------------------

# Maximo addresses a record by a rest id derived from its primary key: here "_"
# plus the base64 of "WO-001/BEDFORD" (wonum/siteid), with "=" written as "-".
_REST_ID = "_V08tMDAxL0JFREZPUkQ-"
# Maximo reports a URI on the host it sees itself on, often an internal name.
_INTERNAL_URI = f"http://maximo-internal:9080/maximo/oslc/os/mxwo/{_REST_ID}"
_UPDATE_URL = httpx.URL(f"{OSLC}/os/mxwo/{_REST_ID}", params={"lean": "1"})
_APPLIED_NOT_READ_BACK = (
    "Maximo applied the update to work order WO-001, but reading it back failed — "
    "do not repeat the update; read the work order to check it"
)


def _lookup_url(wonum: str) -> httpx.URL:
    """The URI lookup an update sends before writing."""
    return _oslc_url(
        "mxwo",
        **{"oslc.pageSize": "2", "oslc.select": "wonum,siteid", "oslc.where": f'wonum="{wonum}"'},
    )


def _add_lookup(
    httpx_mock, wonum: str, *members: dict[str, object], next_page: str | None = None
) -> None:
    """Register the answer to the URI lookup an update sends before writing.

    Each member names ``wonum``, as Maximo's answer to ``oslc.select=wonum,siteid``
    does, unless the test gives it another value.
    """
    httpx_mock.add_response(
        method="GET",
        url=_lookup_url(wonum),
        json={
            "member": [{"wonum": wonum, **member} for member in members],
            "responseInfo": {"nextPage": {"href": next_page}} if next_page else {},
        },
    )


def _add_reread(httpx_mock, status: str, wonum: str = "WO-001", rest_id: str = _REST_ID) -> None:
    """Register the read-back of the updated order from its URI, as Maximo answers it."""
    httpx_mock.add_response(
        method="GET",
        url=httpx.URL(f"{OSLC}/os/mxwo/{rest_id}", params={"lean": "1"}),
        json={
            "wonum": wonum,
            "description": "Fix leak",
            "wopriority": 2,
            "status": status,
            "worktype": "CM",
            "assetnum": "P1",
            "reportdate": "2025-06-01T10:00:00Z",
            "changedate": "2025-07-01T10:00:00Z",
            "href": f"http://maximo-internal:9080/maximo/oslc/os/mxwo/{rest_id}",
        },
    )


class TestUpdateWorkOrder:
    """IBM's REST API guide: an update is a POST to the URI Maximo returned for
    the record, with ``x-method-override: PATCH``; ``patchtype: MERGE`` keeps the
    child objects the payload does not list. The URI's last segment is a rest id
    derived from ``wonum`` and ``siteid``, so it is looked up, never built, and
    the order is read back from the same URI."""

    @pytest.mark.parametrize("post_status", [204, 200])
    @pytest.mark.asyncio
    async def test_update_posts_to_the_work_order_uri(
        self, httpx_mock, connector: MaximoConnector, post_status: int
    ) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"siteid": "BEDFORD", "href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=post_status)
        _add_reread(httpx_mock, "CLOSE")
        updated = await connector.update_work_order("WO-001", status=WorkOrderStatus.CLOSED)
        assert updated.id == "WO-001"
        assert updated.status == WorkOrderStatus.CLOSED
        requests = httpx_mock.get_requests()
        # connect, URI lookup, update, read-back: never the bare PATCH verb
        assert [r.method for r in requests] == ["GET", "GET", "POST", "GET"]
        post, reread = requests[2], requests[3]
        # Sent to the configured URL, not to the internal host in Maximo's URI.
        assert post.url.host == "maximo.example.com"
        assert post.headers["x-method-override"] == "PATCH"
        assert post.headers["patchtype"] == "MERGE"
        assert post.headers["content-type"] == "application/json"
        assert post.headers["apikey"] == "test-key"
        assert json.loads(post.content) == {"status": "CLOSE"}
        # The read-back reads the resource just written, not the first match by wonum.
        assert reread.url.host == "maximo.example.com"
        assert reread.url.raw_path == f"/maximo/oslc/os/mxwo/{_REST_ID}?lean=1".encode()

    @pytest.mark.asyncio
    async def test_every_given_field_is_sent(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=204)
        _add_reread(httpx_mock, "INPRG")
        await connector.update_work_order(
            "WO-001",
            status=WorkOrderStatus.IN_PROGRESS,
            assigned_to="john.doe",
            description="Replace seal",
        )
        post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        assert json.loads(post.content) == {
            "status": "INPRG",
            "lead": "john.doe",
            "description": "Replace seal",
        }

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
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=204)
        _add_reread(httpx_mock, maximo_status)
        await transition(connector)
        post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        assert post.url.path == f"/maximo/oslc/os/mxwo/{_REST_ID}"
        assert post.headers["x-method-override"] == "PATCH"
        assert post.headers["patchtype"] == "MERGE"
        assert json.loads(post.content) == {"status": maximo_status}

    @pytest.mark.parametrize(
        "write",
        [
            pytest.param(lambda c: c.update_work_order("WO-001", description="x"), id="update"),
            pytest.param(lambda c: c.close_work_order("WO-001"), id="close"),
            pytest.param(lambda c: c.cancel_work_order("WO-001"), id="cancel"),
        ],
    )
    @pytest.mark.asyncio
    async def test_never_connected_connector_sends_nothing(
        self, httpx_mock, connector: MaximoConnector, write
    ) -> None:
        """The lookup is an authenticated request too: it waits for connect()."""
        with pytest.raises(ConnectorError, match="Not connected"):
            await write(connector)
        assert httpx_mock.get_requests() == []

    @pytest.mark.asyncio
    async def test_no_field_is_refused_before_any_request(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="at least one field"):
            await connector.update_work_order("WO-001")
        assert [r.url.path for r in httpx_mock.get_requests()] == ["/maximo/oslc/whoami"]

    @pytest.mark.asyncio
    async def test_update_of_an_unknown_wonum_writes_nothing(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "MISS")
        with pytest.raises(ConnectorError, match=r"^Work order MISS not found$"):
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
        with pytest.raises(ConnectorError) as exc_info:
            await connector.update_work_order("1001", description="x")
        assert str(exc_info.value) == (
            "Work order 1001 exists in more than one site — refusing to guess which one to update"
        )
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET"]  # no POST

    @pytest.mark.asyncio
    async def test_a_second_site_on_a_later_page_is_found(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """A server that serves one row per page despite oslc.pageSize=2 still
        links to the next page: reading only the first would see one site."""
        await _connect(httpx_mock, connector)
        page_2 = f"{OSLC}/os/mxwo?pageno=2"
        _add_lookup(
            httpx_mock,
            "1001",
            {"siteid": "TEXAS", "href": f"{OSLC}/os/mxwo/_MTAwMS9URVhBUw--"},
            next_page=page_2,
        )
        httpx_mock.add_response(
            method="GET",
            url=page_2,
            json={
                "member": [
                    {
                        "wonum": "1001",
                        "siteid": "BEDFORD",
                        "href": f"{OSLC}/os/mxwo/_MTAwMS9CRURGT1JE",
                    }
                ],
                "responseInfo": {},
            },
        )
        with pytest.raises(ConnectorError, match="more than one site"):
            await connector.update_work_order("1001", description="x")
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET", "GET"]  # no POST

    @pytest.mark.asyncio
    async def test_lookup_stops_reading_at_two_rows(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """Two rows already decide the refusal. A server or proxy that ignores the
        filter would otherwise page through every work order, two at a time."""
        await _connect(httpx_mock, connector)
        _add_lookup(
            httpx_mock,
            "1001",
            {"siteid": "TEXAS", "href": f"{OSLC}/os/mxwo/_A"},
            {"siteid": "BEDFORD", "href": f"{OSLC}/os/mxwo/_B"},
            next_page=f"{OSLC}/os/mxwo?pageno=2",
        )
        with pytest.raises(ConnectorError, match="more than one site"):
            await connector.update_work_order("1001", description="x")
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET"]  # no page 2

    @pytest.mark.asyncio
    async def test_rows_for_other_orders_are_not_reported_as_sites(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """Rows for other wonums mean the filter was not applied, not two sites."""
        await _connect(httpx_mock, connector)
        _add_lookup(
            httpx_mock,
            "1001",
            {"wonum": "1001", "href": f"{OSLC}/os/mxwo/_A"},
            {"wonum": "2002", "href": f"{OSLC}/os/mxwo/_B"},
        )
        with pytest.raises(ConnectorError, match="does not name that work order"):
            await connector.update_work_order("1001", description="x")
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET"]  # no POST

    @pytest.mark.parametrize(
        "returned_wonum",
        [pytest.param("WO-002", id="another-wonum"), pytest.param(None, id="no-wonum")],
    )
    @pytest.mark.asyncio
    async def test_a_lookup_answer_naming_another_order_writes_nothing(
        self, httpx_mock, connector: MaximoConnector, returned_wonum: object
    ) -> None:
        """A server or proxy that dropped the oslc.where filter answers with
        whatever comes first; its URI would address another order."""
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"wonum": returned_wonum, "href": _INTERNAL_URI})
        with pytest.raises(ConnectorError) as exc_info:
            await connector.update_work_order("WO-001", description="x")
        assert str(exc_info.value) == (
            "Maximo's answer to the lookup of work order WO-001 does not name that "
            "work order — refusing to update it"
        )
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET"]  # no POST

    @pytest.mark.parametrize(
        ("wonum", "returned"),
        [
            # A wonum of None is matched as the text "None".
            pytest.param(None, None, id="missing-for-None"),
            pytest.param("True", True, id="boolean-for-True"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_non_text_wonum_is_not_a_match_for_its_text(
        self, httpx_mock, connector: MaximoConnector, wonum: object, returned: object
    ) -> None:
        """A row without a wonum, or with a JSON boolean, names no work order."""
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, str(wonum), {"wonum": returned, "href": _INTERNAL_URI})
        with pytest.raises(ConnectorError, match="does not name that work order"):
            await connector.update_work_order(wonum, description="x")  # type: ignore[arg-type]
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET"]  # no POST

    @pytest.mark.parametrize("returned", ["1001", 1001])
    @pytest.mark.asyncio
    async def test_a_numeric_wonum_matches_its_digits(
        self, httpx_mock, connector: MaximoConnector, returned: object
    ) -> None:
        """A wonum can arrive as a number from model JSON, and Maximo can answer one."""
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "1001", {"wonum": returned, "href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=204)
        _add_reread(httpx_mock, "WAPPR", wonum="1001")
        updated = await connector.update_work_order(1001, description="x")  # type: ignore[arg-type]
        assert updated.id == "1001"

    @pytest.mark.asyncio
    async def test_the_wonum_match_ignores_case(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """wonum is an upper-case field: Maximo answers "WO-001" to "wo-001"."""
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "wo-001", {"wonum": "WO-001", "href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=204)
        _add_reread(httpx_mock, "WAPPR")
        updated = await connector.update_work_order("wo-001", description="x")
        assert updated.id == "WO-001"

    @pytest.mark.parametrize(
        ("wonum", "members", "reason"),
        [
            pytest.param("MISS", [], "not_found", id="not-found"),
            pytest.param(
                "1001",
                [{"href": f"{OSLC}/os/mxwo/_A"}, {"href": f"{OSLC}/os/mxwo/_B"}],
                "several_sites",
                id="several-sites",
            ),
            pytest.param(
                "WO-001",
                [{"wonum": "WO-002", "href": _INTERNAL_URI}],
                "other_work_order",
                id="other-work-order",
            ),
            pytest.param(
                "WO-001",
                [{"href": f"http://maximo-internal:9080/maximo/oslc/os/mxasset/{_REST_ID}"}],
                "no_work_order_uri",
                id="no-work-order-uri",
            ),
            pytest.param(
                "WO-001",
                [{"href": f"{OSLC}/os/mxwo/.."}],
                "invalid_rest_id",
                id="invalid-rest-id",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_refusal_is_logged_for_the_operator(
        self,
        httpx_mock,
        connector: MaximoConnector,
        wonum: str,
        members: list[dict[str, object]],
        reason: str,
    ) -> None:
        """A proxy or response-shape problem must look different from an
        unknown wonum; the log never carries what Maximo returned."""
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, wonum, *members)
        with structlog.testing.capture_logs() as logs, pytest.raises(ConnectorError):
            await connector.update_work_order(wonum, description="x")
        assert [e for e in logs if e["event"] == "maximo_update_refused"] == [
            {
                "event": "maximo_update_refused",
                "connector": "MaximoConnector",
                "operation": "update_work_order",
                "work_order_id": wonum,
                "reason": reason,
                "log_level": "warning",
            }
        ]
        assert "maximo-internal" not in str(logs)

    @pytest.mark.parametrize(
        ("post_status", "error", "match"),
        [
            pytest.param(400, ConnectorError, "update work order failed: HTTP 400", id="rejected"),
            pytest.param(401, ConnectorAuthError, "authentication failed", id="auth"),
            pytest.param(404, ConnectorError, "update work order failed: HTTP 404", id="gone"),
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
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=post_status)
        with pytest.raises(error, match=match):
            await connector.update_work_order("WO-001", description="x")
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET", "POST"]

    @pytest.mark.parametrize(
        ("read_back", "error_type"),
        [
            # A collection query without oslc.select returns only each member's
            # href; a read-back that parsed such a row could not build the order.
            pytest.param({"json": {"href": _INTERNAL_URI}}, "ValidationError", id="href-only"),
            pytest.param({"status_code": 404}, "ConnectorError", id="not-found"),
            pytest.param({"status_code": 500}, "ConnectorError", id="server-error"),
            pytest.param({"status_code": 401}, "ConnectorAuthError", id="auth"),
            pytest.param({"text": "<html>Sign in</html>"}, "JSONDecodeError", id="not-json"),
            pytest.param({"json": [{"wonum": "WO-001"}]}, "ConnectorError", id="not-an-object"),
            # parse_datetime expects text; a number raises AttributeError.
            pytest.param(
                {"json": {"wonum": "WO-001", "changedate": 1729000000000}},
                "AttributeError",
                id="unparseable-field",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_failed_read_back_says_the_update_was_applied(
        self, httpx_mock, connector: MaximoConnector, read_back: dict[str, object], error_type: str
    ) -> None:
        """Once Maximo accepted the update, a failed read-back must not look like
        a failed update: a caller that sees an error would send the update again."""
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=204)
        httpx_mock.add_response(method="GET", url=_UPDATE_URL, **read_back)
        with structlog.testing.capture_logs() as logs, pytest.raises(ConnectorError) as exc_info:
            await connector.update_work_order("WO-001", description="x")
        assert str(exc_info.value) == _APPLIED_NOT_READ_BACK
        # Neither chained nor shown as context: the library error's text stays out.
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__ is True
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET", "POST", "GET"]
        assert [e for e in logs if e["event"] == "work_order_update_not_read_back"] == [
            {
                "event": "work_order_update_not_read_back",
                "connector": "MaximoConnector",
                "operation": "update_work_order",
                "work_order_id": "WO-001",
                "error_type": error_type,
                "log_level": "warning",
            }
        ]


class TestUpdateRetrySafety:
    """The update POST follows POST's retry policy: a 429 is retried, because
    Maximo refused it unprocessed, but a network error or a 503 is not replayed,
    because the first POST may already have been applied. The lookup and the
    read-back are GETs and are retried like any read. pytest-httpx answers each
    registered response once, so a replay would find no response."""

    @pytest.fixture(autouse=True)
    def _no_backoff_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("machina.connectors.cmms.retry.asyncio.sleep", _fake_sleep)

    @pytest.mark.asyncio
    async def test_503_is_not_replayed(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=503)
        with pytest.raises(ConnectorError, match="HTTP 503"):
            await connector.update_work_order("WO-001", description="x")
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET", "POST"]

    @pytest.mark.asyncio
    async def test_network_error_is_not_replayed(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"href": _INTERNAL_URI})
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"), method="POST", url=_UPDATE_URL)
        with pytest.raises(httpx.ReadTimeout):
            await connector.update_work_order("WO-001", description="x")
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET", "POST"]

    @pytest.mark.asyncio
    async def test_429_is_retried(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=429)
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=204)
        _add_reread(httpx_mock, "WAPPR")
        await connector.update_work_order("WO-001", description="x")
        methods = [r.method for r in httpx_mock.get_requests()]
        assert methods == ["GET", "GET", "POST", "POST", "GET"]

    @pytest.mark.asyncio
    async def test_lookup_is_retried_like_a_read(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_exception(
            httpx.ConnectError("refused"), method="GET", url=_lookup_url("WO-001")
        )
        _add_lookup(httpx_mock, "WO-001", {"href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=204)
        _add_reread(httpx_mock, "WAPPR")
        await connector.update_work_order("WO-001", description="x")
        methods = [r.method for r in httpx_mock.get_requests()]
        assert methods == ["GET", "GET", "GET", "POST", "GET"]

    @pytest.mark.asyncio
    async def test_read_back_network_error_says_the_update_was_applied(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """The read-back is retried as a read; when it still fails, the POST is
        not sent again and the error says it was applied."""
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=204)
        httpx_mock.add_exception(
            httpx.ConnectError("refused"), method="GET", url=_UPDATE_URL, is_reusable=True
        )
        with pytest.raises(ConnectorError) as exc_info:
            await connector.update_work_order("WO-001", description="x")
        assert str(exc_info.value) == _APPLIED_NOT_READ_BACK
        methods = [r.method for r in httpx_mock.get_requests()]
        assert methods.count("POST") == 1
        assert methods[3:] == ["GET"] * 4  # the read-back and its three retries


class TestUpdateResourceIdentity:
    """An update never puts the ``wonum`` in a URL path: the ``wonum`` is a
    quoted oslc.where value in the lookup, and the POST and read-back URLs take
    the rest id from the URI Maximo returns, which must stay one path segment."""

    @pytest.mark.parametrize(
        "wonum",
        [
            # Interpolated into the path as-is, these would address another record.
            pytest.param("WO-1/../../mxasset/X", id="dot-segments"),
            pytest.param("../mxasset/PUMP-201", id="leading-dot-segment"),
            pytest.param("WO-1/mxasset", id="slash"),
        ],
    )
    @pytest.mark.asyncio
    async def test_wonum_reaches_only_the_where_clause(
        self, httpx_mock, connector: MaximoConnector, wonum: str
    ) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, wonum, {"href": _INTERNAL_URI})
        httpx_mock.add_response(method="POST", url=_UPDATE_URL, status_code=204)
        _add_reread(httpx_mock, "WAPPR", wonum=wonum)
        await connector.update_work_order(wonum, description="x")
        post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        assert post.url.raw_path == f"/maximo/oslc/os/mxwo/{_REST_ID}?lean=1".encode()

    @pytest.mark.parametrize(
        ("href", "segment"),
        [
            pytest.param(f"{OSLC}/os/mxwo/_A%2FB", "_A%2FB", id="encoded-slash"),
            pytest.param(f"{OSLC}/os/mxwo/_A%3Flean%3D0", "_A%3Flean%3D0", id="encoded-query"),
            pytest.param(f"{OSLC}/os/MXWO/{_REST_ID}", _REST_ID, id="upper-case-structure"),
            pytest.param(f"/maximo/oslc/os/mxwo/{_REST_ID}", _REST_ID, id="relative-uri"),
            pytest.param(f"{OSLC}/os/mxwo/{_REST_ID}?lean=0#x", _REST_ID, id="query-dropped"),
            pytest.param(
                "https://proxy/maximo/api/os/mxwo/_6KW~5a6J", "_6KW~5a6J", id="api-route"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_rest_id_stays_inside_its_path_segment(
        self, httpx_mock, connector: MaximoConnector, href: str, segment: str
    ) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", {"href": href})
        httpx_mock.add_response(method="POST", status_code=204)
        _add_reread(httpx_mock, "WAPPR", rest_id=segment)
        await connector.update_work_order("WO-001", description="x")
        expected = f"/maximo/oslc/os/mxwo/{segment}?lean=1".encode()
        post, reread = httpx_mock.get_requests()[2:]
        assert post.url.host == reread.url.host == "maximo.example.com"
        assert post.url.raw_path == reread.url.raw_path == expected

    @pytest.mark.parametrize(
        ("member", "reason"),
        [
            pytest.param({"siteid": "BEDFORD"}, "no work-order URI", id="no-href"),
            pytest.param({"href": None}, "no work-order URI", id="null-href"),
            pytest.param({"href": 7}, "no work-order URI", id="non-string-href"),
            pytest.param({"href": "http://[x/os/mxwo/_A"}, "no work-order URI", id="unparseable"),
            pytest.param(
                {"href": f"{OSLC}/os/mxasset/{_REST_ID}"}, "no work-order URI", id="other-object"
            ),
            pytest.param(
                {"href": f"{OSLC}/os/mxwo/{_REST_ID}/"}, "no work-order URI", id="trailing-slash"
            ),
            # Decoded with replacement, %FF would become another rest id.
            pytest.param(
                {"href": f"{OSLC}/os/mxwo/_A%FF"}, "no work-order URI", id="non-utf8-escape"
            ),
            pytest.param({"href": f"{OSLC}/os/mxwo/"}, "Invalid record ID", id="empty-rest-id"),
            pytest.param({"href": f"{OSLC}/os/mxwo/.."}, "Invalid record ID", id="dot-segment"),
            pytest.param(
                {"href": f"{OSLC}/os/mxwo/%2E%2E"}, "Invalid record ID", id="encoded-dot-segment"
            ),
            # Behind a proxy that decodes %2F, this rest id would reach /os/mxasset/X.
            pytest.param(
                {"href": f"{OSLC}/os/mxwo/_A%2F..%2Fmxasset%2FX"},
                "Invalid record ID",
                id="encoded-traversal",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_unexpected_uri_is_refused_before_writing(
        self, httpx_mock, connector: MaximoConnector, member: dict[str, object], reason: str
    ) -> None:
        await _connect(httpx_mock, connector)
        _add_lookup(httpx_mock, "WO-001", member)
        with pytest.raises(ConnectorError, match=reason) as exc_info:
            await connector.update_work_order("WO-001", description="x")
        # The URI can name an internal host; no message repeats it.
        assert "maximo-internal" not in str(exc_info.value)
        assert "/os/" not in str(exc_info.value)
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET"]  # no POST

    @pytest.mark.asyncio
    async def test_rest_id_without_a_utf8_form_is_refused_before_writing(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """JSON's ``\\ud800`` escape decodes to a lone surrogate, which has no UTF-8
        form; the codec's own error would quote it."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_lookup_url("WO-001"),
            content=b'{"member": [{"wonum": "WO-001", "href": "/maximo/oslc/os/mxwo/_A\\ud800"}]}',
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(ConnectorError) as exc_info:
            await connector.update_work_order("WO-001", description="x")
        assert str(exc_info.value) == (
            "Invalid record ID: an ID must be text that can be sent as UTF-8"
        )
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET"]  # no POST


# ---------------------------------------------------------------------------
# Caller-supplied values stay inside their oslc.where literal
# ---------------------------------------------------------------------------

# Closes the string literal and appends a term every row satisfies:
# interpolated as-is, ``itemnum="<value>"`` becomes an unbounded read.
_BREAKOUT = 'X" or itemnum!="'


class TestWhereValuesStayLiteral:
    """IDs, SKUs and raw statuses reach oslc.where from LLM and MCP tool input.

    Maximo documents no escape inside a where literal, so a value that could
    leave it or widen the match is refused before any request: nothing is
    mocked here, and a request would fail the test.
    """

    @pytest.mark.asyncio
    async def test_get_asset(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="Invalid assetnum"):
            await connector.get_asset(_BREAKOUT)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("argument", "attribute"), [("asset_id", "assetnum"), ("status", "status")]
    )
    async def test_read_work_orders(
        self, httpx_mock, connector: MaximoConnector, argument: str, attribute: str
    ) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match=f"Invalid {attribute}"):
            await connector.read_work_orders(**{argument: _BREAKOUT})

    @pytest.mark.asyncio
    async def test_get_work_order(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="Invalid wonum"):
            await connector.get_work_order(_BREAKOUT)

    @pytest.mark.asyncio
    async def test_update_work_order_refuses_before_any_request(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """The update looks the order up by wonum, so it refuses before the lookup."""
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="Invalid wonum"):
            await connector.update_work_order("A,B", description="x")

    @pytest.mark.asyncio
    async def test_read_maintenance_history(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="Invalid assetnum"):
            await connector.read_maintenance_history(_BREAKOUT)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sku",
        [
            pytest.param(_BREAKOUT, id="quote"),
            pytest.param("X\\", id="backslash-escaping-the-closing-quote"),
            pytest.param("%", id="like-wildcard"),
            pytest.param("*", id="not-null"),
            pytest.param("BRG-6205,BRG-6206", id="qbe-alternatives"),
            pytest.param("!=BRG-6205", id="qbe-operator"),
            pytest.param("~null~", id="qbe-null"),
            pytest.param("\x01BRG-6205", id="control-character"),
            pytest.param("  ", id="blank"),
        ],
    )
    async def test_read_spare_parts_refuses(
        self, httpx_mock, connector: MaximoConnector, sku: str
    ) -> None:
        await _connect(httpx_mock, connector)
        # A blank sku is no ID at all: the shared filter check refuses it
        # before the value reaches _oslc_eq.
        expected = "the sku filter is not an ID" if not sku.strip() else "Invalid itemnum"
        with pytest.raises(ConnectorError, match=expected):
            await connector.read_spare_parts(sku=sku)

    @pytest.mark.asyncio
    async def test_ordinary_punctuation_is_sent_unchanged(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxinventory", **{"oslc.where": 'itemnum="6205-2RS/C3_A.1"'}),
            json={"member": [], "responseInfo": {}},
        )
        assert await connector.read_spare_parts(sku="6205-2RS/C3_A.1") == []


# Every method that puts a caller-supplied value into oslc.where, called with it.
def _read_history(conn: MaximoConnector, value: object):
    return conn.read_maintenance_history(value)  # type: ignore[arg-type]


def _read_spare_parts_by_sku(conn: MaximoConnector, value: object):
    return conn.read_spare_parts(sku=value)  # type: ignore[arg-type]


_FILTERED_CALLS = [
    pytest.param(lambda c, v: c.get_asset(v), id="get_asset"),
    pytest.param(lambda c, v: c.get_work_order(v), id="get_work_order"),
    pytest.param(lambda c, v: c.read_work_orders(asset_id=v), id="read_work_orders-asset_id"),
    pytest.param(lambda c, v: c.read_work_orders(status=v), id="read_work_orders-status"),
    pytest.param(_read_spare_parts_by_sku, id="read_spare_parts-sku"),
    pytest.param(_read_history, id="read_maintenance_history"),
    # Looks the order up by wonum before writing, so the value is refused before the lookup.
    pytest.param(lambda c, v: c.update_work_order(v, description="x"), id="update_work_order"),
    pytest.param(lambda c, v: c.close_work_order(v), id="close_work_order"),
    pytest.param(lambda c, v: c.cancel_work_order(v), id="cancel_work_order"),
]

# Values that would rewrite or widen the query if they reached oslc.where.
_HOSTILE_VALUES = [
    pytest.param('PUMP-201" or assetnum="%', id="quote-breakout"),
    pytest.param("%", id="like-wildcard"),
    pytest.param("*", id="not-null-test"),
    pytest.param("PUMP-201,COMP-301", id="qbe-value-list"),
    pytest.param("!=PUMP-201", id="qbe-operator"),
    pytest.param("null", id="qbe-null-keyword"),
    pytest.param("   ", id="blank"),
]


class TestOslcWhereValues:
    """Each hostile value against every method that builds an oslc.where clause."""

    @pytest.mark.asyncio
    async def test_identifier_punctuation_reaches_the_clause_verbatim(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(
                "mxasset",
                **{"oslc.pageSize": "1", "oslc.where": 'assetnum="BR300/P_201 O\'NEIL"'},
            ),
            json={"member": [], "responseInfo": {}},
        )
        assert await connector.get_asset("BR300/P_201 O'NEIL") is None

    @pytest.mark.asyncio
    async def test_numeric_id_is_quoted_like_its_string(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """Tool arguments parsed from model JSON can carry an asset number as a number."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxasset", **{"oslc.pageSize": "1", "oslc.where": 'assetnum="11430"'}),
            json={"member": [], "responseInfo": {}},
        )
        assert await connector.get_asset(11430) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", _HOSTILE_VALUES)
    @pytest.mark.parametrize("call", _FILTERED_CALLS)
    async def test_hostile_value_is_refused_before_any_request(
        self, httpx_mock, connector: MaximoConnector, call, value: str
    ) -> None:
        await _connect(httpx_mock, connector)
        # read_maintenance_history refuses a blank asset ID with its own
        # missing-ID message, and read_spare_parts a blank sku with the shared
        # filter check, before the value reaches _oslc_eq.
        expected = "for a Maximo query"
        if not value.strip() and call is _read_history:
            expected = "requires an asset_id"
        elif not value.strip() and call is _read_spare_parts_by_sku:
            expected = "the sku filter is not an ID"
        with pytest.raises(ConnectorError, match=expected):
            await call(connector, value)
        # Only the whoami handshake went out: no OSLC query, no write.
        assert [r.url.path for r in httpx_mock.get_requests()] == ["/maximo/oslc/whoami"]


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


# ---------------------------------------------------------------------------
# Reads select the attributes the mappers read; members that cannot become
# entities are refused
# ---------------------------------------------------------------------------

# Maximo reports a member's URI on the host it sees itself on; its last
# segment is the record's rest id.
_MEMBER_REST_ID = "_QkVERk9SRC9YMQ--"
_MEMBER_HREF = "http://maximo-internal:9080/maximo/oslc/os/{os}/" + _MEMBER_REST_ID

# One record per object structure as Maximo returns it for the read's
# oslc.select with lean=1, after IBM's REST API guide, "Selecting"
# (https://ibm-maximo-dev.github.io/maximo-restapi-documentation/query/selecting/):
# the selected attributes that are not null (null ones are left out), a
# related object's attributes as a nested object (item.description), a child
# object as a list of rows (invbalances{curbal}), plus href and _rowstamp on
# every member and the status_description Maximo adds next to a domain-bound
# status. Not checked against a live Maximo.
_RECORDS: dict[str, dict[str, object]] = {
    "mxasset": {
        "assetnum": "PUMP-201",
        "siteid": "BEDFORD",
        "description": "Centrifugal pump",
        "location": "BR300",
        "manufacturer": "GRUNDFOS",
        "priority": 1,
        "classstructureid": "1020",
    },
    "mxwo": {
        "wonum": "WO-1",
        "siteid": "BEDFORD",
        "description": "Replace seal",
        "status": "INPRG",
        "status_description": "In Progress",
        "wopriority": 2,
        "worktype": "CM",
        "assetnum": "PUMP-201",
        "lead": "JSMITH",
        "problemcode": "LEAK",
        "reportdate": "2026-10-01T08:00:00+00:00",
        "changedate": "2026-10-02T09:30:00+00:00",
    },
    "mxinventory": {
        "itemnum": "BRG-6205",
        "siteid": "BEDFORD",
        "location": "CENTRAL",
        "minlevel": 5,
        "item": {"description": "Bearing SKF 6205"},
        "invbalances": [{"curbal": 3}, {"curbal": 4}],
        "invcost": [{"avgcost": 12.5, "lastcost": 13.0}],
    },
    "mxpm": {
        "pmnum": "PM-1",
        "siteid": "BEDFORD",
        "assetnum": "PUMP-201",
        "description": "Quarterly inspection",
        "frequency": 3,
        "frequnit": "MONTHS",
        "status": "ACTIVE",
    },
}
_IDENTITY = {"mxasset": "assetnum", "mxwo": "wonum", "mxinventory": "itemnum", "mxpm": "pmnum"}


def _member(object_structure: str, record: dict[str, object] | None = None) -> dict[str, object]:
    """A member as Maximo sends it: the record plus href and _rowstamp."""
    base = _RECORDS[object_structure] if record is None else record
    return {**base, "href": _MEMBER_HREF.format(os=object_structure), "_rowstamp": "3277171"}


# Every public read: the call, its object structure, the query params it
# adds to the defaults (oslc.select included by _oslc_url), and what its
# maximo_member_refused warning names besides the object structure.
_READS = [
    pytest.param(
        lambda c: c.read_assets(),
        "mxasset",
        {},
        {"operation": "read_assets"},
        id="read_assets",
    ),
    pytest.param(
        lambda c: c.get_asset("PUMP-201"),
        "mxasset",
        {"oslc.pageSize": "1", "oslc.where": 'assetnum="PUMP-201"'},
        {"operation": "get_asset", "asset_id": "PUMP-201"},
        id="get_asset",
    ),
    pytest.param(
        lambda c: c.read_work_orders(),
        "mxwo",
        {},
        {"operation": "read_work_orders"},
        id="read_work_orders",
    ),
    pytest.param(
        lambda c: c.read_work_orders(asset_id="PUMP-201", status=WorkOrderStatus.IN_PROGRESS),
        "mxwo",
        {"oslc.where": 'assetnum="PUMP-201" and status="INPRG"'},
        {"operation": "read_work_orders", "asset_id": "PUMP-201"},
        id="read_work_orders-filtered",
    ),
    pytest.param(
        lambda c: c.get_work_order("WO-1"),
        "mxwo",
        {"oslc.pageSize": "1", "oslc.where": 'wonum="WO-1"'},
        {"operation": "get_work_order"},
        id="get_work_order",
    ),
    pytest.param(
        lambda c: c.read_maintenance_history("PUMP-201"),
        "mxwo",
        {"oslc.where": 'assetnum="PUMP-201" and status in ["COMP","CLOSE"]'},
        {"operation": "read_maintenance_history", "asset_id": "PUMP-201"},
        id="read_maintenance_history",
    ),
    pytest.param(
        lambda c: c.read_spare_parts(),
        "mxinventory",
        {},
        {"operation": "read_spare_parts"},
        id="read_spare_parts",
    ),
    pytest.param(
        lambda c: c.read_spare_parts(sku="BRG-6205"),
        "mxinventory",
        {"oslc.where": 'itemnum="BRG-6205"'},
        {"operation": "read_spare_parts"},
        id="read_spare_parts-sku",
    ),
    pytest.param(
        lambda c: c.read_maintenance_plans(),
        "mxpm",
        {},
        {"operation": "read_maintenance_plans"},
        id="read_maintenance_plans",
    ),
]


def _entities(result: object) -> list[object]:
    return result if isinstance(result, list) else [result]


def _refusals(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] == "maximo_member_refused"]


def _refusal(object_structure: str, reason: str, position: int, **context: object) -> dict:
    """The whole maximo_member_refused event: names and codes, no Maximo value."""
    return {
        "event": "maximo_member_refused",
        "connector": "MaximoConnector",
        "object_structure": object_structure,
        "reason": reason,
        "position": position,
        "log_level": "warning",
        **context,
    }


class TestReadsSelectAttributes:
    """IBM's REST API guide ("Selecting"): a collection query without
    oslc.select returns each member's href and nothing else."""

    @pytest.mark.parametrize(("read", "object_structure", "params", "log"), _READS)
    @pytest.mark.asyncio
    async def test_read_selects_and_maps_the_record(
        self, httpx_mock, connector: MaximoConnector, read, object_structure: str, params, log
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(object_structure, **params),
            json={"member": [_member(object_structure)], "responseInfo": {}},
        )
        [entity] = _entities(await read(connector))
        request = httpx_mock.get_requests()[-1]
        assert request.url.params["oslc.select"] == _SELECT[object_structure]
        # OSLC's own keys carry Maximo's host; they stay out of metadata, and
        # the rest id is kept without it. (MaintenancePlan has no metadata.)
        metadata = getattr(entity, "metadata", None)
        if metadata is not None:
            assert not {"href", "_rowstamp"} & set(metadata)
            assert metadata["rest_id"] == _MEMBER_REST_ID
        assert "maximo-internal" not in str(entity.model_dump())

    @pytest.mark.asyncio
    async def test_asset_fields(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxasset"),
            json={"member": [_member("mxasset")], "responseInfo": {}},
        )
        [asset] = await connector.read_assets()
        assert (asset.id, asset.name, asset.location, asset.manufacturer) == (
            "PUMP-201",
            "Centrifugal pump",
            "BR300",
            "GRUNDFOS",
        )
        assert asset.criticality == Criticality.A  # priority 1
        # Null attributes are left out of the answer: serialnum and parent.
        assert asset.serial_number == ""
        assert asset.parent is None
        assert asset.metadata == {
            "siteid": "BEDFORD",
            "classstructureid": "1020",
            "rest_id": _MEMBER_REST_ID,
        }

    @pytest.mark.asyncio
    async def test_asset_without_a_priority_is_not_critical(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """A null priority is left out of the answer; it is not priority 0."""
        await _connect(httpx_mock, connector)
        record = {k: v for k, v in _RECORDS["mxasset"].items() if k != "priority"}
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxasset"),
            json={"member": [_member("mxasset", record)], "responseInfo": {}},
        )
        [asset] = await connector.read_assets()
        assert asset.criticality == Criticality.C

    @pytest.mark.asyncio
    async def test_work_order_fields(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo"),
            json={"member": [_member("mxwo")], "responseInfo": {}},
        )
        [wo] = await connector.read_work_orders()
        assert wo.id == "WO-1"
        assert wo.status == WorkOrderStatus.IN_PROGRESS
        assert wo.priority == Priority.HIGH
        assert wo.asset_id == "PUMP-201"
        assert wo.assigned_to == "JSMITH"
        assert wo.failure_cause == "LEAK"
        assert wo.created_at.isoformat() == "2026-10-01T08:00:00+00:00"
        assert wo.metadata == {
            "siteid": "BEDFORD",
            "status_description": "In Progress",
            "rest_id": _MEMBER_REST_ID,
        }

    @pytest.mark.asyncio
    async def test_spare_part_fields(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxinventory"),
            json={"member": [_member("mxinventory")], "responseInfo": {}},
        )
        [part] = await connector.read_spare_parts()
        assert (part.sku, part.name, part.warehouse_location) == (
            "BRG-6205",
            "Bearing SKF 6205",
            "CENTRAL",
        )
        assert (part.stock_quantity, part.reorder_point, part.unit_cost) == (7, 5, 12.5)
        assert part.metadata == {"siteid": "BEDFORD", "rest_id": _MEMBER_REST_ID}

    @pytest.mark.parametrize(
        ("change", "fields"),
        [
            pytest.param(
                {"invbalances": [{"curbal": 2}, {"curbal": -5}]}, ["stock_quantity"], id="balance"
            ),
            pytest.param({"invcost": [{"avgcost": -0.5}]}, ["unit_cost"], id="cost"),
            pytest.param(
                {"invbalances": [{"curbal": -5}], "invcost": [{"avgcost": -0.5}]},
                ["stock_quantity", "unit_cost"],
                id="both",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_negative_balance_or_cost_ends_the_read(
        self, httpx_mock, connector: MaximoConnector, change: dict[str, object], fields: list[str]
    ) -> None:
        """An organization that allows negative balances can issue past a bin's
        balance. A SparePart holds no negative stock or cost, so the read says
        which field it cannot represent instead of reporting a figure Maximo
        does not hold."""
        await _connect(httpx_mock, connector)
        record = {**_RECORDS["mxinventory"], **change}
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxinventory"),
            json={
                "member": [_member("mxinventory"), _member("mxinventory", record)],
                "responseInfo": {},
            },
        )
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectorSchemaError) as exc_info,
        ):
            await connector.read_spare_parts()
        assert str(exc_info.value) == (
            "Maximo returned a mxinventory record that cannot be mapped "
            f"(ValidationError: {', '.join(fields)}) — refusing to return a partial result"
        )
        assert _refusals(logs) == [
            _refusal(
                "mxinventory",
                "unmappable_ValidationError",
                1,
                operation="read_spare_parts",
                fields=fields,
            )
        ]
        assert "-5" not in str(logs) + str(exc_info.value)
        assert "-0.5" not in str(logs) + str(exc_info.value)

    @pytest.mark.asyncio
    async def test_mcp_spare_parts_tool_reports_the_refusal(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """What an MCP client gets, through a real MCP session: an error result
        with the refusal, not a list of parts with a figure Maximo does not hold."""
        # The MCP SDK is the [mcp] extra: the core CI jobs skip this test, and
        # the optional-extras jobs, which install it, run it.
        pytest.importorskip("mcp", reason="MCP SDK not installed (pip install machina-ai[mcp])")
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock

        from mcp.server.fastmcp import FastMCP
        from mcp.shared.memory import create_connected_server_and_client_session

        from machina.mcp.tools import machina_list_spare_parts

        await _connect(httpx_mock, connector)
        record = {**_RECORDS["mxinventory"], "invbalances": [{"curbal": -5}]}
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxinventory"),
            json={"member": [_member("mxinventory", record)], "responseInfo": {}},
        )
        runtime = MagicMock(sandbox_mode=False)
        runtime.get_primary_cmms.return_value = connector

        @asynccontextmanager
        async def lifespan(_server):
            yield {"runtime": runtime}

        server = FastMCP("maximo-test", lifespan=lifespan)
        server.add_tool(machina_list_spare_parts)
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            result = await client.call_tool("machina_list_spare_parts", {})
        assert result.isError
        [content] = result.content
        assert content.text == (
            "Error executing tool machina_list_spare_parts: Maximo returned a mxinventory record "
            "that cannot be mapped (ValidationError: stock_quantity) — refusing to return a "
            "partial result"
        )

    @pytest.mark.asyncio
    async def test_maintenance_plan_fields(self, httpx_mock, connector: MaximoConnector) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxpm"),
            json={"member": [_member("mxpm")], "responseInfo": {}},
        )
        [plan] = await connector.read_maintenance_plans()
        assert (plan.id, plan.asset_id, plan.active) == ("PM-1", "PUMP-201", True)
        assert plan.interval.months == 3  # frequency 3, frequnit MONTHS

    @pytest.mark.parametrize(("read", "object_structure", "params", "log"), _READS)
    @pytest.mark.asyncio
    async def test_href_only_member_is_refused(
        self, httpx_mock, connector: MaximoConnector, read, object_structure: str, params, log
    ) -> None:
        """What Maximo answers when the selection is missing or not applied, or
        when the object structure does not expose the identity attribute."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(object_structure, **params),
            json={"member": [_member(object_structure, {})], "responseInfo": {}},
        )
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectorSchemaError) as exc_info,
        ):
            await read(connector)
        identity = _IDENTITY[object_structure]
        assert str(exc_info.value) == (
            f"Maximo returned a {object_structure} record without its '{identity}' attribute: "
            "the query's oslc.select may not have been applied, or the object structure may "
            "not expose that attribute — refusing to return a partial result"
        )
        assert _refusals(logs) == [_refusal(object_structure, "missing_identity", 0, **log)]
        # The member's href names Maximo's own host; neither carries it.
        assert "maximo-internal" not in str(exc_info.value) + str(logs)

    @pytest.mark.parametrize(("read", "object_structure", "params", "log"), _READS)
    @pytest.mark.asyncio
    async def test_member_with_only_its_identity_is_read(
        self, httpx_mock, connector: MaximoConnector, read, object_structure: str, params, log
    ) -> None:
        """Only the identity attribute is required: every other attribute Maximo
        leaves out (a null one) reads as the field's default."""
        await _connect(httpx_mock, connector)
        identity = _IDENTITY[object_structure]
        record = {identity: _RECORDS[object_structure][identity]}
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(object_structure, **params),
            json={"member": [_member(object_structure, record)], "responseInfo": {}},
        )
        with structlog.testing.capture_logs() as logs:
            [entity] = _entities(await read(connector))
        assert _refusals(logs) == []
        entity_id = entity.sku if object_structure == "mxinventory" else entity.id
        assert entity_id == record[identity]

    @pytest.mark.asyncio
    async def test_rest_id_from_a_read_is_written_through_the_configured_url(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """metadata["rest_id"] comes from Maximo's answer: a write to it goes to
        the configured url, never to the host in href, as one checked path
        segment."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo", **{"oslc.pageSize": "1", "oslc.where": 'wonum="WO-1"'}),
            json={"member": [_member("mxwo")], "responseInfo": {}},
        )
        httpx_mock.add_response(
            method="POST",
            url=httpx.URL(f"{OSLC}/os/mxwo/{_MEMBER_REST_ID}", params={"lean": "1"}),
            status_code=204,
        )
        wo = await connector.get_work_order("WO-1")
        assert wo is not None
        await connector._patch_resource("mxwo", wo.metadata["rest_id"], {"description": "x"})
        post = httpx_mock.get_requests()[-1]
        assert (post.url.host, post.url.path) == (
            "maximo.example.com",
            f"/maximo/oslc/os/mxwo/{_MEMBER_REST_ID}",
        )

    @pytest.mark.parametrize(
        ("read", "object_structure", "record", "error_type", "log", "leak"),
        [
            # Asset.parent is text; a list cannot be one.
            pytest.param(
                lambda c: c.read_assets(),
                "mxasset",
                {"parent": ["LINE-2"]},
                "ValidationError",
                {"operation": "read_assets", "fields": ["parent"]},
                "LINE-2",
                id="asset",
            ),
            # parse_datetime expects ISO text.
            pytest.param(
                lambda c: c.read_work_orders(),
                "mxwo",
                {"reportdate": 1729000000000},
                "AttributeError",
                {"operation": "read_work_orders"},
                "1729000000000",
                id="work_order",
            ),
            pytest.param(
                lambda c: c.read_spare_parts(),
                "mxinventory",
                {"invbalances": [{"curbal": "many"}]},
                "ValueError",
                {"operation": "read_spare_parts"},
                "many",
                id="spare_part",
            ),
            pytest.param(
                lambda c: c.read_spare_parts(),
                "mxinventory",
                {"invbalances": ["3"]},
                "AttributeError",
                {"operation": "read_spare_parts"},
                None,
                id="spare_part-row-not-an-object",
            ),
            # An explicit null, as with _dropnulls=0.
            pytest.param(
                lambda c: c.read_spare_parts(),
                "mxinventory",
                {"invbalances": [{"curbal": None}]},
                "TypeError",
                {"operation": "read_spare_parts"},
                None,
                id="spare_part-null",
            ),
            # A number too large for a float (1e999) reads as infinity.
            pytest.param(
                lambda c: c.read_spare_parts(),
                "mxinventory",
                {"invbalances": [{"curbal": "1e999"}]},
                "OverflowError",
                {"operation": "read_spare_parts"},
                None,
                id="spare_part-infinite",
            ),
            pytest.param(
                lambda c: c.read_spare_parts(),
                "mxinventory",
                {"invcost": [{"avgcost": "1e999"}]},
                "ValueError",
                {"operation": "read_spare_parts"},
                None,
                id="spare_part-infinite-cost",
            ),
            pytest.param(
                lambda c: c.read_maintenance_plans(),
                "mxpm",
                {"frequency": "quarterly"},
                "ValueError",
                {"operation": "read_maintenance_plans"},
                "quarterly",
                id="maintenance_plan",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_unmappable_member_ends_the_read(
        self,
        httpx_mock,
        connector: MaximoConnector,
        read,
        object_structure: str,
        record: dict[str, object],
        error_type: str,
        log: dict[str, object],
        leak: str | None,
    ) -> None:
        """One unreadable record would otherwise drop out, or crash the read with
        the mapper's own error; the good record before it is not returned alone."""
        await _connect(httpx_mock, connector)
        bad = _member(object_structure, {**_RECORDS[object_structure], **record})
        body = json.dumps({"member": [_member(object_structure), bad], "responseInfo": {}})
        # json.dumps cannot write 1e999; Maximo's JSON can carry it.
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(object_structure),
            content=body.replace('"1e999"', "1e999").encode(),
        )
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectorSchemaError) as exc_info,
        ):
            await read(connector)
        cause = error_type + (f": {', '.join(log['fields'])}" if "fields" in log else "")
        assert str(exc_info.value) == (
            f"Maximo returned a {object_structure} record that cannot be mapped ({cause}) "
            "— refusing to return a partial result"
        )
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None
        assert _refusals(logs) == [
            _refusal(object_structure, f"unmappable_{error_type}", 1, **log)
        ]
        assert "maximo-internal" not in str(exc_info.value) + str(logs)
        if leak is not None:
            assert leak not in str(logs)
            assert leak not in str(exc_info.value)

    @pytest.mark.parametrize(
        ("read", "object_structure", "params", "record", "log"),
        [
            pytest.param(
                lambda c: c.get_asset("PUMP-201"),
                "mxasset",
                {"oslc.pageSize": "1", "oslc.where": 'assetnum="PUMP-201"'},
                {"parent": ["LINE-2"]},
                {"operation": "get_asset", "asset_id": "PUMP-201", "fields": ["parent"]},
                id="get_asset",
            ),
            pytest.param(
                lambda c: c.get_work_order("WO-1"),
                "mxwo",
                {"oslc.pageSize": "1", "oslc.where": 'wonum="WO-1"'},
                {"reportdate": 1729000000000},
                {"operation": "get_work_order"},
                id="get_work_order",
            ),
            pytest.param(
                lambda c: c.read_maintenance_history("PUMP-201"),
                "mxwo",
                {"oslc.where": 'assetnum="PUMP-201" and status in ["COMP","CLOSE"]'},
                {"reportdate": 1729000000000},
                {"operation": "read_maintenance_history", "asset_id": "PUMP-201"},
                id="read_maintenance_history",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_unmappable_record_ends_a_lookup_or_history_read(
        self,
        httpx_mock,
        connector: MaximoConnector,
        read,
        object_structure: str,
        params,
        record: dict[str, object],
        log: dict[str, object],
    ) -> None:
        await _connect(httpx_mock, connector)
        bad = _member(object_structure, {**_RECORDS[object_structure], **record})
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(object_structure, **params),
            json={"member": [bad], "responseInfo": {}},
        )
        with structlog.testing.capture_logs() as logs, pytest.raises(ConnectorSchemaError):
            await read(connector)
        [event] = _refusals(logs)
        assert event == _refusal(object_structure, event["reason"], 0, **log)
        assert event["reason"].startswith("unmappable_")

    @pytest.mark.asyncio
    async def test_refusal_log_names_fields_not_keys_from_maximo(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """An attribute name text cannot hold fails metadata's key check; the log
        names the field, metadata, not the location below it, which is the name."""
        await _connect(httpx_mock, connector)
        bad = _member("mxwo", {**_RECORDS["mxwo"], "SECRET-NAME\ud800": "x"})
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo"),
            # pytest-httpx cannot write a lone surrogate; json.dumps escapes it.
            content=json.dumps({"member": [bad], "responseInfo": {}}).encode(),
        )
        with structlog.testing.capture_logs() as logs, pytest.raises(ConnectorSchemaError):
            await connector.read_work_orders()
        assert _refusals(logs) == [
            _refusal(
                "mxwo",
                "unmappable_ValidationError",
                0,
                operation="read_work_orders",
                fields=["metadata"],
            )
        ]
        assert "SECRET" not in str(logs)

    @pytest.mark.parametrize(
        ("lookup", "object_structure", "params"),
        [
            pytest.param(
                lambda c: c.get_asset("PUMP-201"),
                "mxasset",
                {"oslc.pageSize": "1", "oslc.where": 'assetnum="PUMP-201"'},
                id="get_asset",
            ),
            pytest.param(
                lambda c: c.get_work_order("WO-1"),
                "mxwo",
                {"oslc.pageSize": "1", "oslc.where": 'wonum="WO-1"'},
                id="get_work_order",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_lookup_maps_only_the_first_member(
        self, httpx_mock, connector: MaximoConnector, lookup, object_structure: str, params
    ) -> None:
        """A server that ignores oslc.pageSize=1 sends more; only the first is used."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(object_structure, **params),
            json={
                "member": [_member(object_structure), _member(object_structure, {})],
                "responseInfo": {},
            },
        )
        entity = await lookup(connector)
        assert entity is not None
        assert entity.id == _RECORDS[object_structure][_IDENTITY[object_structure]]

    @pytest.mark.parametrize(
        "member",
        [
            pytest.param({"wonum": "  ", "status": "INPRG"}, id="blank-identity"),
            pytest.param({"wonum": None}, id="null-identity"),
            pytest.param("WO-1", id="not-an-object"),
        ],
    )
    @pytest.mark.asyncio
    async def test_member_without_a_usable_identity_is_refused(
        self, httpx_mock, connector: MaximoConnector, member: object
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo"),
            json={"member": [member], "responseInfo": {}},
        )
        with pytest.raises(ConnectorSchemaError, match="without its 'wonum' attribute"):
            await connector.read_work_orders()

    @pytest.mark.parametrize(
        "identity",
        [
            pytest.param({"x": 1}, id="object"),
            pytest.param(["WO-1"], id="list"),
            pytest.param(True, id="boolean"),
            pytest.param(1.5, id="fraction"),
        ],
    )
    @pytest.mark.asyncio
    async def test_identity_that_is_not_a_plain_value_is_refused(
        self, httpx_mock, connector: MaximoConnector, identity: object
    ) -> None:
        """Such a value would otherwise become an ID like "{'x': 1}" or "True"."""
        await _connect(httpx_mock, connector)
        record = {**_RECORDS["mxwo"], "wonum": identity}
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo"),
            json={"member": [_member("mxwo", record)], "responseInfo": {}},
        )
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectorSchemaError) as exc_info,
        ):
            await connector.read_work_orders()
        assert str(exc_info.value) == (
            "Maximo returned a mxwo record whose 'wonum' attribute is not text or a whole "
            "number — refusing to return a partial result"
        )
        assert _refusals(logs) == [
            _refusal("mxwo", "invalid_identity", 0, operation="read_work_orders")
        ]

    @pytest.mark.asyncio
    async def test_whole_number_identity_is_read_as_text(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        record = {**_RECORDS["mxwo"], "wonum": 1001}
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo"),
            json={"member": [_member("mxwo", record)], "responseInfo": {}},
        )
        [wo] = await connector.read_work_orders()
        assert wo.id == "1001"

    @pytest.mark.asyncio
    async def test_no_members_is_an_empty_read(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET", url=_oslc_url("mxpm"), json={"member": [], "responseInfo": {}}
        )
        assert await connector.read_maintenance_plans() == []


class TestPagesKeepTheSelection:
    """Next pages are requested at the link Maximo returns, which repeats the
    query; the selection is not re-added. A page whose members come back
    without their attributes ends the read instead of shortening it."""

    @pytest.mark.asyncio
    async def test_next_page_link_carries_the_selection(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        next_href = str(_oslc_url("mxwo", pageno="2"))
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo"),
            json={"member": [_member("mxwo")], "responseInfo": {"nextPage": {"href": next_href}}},
        )
        second = {**_RECORDS["mxwo"], "wonum": "WO-2"}
        httpx_mock.add_response(
            method="GET",
            url=next_href,
            json={"member": [_member("mxwo", second)], "responseInfo": {}},
        )
        orders = await connector.read_work_orders()
        assert [wo.id for wo in orders] == ["WO-1", "WO-2"]
        page_two = httpx_mock.get_requests()[-1]
        assert page_two.url.params["oslc.select"] == _SELECT["mxwo"]
        assert page_two.url.params["pageno"] == "2"

    @pytest.mark.asyncio
    async def test_page_without_the_selection_ends_the_read(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """A next-page link that drops oslc.select brings back href-only members."""
        await _connect(httpx_mock, connector)
        next_href = f"{OSLC}/os/mxwo?pageno=2&lean=1"
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxwo"),
            json={"member": [_member("mxwo")], "responseInfo": {"nextPage": {"href": next_href}}},
        )
        httpx_mock.add_response(
            method="GET",
            url=next_href,
            json={"member": [_member("mxwo", {})], "responseInfo": {}},
        )
        with pytest.raises(ConnectorError, match="without its 'wonum' attribute"):
            await connector.read_work_orders()
        assert len(httpx_mock.get_requests()) == 3  # connect, page 1, page 2

    @pytest.mark.parametrize(
        ("lookup", "object_structure", "params"),
        [
            pytest.param(
                lambda c: c.get_asset("PUMP-201"),
                "mxasset",
                {"oslc.pageSize": "1", "oslc.where": 'assetnum="PUMP-201"'},
                id="get_asset",
            ),
            pytest.param(
                lambda c: c.get_work_order("WO-1"),
                "mxwo",
                {"oslc.pageSize": "1", "oslc.where": 'wonum="WO-1"'},
                id="get_work_order",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_single_record_lookup_reads_one_selected_page(
        self, httpx_mock, connector: MaximoConnector, lookup, object_structure: str, params
    ) -> None:
        """The first match only, as since B4.2: its next page is not followed."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url(object_structure, **params),
            json={
                "member": [_member(object_structure)],
                "responseInfo": {"nextPage": {"href": f"{OSLC}/os/{object_structure}?pageno=2"}},
            },
        )
        entity = await lookup(connector)
        assert entity is not None
        assert len(httpx_mock.get_requests()) == 2  # connect, the one page


# ---------------------------------------------------------------------------
# Spare parts by asset: refused before any request
# ---------------------------------------------------------------------------

# Written out, not imported, so a change to the wording fails these tests.
_ASSET_FILTER_REFUSED = (
    "The Maximo connector cannot filter spare parts by asset: it reads them from "
    "mxinventory, which it cannot query by asset. Look the part up by sku instead."
)
_CONNECT_ONLY = ["/maximo/oslc/whoami"]


def _paths(httpx_mock) -> list[str]:
    return [request.url.path for request in httpx_mock.get_requests()]


class TestReadSparePartsAssetFilter:
    """The connector reads spare parts from mxinventory, which it cannot query
    by asset. Dropping the filter would pass the whole inventory, or a bare sku
    match, off as the asset's parts, so the read is refused before any request."""

    @pytest.mark.asyncio
    async def test_asset_filter_is_refused_before_any_request(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorUnsupportedFilterError) as exc_info:
            await connector.read_spare_parts(asset_id="PUMP-201")
        assert str(exc_info.value) == _ASSET_FILTER_REFUSED
        assert _paths(httpx_mock) == _CONNECT_ONLY

    @pytest.mark.asyncio
    async def test_asset_filter_is_refused_even_with_a_sku(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """A sku match says nothing about the part fitting the asset."""
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorUnsupportedFilterError) as exc_info:
            await connector.read_spare_parts(asset_id="PUMP-201", sku="BRG-6205")
        assert str(exc_info.value) == _ASSET_FILTER_REFUSED
        assert _paths(httpx_mock) == _CONNECT_ONLY

    @pytest.mark.parametrize(
        ("sku", "params"),
        [
            pytest.param("", {}, id="unfiltered"),
            pytest.param("BRG-6205", {"oslc.where": 'itemnum="BRG-6205"'}, id="sku"),
        ],
    )
    @pytest.mark.asyncio
    async def test_empty_asset_id_is_no_filter(
        self, httpx_mock, connector: MaximoConnector, sku: str, params: dict[str, str]
    ) -> None:
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="GET",
            url=_oslc_url("mxinventory", **params),
            json={"member": [_member("mxinventory")], "responseInfo": {}},
        )
        parts = await connector.read_spare_parts(asset_id="", sku=sku)
        assert [part.sku for part in parts] == ["BRG-6205"]
        assert len(httpx_mock.get_requests()) == 2  # connect, the read at that exact URL

    @pytest.mark.parametrize(
        "asset_id", [None, "  ", ["PUMP-201"]], ids=["unresolved", "blank", "list"]
    )
    @pytest.mark.asyncio
    async def test_asset_filter_that_is_not_an_id_is_refused_before_any_request(
        self, httpx_mock, connector: MaximoConnector, asset_id: object
    ) -> None:
        """``None`` (an unresolved workflow placeholder) is not "no filter"."""
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError) as exc_info:
            await connector.read_spare_parts(asset_id=asset_id)  # type: ignore[arg-type]
        assert type(exc_info.value) is ConnectorError
        assert str(exc_info.value).startswith("read_spare_parts: the asset_id filter is not an ID")
        assert _paths(httpx_mock) == _CONNECT_ONLY

    @pytest.mark.asyncio
    async def test_refusal_carries_no_value_and_logs_nothing(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """The caller logs the refusal; the connector repeats neither filter value."""
        await _connect(httpx_mock, connector)
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectorUnsupportedFilterError) as exc_info,
        ):
            await connector.read_spare_parts(asset_id="PUMP-SECRET-201", sku="BRG-SECRET-6205")
        assert "SECRET" not in str(exc_info.value)
        assert logs == []

    @pytest.mark.asyncio
    async def test_mcp_tool_reports_the_asset_filter_refusal(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        pytest.importorskip("mcp", reason="MCP SDK not installed (pip install machina-ai[mcp])")
        # What an MCP client gets, through a real MCP session: an error result
        # with the refusal, and no inventory read. The MCP SDK is the [mcp]
        # extra: the core CI jobs skip this test, the optional-extras jobs run it.
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock

        from mcp.server.fastmcp import FastMCP
        from mcp.shared.memory import create_connected_server_and_client_session

        from machina.mcp.tools import machina_list_spare_parts

        await _connect(httpx_mock, connector)
        runtime = MagicMock(sandbox_mode=False)
        runtime.get_primary_cmms.return_value = connector

        @asynccontextmanager
        async def lifespan(_server):
            yield {"runtime": runtime}

        server = FastMCP("maximo-test", lifespan=lifespan)
        server.add_tool(machina_list_spare_parts)
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            result = await client.call_tool("machina_list_spare_parts", {"asset_id": "P-201"})
        assert result.isError
        [content] = result.content
        assert content.text == (
            "Error executing tool machina_list_spare_parts: The Maximo connector cannot filter "
            "spare parts by asset: it reads them from mxinventory, which it cannot query by "
            "asset. Look the part up by sku instead."
        )
        assert _paths(httpx_mock) == _CONNECT_ONLY
