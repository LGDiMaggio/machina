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
from machina.connectors.cmms._http_errors import WRITE_NOT_SENT, WRITE_OUTCOME_UNKNOWN
from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.connectors.cmms.generic_coercers import COERCER_REGISTRY
from machina.connectors.cmms.pagination import NoPagination, OffsetLimitPagination
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
    ConnectorSchemaError,
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


# The failures every REST read maps onto the connector-error hierarchy: an
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

    @pytest.mark.asyncio
    async def test_connect_with_a_non_ascii_credential_raises_config_error(
        self, httpx_mock
    ) -> None:
        """httpx refuses the header before sending; the codec's message, which
        quotes the character, is not kept."""
        conn = GenericCmmsConnector(url=BASE_URL, api_key="clé-s3cret")
        with pytest.raises(ConnectorConfigError) as exc_info:
            await conn.connect()
        assert str(exc_info.value) == (
            "CMMS health check failed: a request header is not ASCII (check the credentials)"
        )
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__
        assert httpx_mock.get_requests() == []


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
            "read_spare_parts": {"path": "spare_parts"},
            "read_maintenance_history": {"path": "assets/{asset_id}/history"},
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
            url=f"{BASE_URL}/work_orders/wo1%2Fusers%3Frole%3Dadmin",
            status_code=404,
        )
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/assets/a%2Fb%23c", status_code=404)

        assert await conn.get_work_order("wo1/users?role=admin") is None
        assert await conn.get_asset("a/b#c") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("record_id", ["..", "../users/1"])
    async def test_dot_segment_ids_are_refused(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector, record_id: str
    ) -> None:
        """A proxy that decodes ``%2F`` would resolve ``../users/1`` into ``/users/1``."""
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)

        with pytest.raises(ConnectorError, match="Invalid record ID"):
            await conn.get_work_order(record_id)

    @pytest.mark.asyncio
    async def test_empty_asset_id_is_refused_not_listed(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        """An empty ID must not fall through to ``GET /assets`` and return its first row."""
        await _connect_with_health(httpx_mock, rest_connector)

        with pytest.raises(ConnectorError, match="Invalid record ID"):
            await rest_connector.get_asset("")


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
    async def test_update_work_order_id_stays_one_path_segment(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        """Interpolated as-is, this ID would PATCH ``/work_orders/wo1/assets``."""
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="PATCH", url=f"{BASE_URL}/work_orders/wo1%2Fassets", json={"ok": True}
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/work_orders/wo1%2Fassets",
            json={"id": "wo1/assets", "asset_id": "P-201", "description": "x"},
        )
        updated = await conn.update_work_order("wo1/assets", description="x")
        assert updated.id == "wo1/assets"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("record_id", ["..", "../users/1"])
    async def test_update_work_order_refuses_a_dot_segment_id_before_any_request(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector, record_id: str
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        sent = len(httpx_mock.get_requests())
        with pytest.raises(ConnectorError, match="Invalid record ID"):
            await conn.update_work_order(record_id, description="x")
        assert len(httpx_mock.get_requests()) == sent

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "read_back",
        [
            pytest.param({"status_code": 500}, id="error-status"),
            pytest.param({"status_code": 404}, id="not-found"),
            pytest.param({"text": "<html>login</html>"}, id="not-json"),
            pytest.param({"json": {"id": "WO-001", "type": "not-a-type"}}, id="unmappable"),
        ],
    )
    async def test_failed_read_back_after_an_accepted_update_leaves_the_outcome_unknown(
        self,
        httpx_mock,
        rest_connector_with_endpoints: GenericCmmsConnector,
        read_back: dict[str, object],
    ) -> None:
        """The CMMS answered the PATCH with 2xx, so a failed re-read must not
        read as a failed update that a caller would send again."""
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(method="PATCH", url=f"{BASE_URL}/work_orders/WO-001", json={})
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/work_orders/WO-001", **read_back)

        with capture_logs() as logs, pytest.raises(ConnectorError) as exc_info:
            await conn.update_work_order("WO-001", description="Re-checked")

        assert type(exc_info.value) is ConnectorError
        assert str(exc_info.value) == (
            "CMMS update work order was answered with a success status, but reading the "
            f"work order back failed. {WRITE_OUTCOME_UNKNOWN}"
        )
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", "PATCH", "GET"]
        events = [e["event"] for e in logs]
        assert "work_order_update_not_read_back" in events
        assert "rest_write_outcome_unknown" in events


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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("call", "operation"),
        [
            pytest.param(
                lambda c: c.read_spare_parts(asset_id="P-201"), "read_spare_parts", id="parts"
            ),
            pytest.param(
                lambda c: c.read_maintenance_history("P-201"),
                "read_maintenance_history",
                id="history",
            ),
        ],
    )
    async def test_unconfigured_read_is_refused_not_empty(
        self,
        httpx_mock,
        rest_connector: GenericCmmsConnector,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
        operation: str,
    ) -> None:
        """No silent empty list: the read fails, and sends no request."""
        await _connect_with_health(httpx_mock, rest_connector)
        with pytest.raises(ConnectorError) as exc_info:
            await call(rest_connector)
        assert str(exc_info.value) == (
            f"{operation} is not configured for this CMMS. "
            f"Add '{operation}' under the 'endpoints' parameter."
        )
        assert len(httpx_mock.get_requests()) == 1  # the health check


