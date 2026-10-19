"""Integration tests for MaximoConnector REST operations.

All HTTP traffic is intercepted by pytest-httpx — no real Maximo API calls.
"""

from __future__ import annotations

import httpx
import pytest
import structlog.testing

from machina.connectors.base import ConnectorRegistry
from machina.connectors.cmms.auth import ApiKeyHeaderAuth, BasicAuth
from machina.connectors.cmms.maximo import MaximoConnector
from machina.domain.asset import Asset, AssetType
from machina.domain.maintenance_plan import MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import ConnectorAuthError, ConnectorError
from machina.workflows.builtins.alarm_to_workorder import alarm_to_workorder
from machina.workflows.engine import WorkflowEngine
from machina.workflows.models import Workflow

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

    @pytest.mark.asyncio
    async def test_update_work_order_id_stays_one_path_segment(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """Interpolated as-is, this ID would PATCH ``/os/mxwo/WO-1/mxasset``."""
        await _connect(httpx_mock, connector)
        httpx_mock.add_response(
            method="PATCH", url=f"{OSLC}/os/mxwo/WO-1%2Fmxasset", status_code=404
        )
        with pytest.raises(ConnectorError, match="HTTP 404"):
            await connector.update_work_order("WO-1/mxasset", description="x")

    @pytest.mark.asyncio
    async def test_update_work_order_refuses_a_dot_segment_id_before_patching(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """Behind a proxy that decodes ``%2F``, this ID would PATCH ``/os/mxasset/PUMP-201``."""
        await _connect(httpx_mock, connector)
        with pytest.raises(ConnectorError, match="Invalid record ID"):
            await connector.update_work_order("../mxasset/PUMP-201", description="x")
        assert [r.url.path for r in httpx_mock.get_requests()] == ["/maximo/oslc/whoami"]


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
    async def test_update_work_order_refuses_before_patching(
        self, httpx_mock, connector: MaximoConnector
    ) -> None:
        """An ID the read-back after the PATCH could not express is refused up front."""
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
        with pytest.raises(ConnectorError, match="Invalid itemnum"):
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


_FILTERED_CALLS = [
    pytest.param(lambda c, v: c.get_asset(v), id="get_asset"),
    pytest.param(lambda c, v: c.get_work_order(v), id="get_work_order"),
    pytest.param(lambda c, v: c.read_work_orders(asset_id=v), id="read_work_orders-asset_id"),
    pytest.param(lambda c, v: c.read_work_orders(status=v), id="read_work_orders-status"),
    pytest.param(lambda c, v: c.read_spare_parts(sku=v), id="read_spare_parts-sku"),
    pytest.param(_read_history, id="read_maintenance_history"),
    # Re-fetches by wonum after the PATCH, so the value must be refused before it.
    pytest.param(lambda c, v: c.update_work_order(v, description="x"), id="update_work_order"),
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
        # missing-ID message before the value reaches _oslc_eq.
        expected = (
            "requires an asset_id"
            if call is _read_history and not value.strip()
            else "for a Maximo query"
        )
        with pytest.raises(ConnectorError, match=expected):
            await call(connector, value)
        # Only the whoami handshake went out: no OSLC query, no PATCH.
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
