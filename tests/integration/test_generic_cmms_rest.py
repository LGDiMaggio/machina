"""Integration tests for the REST mode of GenericCmmsConnector.

Exercises the full HTTP path via pytest-httpx mock fixtures — no real
network calls. Guards against regressions in the REST layer and confirms
that the schema-mapping feature applied to REST responses works the same
way it does for local JSON mode.

Requires: pytest-httpx (dev dep), httpx (cmms-rest extra).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
from structlog.testing import capture_logs

from machina.connectors.base import set_sandbox_mode
from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.connectors.cmms.generic_coercers import COERCER_REGISTRY
from machina.connectors.cmms.pagination import OffsetLimitPagination
from machina.domain.work_order import (
    FailureImpact,
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)
from machina.exceptions import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorTimeoutError,
    SandboxViolationError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

BASE_URL = "https://cmms.example.com/api"


@pytest.fixture
def rest_connector() -> GenericCmmsConnector:
    """Fresh REST-mode connector; not yet connected."""
    return GenericCmmsConnector(url=BASE_URL, api_key="test-key")


async def _connect_with_health(httpx_mock, conn: GenericCmmsConnector) -> None:
    """Helper: register the health-check response and connect."""
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE_URL}/health",
        status_code=200,
        json={"status": "ok"},
    )
    await conn.connect()


# The failures every REST call maps onto the connector-error hierarchy: an
# error status (status, expected exception type) or a transport failure
# (httpx exception type, expected exception type, what the message says).
_STATUS_FAILURES = [
    pytest.param(401, ConnectorAuthError, id="401"),
    pytest.param(403, ConnectorAuthError, id="403"),
    pytest.param(500, ConnectorError, id="500"),
]
_TRANSPORT_FAILURES = [
    pytest.param(httpx.ReadTimeout, ConnectorTimeoutError, "timed out", id="timeout"),
    pytest.param(httpx.ConnectError, ConnectorError, "failed: ConnectError", id="connect-error"),
]


class TestRestConnection:
    """Connection lifecycle and auth."""

    @pytest.mark.asyncio
    async def test_connect_performs_health_check(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert requests[0].url == f"{BASE_URL}/health"
        assert requests[0].headers["Authorization"] == "Bearer test-key"

    @pytest.mark.asyncio
    async def test_connect_fails_without_api_key(self) -> None:
        conn = GenericCmmsConnector(url=BASE_URL, api_key="")
        with pytest.raises(ConnectorAuthError, match="API key"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_fails_on_non_200_health(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/health",
            status_code=503,
        )
        with pytest.raises(ConnectorError, match="health check failed"):
            await rest_connector.connect()

    @pytest.mark.asyncio
    async def test_connect_requires_200_from_health(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        """The health check must answer 200; another 2xx is not enough."""
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/health", status_code=204)
        with pytest.raises(ConnectorError, match=r"^CMMS health check failed: HTTP 204$"):
            await rest_connector.connect()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status", "expected"), _STATUS_FAILURES)
    async def test_connect_maps_error_status(
        self,
        httpx_mock,
        rest_connector: GenericCmmsConnector,
        status: int,
        expected: type[ConnectorError],
    ) -> None:
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/health", status_code=status)
        with pytest.raises(ConnectorError) as exc_info:
            await rest_connector.connect()
        assert type(exc_info.value) is expected
        assert str(exc_info.value) == f"CMMS health check failed: HTTP {status}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("exc_type", "expected", "outcome"), _TRANSPORT_FAILURES)
    async def test_connect_maps_transport_failure(
        self,
        httpx_mock,
        rest_connector: GenericCmmsConnector,
        exc_type: type[httpx.TransportError],
        expected: type[ConnectorError],
        outcome: str,
    ) -> None:
        url = f"{BASE_URL}/health"
        httpx_mock.add_exception(exc_type(f"simulated failure for {url}"), method="GET", url=url)
        with pytest.raises(ConnectorError) as exc_info:
            await rest_connector.connect()
        assert type(exc_info.value) is expected
        assert str(exc_info.value) == f"CMMS health check {outcome}"
        assert isinstance(exc_info.value.__cause__, exc_type)

    @pytest.mark.asyncio
    async def test_connect_with_a_malformed_url_raises_config_error(self, httpx_mock) -> None:
        """httpx.InvalidURL is not an httpx.HTTPError; it is mapped all the same."""
        conn = GenericCmmsConnector(url="https://cmms.example.com:abc/api", api_key="test-key")
        with pytest.raises(ConnectorConfigError) as exc_info:
            await conn.connect()
        assert str(exc_info.value) == (
            "CMMS health check failed: invalid URL (check 'url' and the endpoint paths)"
        )
        assert isinstance(exc_info.value.__cause__, httpx.InvalidURL)


class TestRestReadAssets:
    """REST asset reads exercise GET /assets and GET /assets/{id}."""

    @pytest.mark.asyncio
    async def test_read_assets_list(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/assets",
            json=[
                {
                    "id": "P-201",
                    "name": "Cooling Water Pump",
                    "type": "rotating_equipment",
                    "criticality": "A",
                    "equipment_class_code": "PU",
                },
                {
                    "id": "COMP-301",
                    "name": "Air Compressor",
                    "type": "rotating_equipment",
                    "criticality": "A",
                    "equipment_class_code": "CO",
                },
            ],
        )
        assets = await rest_connector.read_assets()
        assert len(assets) == 2
        assert {a.id for a in assets} == {"P-201", "COMP-301"}
        # ISO 14224 field round-trips through the REST parser
        p201 = next(a for a in assets if a.id == "P-201")
        assert p201.equipment_class_code == "PU"

    @pytest.mark.asyncio
    async def test_get_asset_by_id(self, httpx_mock, rest_connector: GenericCmmsConnector) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/assets/P-201",
            json={
                "id": "P-201",
                "name": "Cooling Water Pump",
                "type": "rotating_equipment",
                "criticality": "A",
            },
        )
        asset = await rest_connector.get_asset("P-201")
        assert asset is not None
        assert asset.id == "P-201"
        assert asset.name == "Cooling Water Pump"

    @pytest.mark.asyncio
    async def test_read_assets_with_schema_mapping(self, httpx_mock) -> None:
        """A CMMS that calls the asset ID ``asset_id`` and the name
        ``display_name`` should still work via the schema_mapping feature."""
        conn = GenericCmmsConnector(
            url=BASE_URL,
            api_key="test-key",
            schema_mapping={
                "assets": {"asset_id": "id", "display_name": "name"},
            },
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/assets",
            json=[
                {
                    "asset_id": "P-201",
                    "display_name": "Cooling Water Pump",
                    "type": "rotating_equipment",
                    "criticality": "A",
                },
            ],
        )
        assets = await conn.read_assets()
        assert len(assets) == 1
        assert assets[0].id == "P-201"
        assert assets[0].name == "Cooling Water Pump"


class TestRestReadWorkOrders:
    """REST work-order reads exercise GET /work_orders with filters."""

    @pytest.mark.asyncio
    async def test_read_work_orders_sends_filter_params(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders?asset_id=P-201&status=created",
            json=[],
        )
        results = await rest_connector.read_work_orders(asset_id="P-201", status="created")
        assert results == []
        req = httpx_mock.get_requests()[-1]
        assert "asset_id=P-201" in str(req.url)
        assert "status=created" in str(req.url)

    @pytest.mark.asyncio
    async def test_read_work_orders_parses_iso_fields(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        """ISO 14224 WorkOrder fields (failure_impact, failure_cause) should
        round-trip through the REST parser just like local mode."""
        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders",
            json=[
                {
                    "id": "WO-2026-1842",
                    "type": "corrective",
                    "priority": "high",
                    "asset_id": "P-201",
                    "description": "Bearing wear",
                    "failure_mode": "BEAR-WEAR-01",
                    "failure_impact": "critical",
                    "failure_cause": "Expected wear and tear",
                },
            ],
        )
        results = await rest_connector.read_work_orders()
        assert len(results) == 1
        wo = results[0]
        assert wo.id == "WO-2026-1842"
        assert wo.failure_impact == FailureImpact.CRITICAL
        assert wo.failure_cause == "Expected wear and tear"


class TestRestCreateWorkOrder:
    """REST work-order creation posts JSON."""

    @pytest.mark.asyncio
    async def test_create_work_order_posts_json(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        wo_in = WorkOrder(
            id="WO-NEW",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="P-201",
            description="New test WO",
        )
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE_URL}/work_orders",
            status_code=201,
            json={
                "id": "WO-NEW",
                "type": "corrective",
                "priority": "high",
                "asset_id": "P-201",
                "description": "New test WO",
            },
        )
        wo_out = await rest_connector.create_work_order(wo_in)
        assert wo_out.id == "WO-NEW"

        # Verify the POST body was JSON-encoded WO payload
        post_req = [r for r in httpx_mock.get_requests() if r.method == "POST"][-1]
        assert post_req.headers["Content-Type"] == "application/json"
        # POST body should contain the input WO's id
        assert b"WO-NEW" in post_req.content

    @pytest.mark.asyncio
    async def test_create_work_order_raises_on_4xx(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        wo_in = WorkOrder(
            id="WO-BAD",
            type=WorkOrderType.CORRECTIVE,
            asset_id="P-999",
            description="Asset does not exist",
        )
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE_URL}/work_orders",
            status_code=422,
            json={"error": "unknown asset_id"},
        )
        with pytest.raises(ConnectorError) as exc_info:
            await rest_connector.create_work_order(wo_in)
        assert type(exc_info.value) is ConnectorError
        assert str(exc_info.value) == "CMMS create work order failed: HTTP 422"
        assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


class TestRequireHttpx:
    """The _require_httpx helper raises a clear error if httpx is missing."""

    def test_require_httpx_returns_httpx_when_available(self) -> None:
        from machina.connectors.cmms.generic import _require_httpx

        httpx_mod = _require_httpx()
        assert httpx_mod is httpx


class TestModernRestCmmsEndToEnd:
    """End-to-end integration test exercising auth + pagination + JMESPath.

    This simulates a modern CMMS REST API that:

    * Uses HTTP Basic authentication instead of Bearer tokens.
    * Wraps each response in a ``{"data": [...], "meta": {...}}`` envelope.
    * Uses offset/limit pagination with custom parameter names
      (``start`` and ``size``) and deeply nested item structures.

    It verifies that the ``GenericCmmsConnector`` can integrate such an API
    without any custom code beyond configuration.
    """

    BASE_URL = "https://modern-cmms.example.com/v2"

    @pytest.mark.asyncio
    async def test_modern_rest_cmms_end_to_end(self, httpx_mock) -> None:
        import base64

        from machina.connectors.cmms import (
            BasicAuth,
            GenericCmmsConnector,
            OffsetLimitPagination,
        )

        # Build the connector with Basic auth, custom pagination, and
        # JMESPath field extraction from a nested response shape.
        conn = GenericCmmsConnector(
            url=self.BASE_URL,
            auth=BasicAuth(username="svc", password="s3cret"),
            pagination=OffsetLimitPagination(
                limit_param="size",
                offset_param="start",
                page_size=2,
                items_path="data",
            ),
            schema_mapping={
                "assets": {
                    "_fields": {
                        "id": "equipment.id",
                        "name": "equipment.display_name",
                        "type": "meta.asset_type",
                        "criticality": "meta.criticality_class",
                        "equipment_class_code": "meta.iso_code",
                    },
                },
            },
        )

        # Health check (single-shot GET, no pagination)
        httpx_mock.add_response(
            method="GET",
            url=f"{self.BASE_URL}/health",
            status_code=200,
            json={"status": "ok"},
        )
        await conn.connect()

        # First page: 2 items (full page) → pagination keeps going
        httpx_mock.add_response(
            method="GET",
            url=f"{self.BASE_URL}/assets?size=2&start=0",
            json={
                "meta": {"total": 3},
                "data": [
                    {
                        "equipment": {"id": "P-201", "display_name": "Cooling Pump"},
                        "meta": {
                            "asset_type": "rotating_equipment",
                            "criticality_class": "A",
                            "iso_code": "PU",
                        },
                    },
                    {
                        "equipment": {
                            "id": "COMP-301",
                            "display_name": "Air Compressor",
                        },
                        "meta": {
                            "asset_type": "rotating_equipment",
                            "criticality_class": "A",
                            "iso_code": "CO",
                        },
                    },
                ],
            },
        )

        # Second page: 1 item (short page) → pagination stops here
        httpx_mock.add_response(
            method="GET",
            url=f"{self.BASE_URL}/assets?size=2&start=2",
            json={
                "meta": {"total": 3},
                "data": [
                    {
                        "equipment": {"id": "HEX-101", "display_name": "Heat Exchanger"},
                        "meta": {
                            "asset_type": "static_equipment",
                            "criticality_class": "B",
                            "iso_code": "HE",
                        },
                    },
                ],
            },
        )

        assets = await conn.read_assets()

        # All three assets were retrieved across both pages
        assert len(assets) == 3
        assert {a.id for a in assets} == {"P-201", "COMP-301", "HEX-101"}

        # Field mapping extracted nested values correctly
        pump = next(a for a in assets if a.id == "P-201")
        assert pump.name == "Cooling Pump"
        assert pump.equipment_class_code == "PU"
        assert pump.criticality.value == "A"

        hex_unit = next(a for a in assets if a.id == "HEX-101")
        assert hex_unit.equipment_class_code == "HE"
        assert hex_unit.criticality.value == "B"

        # Every HTTP request carried the expected Basic auth header
        expected_creds = base64.b64encode(b"svc:s3cret").decode("ascii")
        expected_auth = f"Basic {expected_creds}"
        for req in httpx_mock.get_requests():
            assert req.headers["Authorization"] == expected_auth

        await conn.disconnect()


# ---------------------------------------------------------------------------
# New REST methods: get, update, close, cancel work orders & maintenance plans
# ---------------------------------------------------------------------------


@pytest.fixture
def rest_connector_with_endpoints() -> GenericCmmsConnector:
    """REST-mode connector with all optional endpoints configured."""
    return GenericCmmsConnector(
        url=BASE_URL,
        api_key="test-key",
        endpoints={
            "get_work_order": {"path": "work_orders/{id}", "method": "GET"},
            "update_work_order": {"path": "work_orders/{id}", "method": "PATCH"},
            "read_maintenance_plans": {"path": "maintenance_plans"},
        },
    )


@pytest.fixture
def rest_connector_without_get() -> GenericCmmsConnector:
    """REST-mode connector whose update returns the PATCH response (no re-read)."""
    return GenericCmmsConnector(
        url=BASE_URL,
        api_key="test-key",
        endpoints={"update_work_order": {"path": "work_orders/{id}"}},
    )


class TestRestGetWorkOrder:
    """REST get_work_order exercises GET /work_orders/{id}."""

    @pytest.mark.asyncio
    async def test_get_work_order_found(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={
                "id": "WO-001",
                "type": "corrective",
                "priority": "high",
                "asset_id": "P-201",
                "description": "Bearing replacement",
            },
        )
        wo = await conn.get_work_order("WO-001")
        assert wo is not None
        assert wo.id == "WO-001"

    @pytest.mark.asyncio
    async def test_get_work_order_not_found(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/NONEXISTENT",
            status_code=404,
        )
        wo = await conn.get_work_order("NONEXISTENT")
        assert wo is None

    @pytest.mark.asyncio
    async def test_unknown_asset_is_none_not_an_http_error(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        """A 404 on /assets/{id} means "no such asset", as for work orders."""
        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/assets/NOPE", status_code=404)

        assert await rest_connector.get_asset("NOPE") is None

    @pytest.mark.asyncio
    async def test_ids_are_sent_as_one_path_segment(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        """An ID cannot climb out of the configured path or add a query."""
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/..%2Fusers%2F1%3Frole%3Dadmin",
            status_code=404,
        )
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/assets/a%2Fb%23c", status_code=404)

        assert await conn.get_work_order("../users/1?role=admin") is None
        assert await conn.get_asset("a/b#c") is None

    @pytest.mark.asyncio
    async def test_dot_segment_ids_are_refused(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)

        with pytest.raises(ConnectorError, match="Invalid record ID"):
            await conn.get_work_order("..")


class TestRestUpdateWorkOrder:
    """REST update_work_order exercises PATCH /work_orders/{id}."""

    @pytest.mark.asyncio
    async def test_update_work_order_sends_patch(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        # PATCH response
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={"ok": True},
        )
        # Re-fetch after update
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={
                "id": "WO-001",
                "type": "corrective",
                "priority": "high",
                "asset_id": "P-201",
                "description": "Updated",
                "status": "assigned",
            },
        )
        updated = await conn.update_work_order(
            "WO-001", status=WorkOrderStatus.ASSIGNED, description="Updated"
        )
        assert updated.id == "WO-001"
        # Verify PATCH body contained the right fields
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        import json

        body = json.loads(patch_req.content)
        assert body["status"] == "assigned"
        assert body["description"] == "Updated"

    @pytest.mark.asyncio
    async def test_update_work_order_with_field_map(self, httpx_mock) -> None:
        """When field_map is configured, outgoing keys are remapped."""
        conn = GenericCmmsConnector(
            url=BASE_URL,
            api_key="test-key",
            endpoints={
                "get_work_order": {"path": "work_orders/{id}", "method": "GET"},
                "update_work_order": {
                    "path": "work_orders/{id}",
                    "method": "PATCH",
                    "field_map": {
                        "status": "wo_status",
                        "description": "wo_desc",
                    },
                },
            },
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={"ok": True},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={
                "id": "WO-001",
                "type": "corrective",
                "asset_id": "P-201",
                "description": "Mapped",
            },
        )
        await conn.update_work_order(
            "WO-001", status=WorkOrderStatus.ASSIGNED, description="Mapped"
        )
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        import json

        body = json.loads(patch_req.content)
        assert "wo_status" in body
        assert "wo_desc" in body
        assert "status" not in body

    @pytest.mark.asyncio
    async def test_update_without_get_endpoint_returns_the_patch_response(
        self, httpx_mock, rest_connector_without_get: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_without_get
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={"id": "WO-001", "type": "corrective", "asset_id": "P-201", "status": "assigned"},
        )

        updated = await conn.update_work_order("WO-001", status=WorkOrderStatus.ASSIGNED)

        assert updated.status == WorkOrderStatus.ASSIGNED
        assert updated.asset_id == "P-201"

    @pytest.mark.asyncio
    async def test_update_without_get_endpoint_or_response_body(
        self, httpx_mock, rest_connector_without_get: GenericCmmsConnector
    ) -> None:
        """An empty PATCH response yields a minimal work order with the new status."""
        conn = rest_connector_without_get
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="PATCH", url=f"{BASE_URL}/work_orders/WO-001", status_code=204
        )

        updated = await conn.update_work_order("WO-001", status=WorkOrderStatus.ASSIGNED)

        assert updated.id == "WO-001"
        assert updated.status == WorkOrderStatus.ASSIGNED


class TestRestCloseAndCancelWorkOrder:
    """close_work_order and cancel_work_order delegate to update."""

    @pytest.mark.asyncio
    async def test_close_delegates_to_update(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={"ok": True},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={
                "id": "WO-001",
                "type": "corrective",
                "asset_id": "P-201",
                "status": "closed",
            },
        )
        result = await conn.close_work_order("WO-001")
        assert result.id == "WO-001"
        # Verify the PATCH sent status=closed
        patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
        import json

        body = json.loads(patch_req.content)
        assert body["status"] == "closed"

    @pytest.mark.asyncio
    async def test_cancel_delegates_to_update(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="PATCH",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={"ok": True},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/WO-001",
            json={
                "id": "WO-001",
                "type": "corrective",
                "asset_id": "P-201",
                "status": "cancelled",
            },
        )
        result = await conn.cancel_work_order("WO-001")
        assert result.id == "WO-001"


class TestRestReadMaintenancePlans:
    """REST read_maintenance_plans exercises GET /maintenance_plans."""

    @pytest.mark.asyncio
    async def test_read_maintenance_plans(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/maintenance_plans",
            json=[
                {
                    "id": "MP-001",
                    "asset_id": "P-201",
                    "name": "Quarterly Bearing Inspection",
                    "interval": {"months": 3},
                    "tasks": ["Check vibration levels"],
                    "active": True,
                },
            ],
        )
        plans = await conn.read_maintenance_plans()
        assert len(plans) == 1
        assert plans[0].id == "MP-001"
        assert plans[0].interval.months == 3


class TestRestGracefulDegradation:
    """Unconfigured endpoints raise ConnectorError with actionable message."""

    @pytest.mark.asyncio
    async def test_get_work_order_not_configured(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        with pytest.raises(ConnectorError, match="not configured"):
            await rest_connector.get_work_order("WO-001")

    @pytest.mark.asyncio
    async def test_update_work_order_not_configured(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        with pytest.raises(ConnectorError, match="not configured"):
            await rest_connector.update_work_order("WO-001", description="Nope")

    @pytest.mark.asyncio
    async def test_read_maintenance_plans_not_configured(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        await _connect_with_health(httpx_mock, rest_connector)
        with pytest.raises(ConnectorError, match="not configured"):
            await rest_connector.read_maintenance_plans()


_WORK_ORDER = WorkOrder(id="WO-1", type=WorkOrderType.CORRECTIVE, asset_id="P-201")

# Every REST operation of rest_connector_with_endpoints: HTTP method, path
# under BASE_URL, the connector call, and the operation its errors name.
_OPERATIONS = [
    pytest.param("GET", "assets", lambda c: c.read_assets(), "read assets", id="read_assets"),
    pytest.param(
        "GET", "assets/P-201", lambda c: c.get_asset("P-201"), "get asset", id="get_asset"
    ),
    pytest.param(
        "GET",
        "work_orders",
        lambda c: c.read_work_orders(),
        "read work orders",
        id="read_work_orders",
    ),
    pytest.param(
        "POST",
        "work_orders",
        lambda c: c.create_work_order(_WORK_ORDER),
        "create work order",
        id="create_work_order",
    ),
    pytest.param(
        "GET",
        "work_orders/WO-1",
        lambda c: c.get_work_order("WO-1"),
        "get work order",
        id="get_work_order",
    ),
    pytest.param(
        "PATCH",
        "work_orders/WO-1",
        lambda c: c.update_work_order("WO-1", description="Re-checked"),
        "update work order",
        id="update_work_order",
    ),
    pytest.param(
        "GET",
        "maintenance_plans",
        lambda c: c.read_maintenance_plans(),
        "read maintenance plans",
        id="read_maintenance_plans",
    ),
]


class TestRestErrorMapping:
    """A failed REST call raises a ConnectorError subclass, never an httpx error.

    The message names the operation and the status or failure type, never the
    URL, and the httpx exception stays chained as ``__cause__``.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status", "expected"), _STATUS_FAILURES)
    @pytest.mark.parametrize(("method", "path", "call", "operation"), _OPERATIONS)
    async def test_error_status_is_mapped(
        self,
        httpx_mock,
        rest_connector_with_endpoints: GenericCmmsConnector,
        method: str,
        path: str,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
        operation: str,
        status: int,
        expected: type[ConnectorError],
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(method=method, url=f"{BASE_URL}/{path}", status_code=status)

        with pytest.raises(ConnectorError) as exc_info:
            await call(conn)

        assert type(exc_info.value) is expected
        assert str(exc_info.value) == f"CMMS {operation} failed: HTTP {status}"
        assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("exc_type", "expected", "outcome"), _TRANSPORT_FAILURES)
    @pytest.mark.parametrize(("method", "path", "call", "operation"), _OPERATIONS)
    async def test_transport_failure_is_mapped(
        self,
        httpx_mock,
        rest_connector_with_endpoints: GenericCmmsConnector,
        method: str,
        path: str,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
        operation: str,
        exc_type: type[httpx.TransportError],
        expected: type[ConnectorError],
        outcome: str,
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        url = f"{BASE_URL}/{path}"
        httpx_mock.add_exception(exc_type(f"simulated failure for {url}"), method=method, url=url)

        with pytest.raises(ConnectorError) as exc_info:
            await call(conn)

        assert type(exc_info.value) is expected
        assert str(exc_info.value) == f"CMMS {operation} {outcome}"
        assert isinstance(exc_info.value.__cause__, exc_type)

    @pytest.mark.asyncio
    async def test_failure_on_a_later_page_is_mapped(self, httpx_mock) -> None:
        conn = GenericCmmsConnector(
            url=BASE_URL, api_key="test-key", pagination=OffsetLimitPagination(page_size=1)
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/assets?limit=1&offset=0", json=[{"id": "P-201"}]
        )
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/assets?limit=1&offset=1", status_code=503
        )

        with pytest.raises(ConnectorError, match=r"^CMMS read assets failed: HTTP 503$"):
            await conn.read_assets()

    @pytest.mark.asyncio
    async def test_mcp_tool_reports_the_failure_as_an_error_entry(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        """MCP tools that handle ConnectorError now catch REST failures too."""
        from unittest.mock import MagicMock

        from machina.mcp.tools import machina_list_assets
        from machina.runtime import MachinaRuntime

        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/assets", status_code=500)
        runtime = MachinaRuntime(connectors={"cmms": rest_connector}, primary_cmms_name="cmms")
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"runtime": runtime}

        assert await machina_list_assets(ctx) == [{"error": "CMMS read assets failed: HTTP 500"}]