class TestRestReadSpareParts:
    """REST read_spare_parts reads the configured ``read_spare_parts`` endpoint."""

    @staticmethod
    def _connector(path: str = "spare_parts", **kwargs: object) -> GenericCmmsConnector:
        return GenericCmmsConnector(
            url=BASE_URL,
            api_key="test-key",
            endpoints={"read_spare_parts": {"path": path}},
            **kwargs,  # type: ignore[arg-type]
        )

    @pytest.mark.asyncio
    async def test_reads_the_configured_endpoint(self, httpx_mock) -> None:
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/spare_parts",
            json=[
                {
                    "sku": "BRG-6205",
                    "name": "Bearing 6205",
                    "compatible_assets": ["P-201"],
                    "stock_quantity": 4,
                    "reorder_point": 2,
                }
            ],
        )
        [part] = await conn.read_spare_parts()
        assert part.sku == "BRG-6205"
        assert part.stock_quantity == 4
        assert part.compatible_assets == ["P-201"]

    @pytest.mark.asyncio
    async def test_filters_are_sent_as_query_params(self, httpx_mock) -> None:
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/spare_parts?asset_id=P-201&sku=BRG-6205",
            json=[{"sku": "BRG-6205", "name": "Bearing 6205"}],
        )
        parts = await conn.read_spare_parts(asset_id="P-201", sku="BRG-6205")
        assert [p.sku for p in parts] == ["BRG-6205"]

    @pytest.mark.asyncio
    async def test_numeric_asset_filter_is_sent_as_text(self, httpx_mock) -> None:
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/spare_parts?asset_id=10442",
            json=[{"sku": "BRG-6205", "compatible_assets": ["10442"]}],
        )
        parts = await conn.read_spare_parts(asset_id=10442)  # type: ignore[arg-type]
        assert [p.sku for p in parts] == ["BRG-6205"]

    @pytest.mark.asyncio
    async def test_schema_mapping_is_applied(self, httpx_mock) -> None:
        conn = self._connector(
            path="inventory", schema_mapping={"spare_parts": {"part_no": "sku", "label": "name"}}
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/inventory",
            json=[{"part_no": "SEAL-KIT-32", "label": "Mechanical seal kit"}],
        )
        [part] = await conn.read_spare_parts()
        assert part.sku == "SEAL-KIT-32"
        assert part.name == "Mechanical seal kit"

    @pytest.mark.asyncio
    async def test_every_page_is_read(self, httpx_mock) -> None:
        conn = self._connector(pagination=OffsetLimitPagination(page_size=1))
        await _connect_with_health(httpx_mock, conn)
        for offset, body in enumerate([[{"sku": "BRG-6205"}], [{"sku": "SEAL-KIT-32"}], []]):
            httpx_mock.add_response(
                method="GET", url=f"{BASE_URL}/spare_parts?limit=1&offset={offset}", json=body
            )
        parts = await conn.read_spare_parts()
        assert [p.sku for p in parts] == ["BRG-6205", "SEAL-KIT-32"]

    @pytest.mark.asyncio
    async def test_records_contradicting_the_filters_are_dropped(self, httpx_mock) -> None:
        """A CMMS that ignores the filter params cannot mislabel parts.

        Parts listed as compatible only with other assets, or carrying another
        SKU, are dropped.
        """
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        catalog = [
            {"sku": "BRG-6205", "name": "Bearing 6205", "compatible_assets": ["P-201"]},
            {"sku": "FAN-160", "name": "Motor cooling fan", "compatible_assets": ["M-301"]},
            {"sku": "SEAL-KIT-32", "compatible_assets": ["M-301", "P-201"]},
        ]
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/spare_parts?asset_id=P-201", json=catalog
        )
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/spare_parts?sku=FAN-160", json=catalog
        )

        with capture_logs() as logs:
            by_asset = await conn.read_spare_parts(asset_id="P-201")
        assert [p.sku for p in by_asset] == ["BRG-6205", "SEAL-KIT-32"]
        [dropped] = [e for e in logs if e["event"] == "spare_parts_contradicting_filters_dropped"]
        assert (dropped["dropped"], dropped["unattributed"]) == (1, 0)
        by_sku = await conn.read_spare_parts(sku="FAN-160")
        assert [p.sku for p in by_sku] == ["FAN-160"]

    @pytest.mark.asyncio
    async def test_parts_listing_no_asset_are_kept_when_none_contradicts(self, httpx_mock) -> None:
        """The CMMS may filter on a relation (a bill of materials) it does not return."""
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/spare_parts?asset_id=P-201",
            json=[{"sku": "GREASE-EP2"}, {"sku": "BRG-6205", "compatible_assets": ["P-201"]}],
        )
        parts = await conn.read_spare_parts(asset_id="P-201")
        assert [p.sku for p in parts] == ["GREASE-EP2", "BRG-6205"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "catalog",
        [
            pytest.param(
                [{"sku": "FAN-160", "compatible_assets": ["M-301"]}], id="other-assets-only"
            ),
            pytest.param(
                [
                    {"sku": "BRG-6205", "compatible_assets": ["P-201"]},
                    {"sku": "FAN-160", "compatible_assets": ["M-301"]},
                    {"sku": "GREASE-EP2"},
                ],
                id="other-assets-and-unlisted",
            ),
        ],
    )
    async def test_an_answer_that_ignored_the_asset_is_refused(
        self, httpx_mock, catalog: list[dict[str, object]]
    ) -> None:
        """Parts listed for other assets show the CMMS did not filter by asset:
        parts listing no asset cannot be attributed to it, and an answer with
        none of its parts is not evidence that it has none."""
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/spare_parts?asset_id=P-201", json=catalog
        )
        with pytest.raises(ConnectorError) as exc_info:
            await conn.read_spare_parts(asset_id="P-201")
        assert str(exc_info.value) == (
            "CMMS read spare parts: the CMMS answered with parts listed for other assets, "
            "so the asset's parts could not be verified. Check that the 'read_spare_parts' "
            "endpoint filters by asset."
        )

    @pytest.mark.asyncio
    async def test_numeric_compatible_assets_match_as_text(self, httpx_mock) -> None:
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/spare_parts?asset_id=10442",
            json=[
                {"sku": "BRG-6205", "compatible_assets": [10442, 10443]},
                {"sku": "FAN-160", "compatible_assets": [20001]},
            ],
        )
        parts = await conn.read_spare_parts(asset_id="10442")
        assert [p.sku for p in parts] == ["BRG-6205"]
        assert parts[0].compatible_assets == ["10442", "10443"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "compatible_assets",
        [
            pytest.param("P-201", id="text-not-a-list"),
            pytest.param([1.5], id="float-item"),
            pytest.param([True], id="boolean-item"),
        ],
    )
    async def test_compatible_assets_that_are_not_ids_are_refused(
        self, httpx_mock, compatible_assets: object
    ) -> None:
        """Only text and whole numbers are read as asset IDs; anything else is
        left for the domain model to refuse, not turned into text."""
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/spare_parts",
            json=[{"sku": "BRG-6205", "compatible_assets": compatible_assets}],
        )
        with pytest.raises(ValueError, match="compatible_assets"):
            await conn.read_spare_parts()

    @pytest.mark.asyncio
    async def test_null_fields_read_as_defaults(self, httpx_mock) -> None:
        """REST payloads send ``null`` for unknown values; they read as defaults."""
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        fields = (
            "name",
            "manufacturer",
            "compatible_assets",
            "stock_quantity",
            "reorder_point",
            "lead_time_days",
            "unit_cost",
            "warehouse_location",
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/spare_parts",
            json=[{"sku": "BRG-6205", **dict.fromkeys(fields)}],
        )
        [part] = await conn.read_spare_parts()
        assert part.sku == "BRG-6205"
        assert part.name == ""
        assert part.compatible_assets == []
        assert part.stock_quantity == 0
        assert part.unit_cost == 0.0
        assert part.warehouse_location == ""

    @pytest.mark.asyncio
    async def test_a_null_sku_is_refused_not_read_as_none(self, httpx_mock) -> None:
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/spare_parts", json=[{"sku": None}])
        with pytest.raises(ValueError, match="sku"):
            await conn.read_spare_parts()

    @pytest.mark.asyncio
    async def test_a_filter_that_is_not_an_id_is_refused_before_any_request(
        self, httpx_mock
    ) -> None:
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        with pytest.raises(ConnectorError, match="the asset_id filter is not an ID"):
            await conn.read_spare_parts(asset_id=None)  # type: ignore[arg-type]
        assert len(httpx_mock.get_requests()) == 1  # the health check

    @pytest.mark.asyncio
    async def test_a_filter_that_cannot_be_encoded_is_refused_before_the_request(
        self, httpx_mock
    ) -> None:
        """An unpaired surrogate (from model or MCP-client JSON, say) fails while
        httpx encodes the query: nothing reaches the CMMS, and the message does
        not point at the credentials."""
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        with pytest.raises(ConnectorError) as exc_info:
            await conn.read_spare_parts(sku="BRG\ud800")
        assert type(exc_info.value) is ConnectorError
        assert str(exc_info.value) == (
            "CMMS read spare parts failed: a request value cannot be encoded as UTF-8"
        )
        assert len(httpx_mock.get_requests()) == 1  # the health check

    @pytest.mark.asyncio
    async def test_not_found_is_an_error_not_an_empty_list(self, httpx_mock) -> None:
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/spare_parts?asset_id=P-201", status_code=404
        )
        with pytest.raises(ConnectorError, match=r"^CMMS read spare parts failed: HTTP 404$"):
            await conn.read_spare_parts(asset_id="P-201")


class TestRestReadMaintenanceHistory:
    """REST read_maintenance_history reads the configured endpoint for one asset."""

    @staticmethod
    def _connector(
        path: str = "assets/{asset_id}/history", **kwargs: object
    ) -> GenericCmmsConnector:
        return GenericCmmsConnector(
            url=BASE_URL,
            api_key="test-key",
            endpoints={"read_maintenance_history": {"path": path}},
            **kwargs,  # type: ignore[arg-type]
        )

    @pytest.mark.asyncio
    async def test_asset_id_is_substituted_in_the_path(self, httpx_mock) -> None:
        """Records are returned as served, without status filtering."""
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/assets/P-201/history",
            json=[
                {
                    "id": "WO-3101",
                    "type": "corrective",
                    "status": "closed",
                    "asset_id": "P-201",
                    "description": "Replaced drive-end bearing",
                    "failure_mode": "BEAR-WEAR-01",
                },
                {"id": "WO-3102", "status": "in_progress", "asset_id": "P-201"},
            ],
        )
        history = await conn.read_maintenance_history("P-201")
        assert [wo.id for wo in history] == ["WO-3101", "WO-3102"]
        assert history[0].status == WorkOrderStatus.CLOSED
        assert history[0].failure_mode == "BEAR-WEAR-01"
        assert history[1].status == WorkOrderStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_asset_id_is_one_path_segment(self, httpx_mock) -> None:
        """'/' or '?' in an asset ID cannot reshape the URL."""
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/assets/LINE3%2FM-301%3Fx%3D1/history", json=[]
        )
        assert await conn.read_maintenance_history("LINE3/M-301?x=1") == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("asset_id", "message"),
        [
            pytest.param("", "requires an asset_id", id="empty"),
            pytest.param("  ", "requires an asset_id", id="blank"),
            pytest.param(".", "Invalid record ID", id="dot"),
            pytest.param("..", "Invalid record ID", id="dot-dot"),
            pytest.param("a/../b", "Invalid record ID", id="dot-dot-part"),
        ],
    )
    @pytest.mark.parametrize("path", ["assets/{asset_id}/history", "maintenance-history"])
    async def test_unusable_asset_id_is_refused_before_any_request(
        self, httpx_mock, path: str, asset_id: str, message: str
    ) -> None:
        """'..' would GET {url}/history; an empty ``?asset_id=`` reads as no filter."""
        conn = self._connector(path)
        await _connect_with_health(httpx_mock, conn)
        with pytest.raises(ConnectorError, match=message):
            await conn.read_maintenance_history(asset_id)
        assert len(httpx_mock.get_requests()) == 1  # the health check

    @pytest.mark.asyncio
    async def test_asset_id_is_substituted_in_the_query_string(self, httpx_mock) -> None:
        """``{asset_id}`` in the query lets the endpoint name its own filter param."""
        conn = self._connector("history?equipment={asset_id}")
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/history?equipment=LINE3%2FP-201", json=[]
        )
        assert await conn.read_maintenance_history("LINE3/P-201") == []

    @pytest.mark.asyncio
    async def test_asset_id_is_sent_as_a_query_param_without_a_placeholder(
        self, httpx_mock
    ) -> None:
        conn = self._connector("maintenance-history")
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/maintenance-history?asset_id=P-201",
            json=[{"id": "WO-3101", "status": "completed", "asset_id": "P-201"}],
        )
        history = await conn.read_maintenance_history("P-201")
        assert [wo.id for wo in history] == ["WO-3101"]

    @pytest.mark.asyncio
    async def test_numeric_asset_ids_match_as_text(self, httpx_mock) -> None:
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/assets/10442/history",
            json=[{"id": "WO-3101", "status": "completed", "asset_id": 10442}],
        )
        history = await conn.read_maintenance_history(10442)  # type: ignore[arg-type]
        assert [wo.asset_id for wo in history] == ["10442"]

    @pytest.mark.asyncio
    async def test_records_of_other_assets_are_dropped(self, httpx_mock) -> None:
        conn = self._connector("maintenance-history")
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/maintenance-history?asset_id=P-201",
            json=[
                {"id": "WO-3101", "status": "closed", "asset_id": "P-201"},
                {"id": "WO-3201", "status": "closed", "asset_id": "M-301"},
                {"id": "WO-3102", "status": "completed", "asset_id": "P-201"},
            ],
        )
        with capture_logs() as logs:
            history = await conn.read_maintenance_history("P-201")
        assert [wo.id for wo in history] == ["WO-3101", "WO-3102"]
        [dropped] = [e for e in logs if e["event"] == "maintenance_history_other_assets_dropped"]
        assert dropped["dropped"] == 1
        assert dropped["unattributed"] == 0
        assert dropped["asset_id"] == "P-201"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "records",
        [
            pytest.param(
                [{"id": "WO-3201", "asset_id": "M-301"}, {"id": "WO-3202", "asset_id": "M-302"}],
                id="other-assets-only",
            ),
            pytest.param(
                [
                    {"id": "WO-3101", "asset_id": "P-201"},
                    {"id": "WO-3201", "asset_id": "M-301"},
                    {"id": "WO-3301"},
                ],
                id="other-assets-and-unattributed",
            ),
        ],
    )
    async def test_an_answer_that_ignored_the_asset_is_refused(
        self, httpx_mock, records: list[dict[str, object]]
    ) -> None:
        """Records of other assets show the CMMS did not filter by asset: an
        answer with none of the asset's records is not an empty history, and
        records without an asset ID cannot be attributed to it."""
        conn = self._connector("maintenance-history")
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/maintenance-history?asset_id=P-201", json=records
        )
        with pytest.raises(ConnectorError) as exc_info:
            await conn.read_maintenance_history("P-201")
        assert str(exc_info.value) == (
            "CMMS read maintenance history: the CMMS answered with records of other assets, "
            "so the asset's history could not be verified. Check that the "
            "'read_maintenance_history' endpoint filters by asset."
        )
        assert "P-201" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_records_without_an_asset_id_are_kept_when_none_contradicts(
        self, httpx_mock
    ) -> None:
        """An endpoint scoped to the asset commonly omits the asset ID."""
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/assets/P-201/history",
            json=[
                {"id": "WO-3101", "status": "closed"},
                {"id": "WO-3102", "status": "closed", "asset_id": None},
                {"id": "WO-3103", "status": "closed", "asset_id": " "},
                {"id": "WO-3104", "status": "closed", "asset_id": "P-201"},
            ],
        )
        history = await conn.read_maintenance_history("P-201")
        assert [wo.id for wo in history] == ["WO-3101", "WO-3102", "WO-3103", "WO-3104"]
        # A null asset ID is not the asset 'None'.
        assert [wo.asset_id for wo in history] == ["", "", "", "P-201"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "url"),
        [
            pytest.param(
                "maintenance-history", "maintenance-history?asset_id=P-201", id="query-param"
            ),
            pytest.param(
                "history?equipment={asset_id}", "history?equipment=P-201", id="query-placeholder"
            ),
        ],
    )
    async def test_an_unattributed_answer_to_a_query_scoped_endpoint_is_refused(
        self, httpx_mock, path: str, url: str
    ) -> None:
        """A CMMS may ignore a query param and answer with every asset's work
        orders; if none names its asset, nothing shows that it filtered."""
        conn = self._connector(path)
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/{url}",
            json=[{"id": "WO-3101", "status": "in_progress"}, {"id": "WO-3102"}],
        )
        with capture_logs() as logs, pytest.raises(ConnectorError) as exc_info:
            await conn.read_maintenance_history("P-201")
        assert str(exc_info.value) == (
            "CMMS read maintenance history: no record names its asset, and the asset ID "
            "was sent as a query parameter, which the CMMS may have ignored, so the "
            "asset's history could not be verified. Map the records' asset ID, or put "
            "{asset_id} in the endpoint path."
        )
        assert any(
            e["event"] == "maintenance_history_unattributed" and e["unattributed"] == 2
            for e in logs
        )

    @pytest.mark.asyncio
    async def test_a_query_scoped_answer_with_a_record_of_the_asset_is_kept(
        self, httpx_mock
    ) -> None:
        conn = self._connector("maintenance-history")
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/maintenance-history?asset_id=P-201",
            json=[{"id": "WO-3101", "asset_id": "P-201"}, {"id": "WO-3102"}],
        )
        history = await conn.read_maintenance_history("P-201")
        assert [wo.id for wo in history] == ["WO-3101", "WO-3102"]

    @pytest.mark.asyncio
    async def test_an_empty_answer_is_an_empty_history(self, httpx_mock) -> None:
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/assets/P-201/history", json=[])
        assert await conn.read_maintenance_history("P-201") == []

    @pytest.mark.asyncio
    async def test_records_on_every_page_are_checked(self, httpx_mock) -> None:
        conn = self._connector(pagination=OffsetLimitPagination(page_size=1))
        await _connect_with_health(httpx_mock, conn)
        pages = [
            [{"id": "WO-3101", "asset_id": "P-201"}],
            [{"id": "WO-3201", "asset_id": "M-301"}],
        ]
        for offset, body in enumerate([*pages, []]):
            httpx_mock.add_response(
                method="GET",
                url=f"{BASE_URL}/assets/P-201/history?limit=1&offset={offset}",
                json=body,
            )
        history = await conn.read_maintenance_history("P-201")
        assert [wo.id for wo in history] == ["WO-3101"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [404, 500])
    async def test_a_failed_lookup_is_an_error_not_an_empty_history(
        self, httpx_mock, status: int
    ) -> None:
        conn = self._connector()
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/assets/P-201/history", status_code=status
        )
        with pytest.raises(
            ConnectorError, match=rf"^CMMS read maintenance history failed: HTTP {status}$"
        ):
            await conn.read_maintenance_history("P-201")