# 2xx bodies resp.json() cannot decode, the error each one raises, and how the
# message describes it: an HTML page, as a proxy in front of the CMMS (an SSO
# login page, say) may answer, and JSON encoded in Latin-1 rather than UTF-8.
_NON_JSON_BODIES = [
    pytest.param(b"<html>login</html>", json.JSONDecodeError, "not JSON", id="html"),
    pytest.param(
        '{"name": "Pompa unità 2"}'.encode("latin-1"),
        UnicodeDecodeError,
        "not UTF-8",
        id="latin-1",
    ),
]

# The reads of _OPERATIONS. The CMMS may already have applied a write it
# answered with a 2xx, so a write's non-JSON response is reported differently.
_READ_OPERATIONS = [
    p for p in _OPERATIONS if p.id not in ("create_work_order", "update_work_order")
]

# The writes that decode their response, on rest_connector_without_get: create,
# and update, which returns the PATCH response when it does not re-read.
_DECODING_WRITES = [
    pytest.param(
        "POST",
        "work_orders",
        lambda c: c.create_work_order(_WORK_ORDER),
        "create work order",
        id="create_work_order",
    ),
    pytest.param(
        "PATCH",
        "work_orders/WO-1",
        lambda c: c.update_work_order("WO-1", description="Re-checked"),
        "update work order",
        id="update_work_order",
    ),
]