class TestRestEndpointQueryString:
    """A query string configured on an endpoint path survives the paginated GET.

    httpx replaces a URL's query string whenever ``params`` are passed, and
    every pagination strategy passes them, so the query travels as params.
    """

    @pytest.mark.asyncio
    async def test_query_string_in_endpoint_path_is_kept(self, httpx_mock) -> None:
        conn = GenericCmmsConnector(
            url=BASE_URL,
            api_key="test-key",
            endpoints={
                "read_spare_parts": {"path": "spare_parts?warehouse=W1"},
                "read_maintenance_history": {"path": "work_orders?status=closed"},
                "read_maintenance_plans": {"path": "maintenance_plans?active=true"},
            },
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/spare_parts?warehouse=W1&asset_id=P-201", json=[]
        )
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/work_orders?status=closed&asset_id=P-201", json=[]
        )
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/maintenance_plans?active=true", json=[]
        )
        assert await conn.read_spare_parts(asset_id="P-201") == []
        assert await conn.read_maintenance_history("P-201") == []
        assert await conn.read_maintenance_plans() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("endpoint", "path", "call"),
        [
            pytest.param(
                "read_spare_parts",
                "spare_parts?tag=a&tag=b",
                lambda c: c.read_spare_parts(),
                id="read_spare_parts",
            ),
            pytest.param(
                "read_maintenance_history",
                "history?status=closed&status=completed&equipment={asset_id}",
                lambda c: c.read_maintenance_history("P-201"),
                id="read_maintenance_history",
            ),
            pytest.param(
                "read_maintenance_plans",
                "maintenance_plans?site=A&site=B",
                lambda c: c.read_maintenance_plans(),
                id="read_maintenance_plans",
            ),
        ],
    )
    async def test_a_query_parameter_given_twice_is_refused_before_any_request(
        self,
        httpx_mock,
        endpoint: str,
        path: str,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
    ) -> None:
        """The params are a dict: all but one value would be dropped silently."""
        conn = GenericCmmsConnector(
            url=BASE_URL, api_key="test-key", endpoints={endpoint: {"path": path}}
        )
        await _connect_with_health(httpx_mock, conn)
        with pytest.raises(ConnectorConfigError) as exc_info:
            await call(conn)
        assert str(exc_info.value) == (
            f"The '{endpoint}' endpoint path gives a query parameter more than once, "
            "which cannot be sent; give each parameter once."
        )
        assert len(httpx_mock.get_requests()) == 1  # the health check


class TestRestMalformedListResponse:
    """A list read answered with 2xx but no list of records fails; it is not empty.

    An error envelope or a wrapped list read without its ``items_path`` would
    otherwise answer "this asset was never repaired" or "no parts".
    """

    _NOT_A_LIST = (
        "response is not a JSON list of records (set the pagination 'items_path' "
        "if the list is wrapped in an object)"
    )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"error": "session expired"}, id="error-envelope"),
            pytest.param({"data": [{"id": "WO-3101", "asset_id": "P-201"}]}, id="wrapped"),
        ],
    )
    @pytest.mark.parametrize(
        ("method", "path", "call", "operation"),
        [
            pytest.param("GET", "assets", lambda c: c.read_assets(), "read assets", id="assets"),
            pytest.param(
                "GET", "work_orders", lambda c: c.read_work_orders(), "read work orders", id="wos"
            ),
            pytest.param(
                "GET",
                "maintenance_plans",
                lambda c: c.read_maintenance_plans(),
                "read maintenance plans",
                id="plans",
            ),
            pytest.param(
                "GET",
                "spare_parts",
                lambda c: c.read_spare_parts(),
                "read spare parts",
                id="parts",
            ),
            pytest.param(
                "GET",
                "assets/P-201/history",
                lambda c: c.read_maintenance_history("P-201"),
                "read maintenance history",
                id="history",
            ),
        ],
    )
    async def test_body_that_is_not_a_list_is_an_error(
        self,
        httpx_mock,
        rest_connector_with_endpoints: GenericCmmsConnector,
        method: str,
        path: str,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
        operation: str,
        body: object,
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(method=method, url=f"{BASE_URL}/{path}", json=body)

        with pytest.raises(ConnectorSchemaError) as exc_info:
            await call(conn)

        assert str(exc_info.value) == f"CMMS {operation} failed: {self._NOT_A_LIST}"
        assert "session expired" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_items_path_that_misses_is_an_error_not_an_empty_history(
        self, httpx_mock
    ) -> None:
        conn = GenericCmmsConnector(
            url=BASE_URL,
            api_key="test-key",
            pagination=NoPagination(items_path="data"),
            endpoints={"read_maintenance_history": {"path": "assets/{asset_id}/history"}},
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/assets/P-201/history", json={"records": []}
        )
        with pytest.raises(ConnectorSchemaError) as exc_info:
            await conn.read_maintenance_history("P-201")
        assert str(exc_info.value) == (
            "CMMS read maintenance history failed: response has no list of records at the "
            "pagination 'items_path'"
        )

    @pytest.mark.asyncio
    async def test_a_record_that_is_not_an_object_is_an_error(self, httpx_mock) -> None:
        conn = GenericCmmsConnector(
            url=BASE_URL,
            api_key="test-key",
            endpoints={"read_spare_parts": {"path": "spare_parts"}},
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/spare_parts", json=["BRG-6205", "FAN-160"]
        )
        with pytest.raises(
            ConnectorSchemaError,
            match=r"^CMMS read spare parts failed: response lists a record that is not a JSON "
            r"object$",
        ):
            await conn.read_spare_parts()

    @pytest.mark.asyncio
    async def test_a_wrapped_empty_list_is_still_an_empty_history(self, httpx_mock) -> None:
        conn = GenericCmmsConnector(
            url=BASE_URL,
            api_key="test-key",
            pagination=NoPagination(items_path="data"),
            endpoints={"read_maintenance_history": {"path": "assets/{asset_id}/history"}},
        )
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/assets/P-201/history", json={"data": []}
        )
        assert await conn.read_maintenance_history("P-201") == []

    @pytest.mark.asyncio
    async def test_mcp_tool_reports_a_malformed_list_as_an_error_entry(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        from unittest.mock import MagicMock

        from machina.mcp.tools import machina_list_assets
        from machina.runtime import MachinaRuntime

        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/assets", json={"error": "x"})
        runtime = MachinaRuntime(connectors={"cmms": rest_connector}, primary_cmms_name="cmms")
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"runtime": runtime}

        assert await machina_list_assets(ctx) == [
            {"error": f"CMMS read assets failed: {self._NOT_A_LIST}"}
        ]