# An asset whose ``specs`` field holds JSON-encoded text that is not valid JSON.
_ASSET_WITH_BAD_SPECS = {"id": "P-201", "specs": "{not json"}


class TestRestNonJsonBody:
    """A 2xx response whose body cannot be decoded as JSON raises ConnectorError.

    The message names the operation and says the body is not JSON or not
    UTF-8, never the body or the URL; the decoding error stays chained as
    ``__cause__``.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("body", "decode_error", "problem"), _NON_JSON_BODIES)
    @pytest.mark.parametrize(("method", "path", "call", "operation"), _READ_OPERATIONS)
    async def test_non_json_body_is_mapped(
        self,
        httpx_mock,
        rest_connector_with_endpoints: GenericCmmsConnector,
        method: str,
        path: str,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
        operation: str,
        body: bytes,
        decode_error: type[ValueError],
        problem: str,
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(method=method, url=f"{BASE_URL}/{path}", content=body)

        with pytest.raises(ConnectorError) as exc_info:
            await call(conn)

        assert type(exc_info.value) is ConnectorError
        assert str(exc_info.value) == f"CMMS {operation} failed: response is {problem}"
        assert isinstance(exc_info.value.__cause__, decode_error)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("body", "decode_error", "problem"), _NON_JSON_BODIES)
    @pytest.mark.parametrize(("method", "path", "call", "operation"), _DECODING_WRITES)
    async def test_non_json_write_response_says_the_change_may_have_been_applied(
        self,
        httpx_mock,
        rest_connector_without_get: GenericCmmsConnector,
        method: str,
        path: str,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
        operation: str,
        body: bytes,
        decode_error: type[ValueError],
        problem: str,
    ) -> None:
        """The 2xx means the CMMS may have applied the write, so the error must
        not read as a failure that invites a retry, which could duplicate it."""
        conn = rest_connector_without_get
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(method=method, url=f"{BASE_URL}/{path}", content=body)

        with pytest.raises(ConnectorError) as exc_info:
            await call(conn)

        assert type(exc_info.value) is ConnectorError
        assert str(exc_info.value) == (
            f"CMMS {operation}: response is {problem}; the change may have been applied, "
            "so check the CMMS before retrying"
        )
        assert isinstance(exc_info.value.__cause__, decode_error)

    @pytest.mark.asyncio
    async def test_update_answered_with_2xx_is_logged_although_its_response_is_not_json(
        self, httpx_mock, rest_connector_without_get: GenericCmmsConnector
    ) -> None:
        """work_order_updated records a PATCH the CMMS answered with 2xx, as on
        the path that re-reads the work order."""
        conn = rest_connector_without_get
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="PATCH", url=f"{BASE_URL}/work_orders/WO-1", text="<html>login</html>"
        )

        with capture_logs() as logs, pytest.raises(ConnectorError):
            await conn.update_work_order("WO-1", description="Re-checked")

        assert any(
            e["event"] == "work_order_updated" and e["work_order_id"] == "WO-1" for e in logs
        )

    @pytest.mark.asyncio
    async def test_update_that_re_reads_does_not_decode_the_patch_response(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        """With get_work_order the update returns the re-read work order, so a
        PATCH response that is not JSON (a plain "OK", say) is not an error."""
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(method="PATCH", url=f"{BASE_URL}/work_orders/WO-1", text="OK")
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/WO-1",
            json={"id": "WO-1", "type": "corrective", "asset_id": "P-201"},
        )

        updated = await conn.update_work_order("WO-1", description="Re-checked")

        assert updated.id == "WO-1"
        assert updated.asset_id == "P-201"

    @pytest.mark.asyncio
    async def test_non_json_later_page_is_mapped(self, httpx_mock) -> None:
        conn = GenericCmmsConnector(
            url=BASE_URL, api_key="test-key", pagination=OffsetLimitPagination(page_size=1)
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/assets?limit=1&offset=0", json=[{"id": "P-201"}]
        )
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/assets?limit=1&offset=1", text="<html>login</html>"
        )

        with pytest.raises(
            ConnectorError, match=r"^CMMS read assets failed: response is not JSON$"
        ):
            await conn.read_assets()

    @pytest.mark.asyncio
    async def test_mcp_tool_reports_a_non_json_body_as_an_error_entry(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        from unittest.mock import MagicMock

        from machina.mcp.tools import machina_list_assets
        from machina.runtime import MachinaRuntime

        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/assets", text="<html>login</html>")
        runtime = MachinaRuntime(connectors={"cmms": rest_connector}, primary_cmms_name="cmms")
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"runtime": runtime}

        assert await machina_list_assets(ctx) == [
            {"error": "CMMS read assets failed: response is not JSON"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "body", "call"),
        [
            pytest.param(
                "assets", [_ASSET_WITH_BAD_SPECS], lambda c: c.read_assets(), id="read_assets"
            ),
            pytest.param(
                "assets/P-201",
                _ASSET_WITH_BAD_SPECS,
                lambda c: c.get_asset("P-201"),
                id="get_asset",
            ),
        ],
    )
    async def test_mapping_error_is_not_reported_as_a_non_json_body(
        self,
        httpx_mock,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        body: object,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
    ) -> None:
        """Records are mapped after the REST call, so a coercer's own decoding
        error (here a plugin coercer for a JSON-encoded field) is not reported
        as a non-JSON response: like any mapping error, it propagates as is."""
        monkeypatch.setitem(COERCER_REGISTRY, "json_text", lambda value, **_: json.loads(value))
        conn = GenericCmmsConnector(
            url=BASE_URL,
            api_key="test-key",
            yaml_mapping={
                "mapping": {
                    "asset": {
                        "endpoint": {"path": "assets"},
                        "fields": {
                            "id": {"source": "id"},
                            "metadata": {"specs": {"source": "specs", "coerce": "json_text"}},
                        },
                    }
                }
            },
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/{path}", json=body)

        with pytest.raises(json.JSONDecodeError):
            await call(conn)


# The REST writes; close and cancel go through update_work_order.
_WRITES = [
    pytest.param(lambda c: c.create_work_order(_WORK_ORDER), id="create_work_order"),
    pytest.param(
        lambda c: c.update_work_order("WO-1", description="Re-checked"), id="update_work_order"
    ),
    pytest.param(lambda c: c.close_work_order("WO-1"), id="close_work_order"),
    pytest.param(lambda c: c.cancel_work_order("WO-1"), id="cancel_work_order"),
]


class TestRestSandbox:
    """In sandbox mode a REST write is refused before any request is sent."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", _WRITES)
    async def test_write_sends_no_request(
        self,
        httpx_mock,
        rest_connector_with_endpoints: GenericCmmsConnector,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)

        set_sandbox_mode(True)
        try:
            with pytest.raises(SandboxViolationError):
                await call(conn)
        finally:
            set_sandbox_mode(False)

        # Only the health check went out.
        assert [r.method for r in httpx_mock.get_requests()] == ["GET"]