_WORK_ORDER = WorkOrder(id="WO-1", type=WorkOrderType.CORRECTIVE, asset_id="P-201")

# The reads of rest_connector_with_endpoints: HTTP method, path under
# BASE_URL, the connector call, and the operation its errors name.
_READ_OPERATIONS = [
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
        "GET",
        "work_orders/WO-1",
        lambda c: c.get_work_order("WO-1"),
        "get work order",
        id="get_work_order",
    ),
    pytest.param(
        "GET",
        "maintenance_plans",
        lambda c: c.read_maintenance_plans(),
        "read maintenance plans",
        id="read_maintenance_plans",
    ),
    pytest.param(
        "GET",
        "spare_parts",
        lambda c: c.read_spare_parts(),
        "read spare parts",
        id="read_spare_parts",
    ),
    pytest.param(
        "GET",
        "assets/P-201/history",
        lambda c: c.read_maintenance_history("P-201"),
        "read maintenance history",
        id="read_maintenance_history",
    ),
]

# The writes, which send their request once and report whether the CMMS may
# have applied them. Update fails here before its re-read.
_WRITE_OPERATIONS = [
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
    pytest.param(
        "PATCH",
        "work_orders/WO-1",
        lambda c: c.close_work_order("WO-1"),
        "update work order",
        id="close_work_order",
    ),
]


class TestRestErrorMapping:
    """A failed REST read raises a ConnectorError subclass, never an httpx error.

    The message names the operation and the status or failure type, never the
    URL, and the httpx exception stays chained as ``__cause__``.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status", "expected"), _STATUS_FAILURES)
    @pytest.mark.parametrize(("method", "path", "call", "operation"), _READ_OPERATIONS)
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
    @pytest.mark.parametrize(("method", "path", "call", "operation"), _READ_OPERATIONS)
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
        assert "cmms.example.com" not in str(exc_info.value)
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


class TestRestWriteOutcome:
    """A failed REST write says whether the CMMS may have applied it.

    A failure before the request was sent says no request was sent; a 4xx
    status is the CMMS refusing the write; anything else leaves the outcome
    unknown, and the message says so instead of reporting a failure that
    invites a duplicating retry. The write is sent once, never retried.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            pytest.param(401, ConnectorAuthError, id="401"),
            pytest.param(403, ConnectorAuthError, id="403"),
            pytest.param(404, ConnectorError, id="404"),
            pytest.param(409, ConnectorError, id="409"),
            pytest.param(422, ConnectorError, id="422"),
        ],
    )
    @pytest.mark.parametrize(("method", "path", "call", "operation"), _WRITE_OPERATIONS)
    async def test_refused_write_fails(
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
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", method]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            pytest.param(301, id="301"),
            # POST-redirect-GET answers an applied write with 303 See Other.
            pytest.param(303, id="303"),
            pytest.param(307, id="307"),
            pytest.param(500, id="500"),
            pytest.param(502, id="502"),
            pytest.param(503, id="503"),
            pytest.param(504, id="504"),
        ],
    )
    @pytest.mark.parametrize(("method", "path", "call", "operation"), _WRITE_OPERATIONS)
    async def test_status_other_than_a_refusal_leaves_the_outcome_unknown(
        self,
        httpx_mock,
        rest_connector_with_endpoints: GenericCmmsConnector,
        method: str,
        path: str,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
        operation: str,
        status: int,
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(method=method, url=f"{BASE_URL}/{path}", status_code=status)

        with capture_logs() as logs, pytest.raises(ConnectorError) as exc_info:
            await call(conn)

        assert type(exc_info.value) is ConnectorError
        assert str(exc_info.value) == (
            f"CMMS {operation} was answered with HTTP {status}. {WRITE_OUTCOME_UNKNOWN}"
        )
        assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", method]
        assert any(
            e["event"] == "rest_write_outcome_unknown" and e["operation"] == operation
            for e in logs
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc_type", "expected", "what_happened"),
        [
            pytest.param(
                httpx.ReadTimeout,
                ConnectorTimeoutError,
                "timed out after sending began",
                id="read-timeout",
            ),
            pytest.param(
                httpx.WriteTimeout,
                ConnectorTimeoutError,
                "timed out after sending began",
                id="write-timeout",
            ),
            pytest.param(
                httpx.ReadError,
                ConnectorError,
                "was interrupted after sending began: ReadError",
                id="read-error",
            ),
            pytest.param(
                httpx.RemoteProtocolError,
                ConnectorError,
                "was interrupted after sending began: RemoteProtocolError",
                id="remote-protocol-error",
            ),
        ],
    )
    @pytest.mark.parametrize(("method", "path", "call", "operation"), _WRITE_OPERATIONS)
    async def test_failure_after_sending_began_leaves_the_outcome_unknown(
        self,
        httpx_mock,
        rest_connector_with_endpoints: GenericCmmsConnector,
        method: str,
        path: str,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
        operation: str,
        exc_type: type[httpx.TransportError],
        expected: type[ConnectorError],
        what_happened: str,
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        url = f"{BASE_URL}/{path}"
        httpx_mock.add_exception(exc_type(f"simulated failure for {url}"), method=method, url=url)

        with pytest.raises(ConnectorError) as exc_info:
            await call(conn)

        assert type(exc_info.value) is expected
        assert str(exc_info.value) == (
            f"CMMS {operation} {what_happened}. {WRITE_OUTCOME_UNKNOWN}"
        )
        assert isinstance(exc_info.value.__cause__, exc_type)
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", method]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc_type", "expected", "what_happened"),
        [
            pytest.param(
                httpx.ConnectError,
                ConnectorError,
                "failed: ConnectError",
                id="connect-error",
            ),
            pytest.param(
                httpx.ConnectTimeout,
                ConnectorTimeoutError,
                "timed out",
                id="connect-timeout",
            ),
            pytest.param(
                httpx.PoolTimeout,
                ConnectorTimeoutError,
                "timed out",
                id="pool-timeout",
            ),
        ],
    )
    @pytest.mark.parametrize(("method", "path", "call", "operation"), _WRITE_OPERATIONS)
    async def test_failure_before_the_request_was_sent_says_no_request_was_sent(
        self,
        httpx_mock,
        rest_connector_with_endpoints: GenericCmmsConnector,
        method: str,
        path: str,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
        operation: str,
        exc_type: type[httpx.TransportError],
        expected: type[ConnectorError],
        what_happened: str,
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        url = f"{BASE_URL}/{path}"
        httpx_mock.add_exception(exc_type(f"simulated failure for {url}"), method=method, url=url)

        with capture_logs() as logs, pytest.raises(ConnectorError) as exc_info:
            await call(conn)

        assert type(exc_info.value) is expected
        assert str(exc_info.value) == f"CMMS {operation} {what_happened}. {WRITE_NOT_SENT}"
        assert isinstance(exc_info.value.__cause__, exc_type)
        assert not any(e["event"] == "rest_write_outcome_unknown" for e in logs)

    @pytest.mark.asyncio
    async def test_mcp_create_tool_raises_the_unknown_outcome_message(
        self, httpx_mock, rest_connector: GenericCmmsConnector
    ) -> None:
        """The MCP create tool lets the connector error through, and the MCP
        server reports its message to the client: the work order may exist."""
        from unittest.mock import MagicMock

        from machina.mcp.tools import machina_create_work_order
        from machina.runtime import MachinaRuntime

        await _connect_with_health(httpx_mock, rest_connector)
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/assets/P-201", json={"id": "P-201", "name": "Pump"}
        )
        httpx_mock.add_response(method="POST", url=f"{BASE_URL}/work_orders", status_code=502)
        runtime = MachinaRuntime(connectors={"cmms": rest_connector}, primary_cmms_name="cmms")
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"runtime": runtime}

        with pytest.raises(ConnectorError) as exc_info:
            await machina_create_work_order(
                ctx, asset_id="P-201", description="Seal leak", priority="high"
            )

        assert str(exc_info.value) == (
            f"CMMS create work order was answered with HTTP 502. {WRITE_OUTCOME_UNKNOWN}"
        )
        assert [r.method for r in httpx_mock.get_requests()].count("POST") == 1


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
    async def test_non_json_write_response_leaves_the_outcome_unknown(
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
            f"CMMS {operation} was answered with a success status, but the response is "
            f"{problem}. {WRITE_OUTCOME_UNKNOWN}"
        )
        assert isinstance(exc_info.value.__cause__, decode_error)
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", method]

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("parser", "method", "path", "call"),
        [
            pytest.param(
                "_parse_work_order",
                "GET",
                "work_orders",
                lambda c: c.read_work_orders(),
                id="read_work_orders",
            ),
            pytest.param(
                "_parse_work_order",
                "GET",
                "assets/P-201/history",
                lambda c: c.read_maintenance_history("P-201"),
                id="read_maintenance_history",
            ),
            pytest.param(
                "_parse_spare_part",
                "GET",
                "spare_parts",
                lambda c: c.read_spare_parts(),
                id="read_spare_parts",
            ),
            pytest.param(
                "_parse_maintenance_plan",
                "GET",
                "maintenance_plans",
                lambda c: c.read_maintenance_plans(),
                id="read_maintenance_plans",
            ),
        ],
    )
    async def test_parse_error_of_a_read_is_not_reported_as_a_failed_call(
        self,
        httpx_mock,
        monkeypatch: pytest.MonkeyPatch,
        rest_connector_with_endpoints: GenericCmmsConnector,
        parser: str,
        method: str,
        path: str,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
    ) -> None:
        """A decoding error raised while parsing a decoded record is not the
        response's: it propagates as is, not as "response is not JSON"."""
        import machina.connectors.cmms.generic as generic_module

        error = json.JSONDecodeError("bad nested field", "{", 0)

        def broken_parser(data: dict[str, object]) -> object:
            raise error

        monkeypatch.setattr(generic_module, parser, broken_parser)
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        record = {"id": "WO-1", "sku": "BRG-6205", "asset_id": "P-201"}
        httpx_mock.add_response(method=method, url=f"{BASE_URL}/{path}", json=[record])

        with pytest.raises(json.JSONDecodeError) as exc_info:
            await call(conn)

        assert exc_info.value is error

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("connector", "method", "path", "call", "operation"),
        [
            pytest.param(
                "rest_connector",
                "POST",
                "work_orders",
                lambda c: c.create_work_order(_WORK_ORDER),
                "create work order",
                id="create_work_order",
            ),
            pytest.param(
                "rest_connector_without_get",
                "PATCH",
                "work_orders/WO-1",
                lambda c: c.update_work_order("WO-1", description="Re-checked"),
                "update work order",
                id="update_work_order",
            ),
        ],
    )
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"id": "WO-1", "status": "open"}, id="unknown-status"),
            pytest.param({"id": "WO-1", "type": "inspection"}, id="unknown-type"),
            pytest.param(["not", "a", "record"], id="not-a-record"),
        ],
    )
    async def test_unmappable_response_of_an_accepted_write_leaves_the_outcome_unknown(
        self,
        httpx_mock,
        request: pytest.FixtureRequest,
        connector: str,
        method: str,
        path: str,
        call: Callable[[GenericCmmsConnector], Awaitable[object]],
        operation: str,
        body: object,
    ) -> None:
        """The CMMS answered the write with a success status: a response the
        mapping cannot read must not look like a failed write, which a caller
        would send again (a duplicate work order, for a create)."""
        conn: GenericCmmsConnector = request.getfixturevalue(connector)
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(method=method, url=f"{BASE_URL}/{path}", json=body)

        with capture_logs() as logs, pytest.raises(ConnectorError) as exc_info:
            await call(conn)

        assert type(exc_info.value) is ConnectorError
        assert str(exc_info.value) == (
            f"CMMS {operation} was answered with a success status, but the response could "
            f"not be mapped to a work order. {WRITE_OUTCOME_UNKNOWN}"
        )
        assert exc_info.value.__cause__ is not None
        assert [r.method for r in httpx_mock.get_requests()] == ["GET", method]
        [event] = [e for e in logs if e["event"] == "work_order_write_response_not_mapped"]
        assert event["work_order_id"] == "WO-1"
        assert event["operation"] == operation.replace(" ", "_")


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

    @pytest.mark.asyncio
    async def test_reads_still_work_in_sandbox_mode(
        self, httpx_mock, rest_connector_with_endpoints: GenericCmmsConnector
    ) -> None:
        conn = rest_connector_with_endpoints
        await _connect_with_health(httpx_mock, conn)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE_URL}/assets/P-201/history",
            json=[{"id": "WO-3101", "asset_id": "P-201"}],
        )
        httpx_mock.add_response(
            method="GET", url=f"{BASE_URL}/spare_parts", json=[{"sku": "BRG-6205"}]
        )

        set_sandbox_mode(True)
        try:
            history = await conn.read_maintenance_history("P-201")
            parts = await conn.read_spare_parts()
        finally:
            set_sandbox_mode(False)

        assert [wo.id for wo in history] == ["WO-3101"]
        assert [p.sku for p in parts] == ["BRG-6205"]
