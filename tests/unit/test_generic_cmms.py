"""Tests for the GenericCmmsConnector."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import ConnectorAuthError, ConnectorError


@pytest.fixture
def sample_data_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with sample CMMS data."""
    cmms_dir = tmp_path / "cmms"
    cmms_dir.mkdir()

    assets = [
        {
            "id": "P-201",
            "name": "Cooling Water Pump",
            "type": "rotating_equipment",
            "location": "Building A",
            "criticality": "A",
        },
        {
            "id": "COMP-301",
            "name": "Air Compressor",
            "type": "rotating_equipment",
            "location": "Building B",
            "criticality": "A",
        },
    ]
    (cmms_dir / "assets.json").write_text(json.dumps(assets))

    work_orders = [
        {
            "id": "WO-001",
            "type": "corrective",
            "priority": "high",
            "asset_id": "P-201",
            "description": "Bearing replacement",
            "failure_mode": "BEAR-WEAR-01",
        },
        {
            "id": "WO-002",
            "type": "preventive",
            "priority": "medium",
            "asset_id": "COMP-301",
            "description": "Filter replacement",
        },
    ]
    (cmms_dir / "work_orders.json").write_text(json.dumps(work_orders))

    spare_parts = [
        {
            "sku": "SKF-6310",
            "name": "Bearing 6310",
            "manufacturer": "SKF",
            "compatible_assets": ["P-201"],
            "stock_quantity": 4,
            "reorder_point": 2,
            "lead_time_days": 5,
            "unit_cost": 45.0,
            "warehouse_location": "W1",
        },
        {
            "sku": "FILTER-GA55",
            "name": "Air Filter GA55",
            "manufacturer": "Atlas Copco",
            "compatible_assets": ["COMP-301"],
            "stock_quantity": 2,
            "reorder_point": 1,
            "lead_time_days": 10,
            "unit_cost": 120.0,
            "warehouse_location": "W2",
        },
    ]
    (cmms_dir / "spare_parts.json").write_text(json.dumps(spare_parts))

    return cmms_dir


class TestGenericCmmsConnectorLocal:
    """Test GenericCmmsConnector in local data mode."""

    @pytest.mark.asyncio
    async def test_connect_with_data_dir(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        health = await conn.health_check()
        assert health.status.value == "healthy"
        await conn.disconnect()

    @pytest.mark.asyncio
    async def test_read_assets(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        assets = await conn.read_assets()
        assert len(assets) == 2
        assert assets[0].id == "P-201"
        assert assets[0].name == "Cooling Water Pump"

    @pytest.mark.asyncio
    async def test_get_asset(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        asset = await conn.get_asset("P-201")
        assert asset is not None
        assert asset.id == "P-201"

    @pytest.mark.asyncio
    async def test_get_missing_asset(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        asset = await conn.get_asset("NONEXISTENT")
        assert asset is None

    @pytest.mark.asyncio
    async def test_read_work_orders(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        wos = await conn.read_work_orders()
        assert len(wos) == 2

    @pytest.mark.asyncio
    async def test_read_work_orders_by_asset(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        wos = await conn.read_work_orders(asset_id="P-201")
        assert len(wos) == 1
        assert wos[0].asset_id == "P-201"

    @pytest.mark.asyncio
    async def test_read_work_orders_by_status(self, sample_data_dir: Path) -> None:
        """Filter work orders by status."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        # Default status is "created", filter for it
        wos = await conn.read_work_orders(status="created")
        assert len(wos) == 2  # all WOs default to created status
        # Filter for a status that doesn't exist
        wos = await conn.read_work_orders(status="completed")
        assert len(wos) == 0

    @pytest.mark.asyncio
    async def test_create_work_order(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        wo = WorkOrder(
            id="WO-NEW",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="P-201",
            description="New test WO",
        )
        result = await conn.create_work_order(wo)
        assert result.id == "WO-NEW"
        # Verify it's persisted in memory
        all_wos = await conn.read_work_orders()
        assert len(all_wos) == 3

    @pytest.mark.asyncio
    async def test_read_spare_parts(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        parts = await conn.read_spare_parts(asset_id="P-201")
        assert len(parts) == 1
        assert parts[0].sku == "SKF-6310"

    @pytest.mark.asyncio
    async def test_read_spare_parts_by_sku(self, sample_data_dir: Path) -> None:
        """Filter spare parts by SKU."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        parts = await conn.read_spare_parts(sku="FILTER-GA55")
        assert len(parts) == 1
        assert parts[0].name == "Air Filter GA55"

    @pytest.mark.asyncio
    async def test_read_spare_parts_no_match(self, sample_data_dir: Path) -> None:
        """Filter spare parts for nonexistent asset."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        parts = await conn.read_spare_parts(asset_id="NONEXISTENT")
        assert len(parts) == 0

    @pytest.mark.asyncio
    async def test_read_spare_parts_all(self, sample_data_dir: Path) -> None:
        """Read all spare parts without filters."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        parts = await conn.read_spare_parts()
        assert len(parts) == 2

    @pytest.mark.asyncio
    async def test_read_maintenance_history(self, sample_data_dir: Path) -> None:
        """Maintenance history returns completed/closed WOs."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        # Default WOs are in "created" status, so history should be empty
        history = await conn.read_maintenance_history("P-201")
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_read_maintenance_history_with_completed(self, sample_data_dir: Path) -> None:
        """Add a completed WO and verify it shows in maintenance history."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        from machina.domain.work_order import WorkOrderStatus

        wo = WorkOrder(
            id="WO-DONE",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="P-201",
            description="Completed bearing replacement",
        )
        # Transition through valid states to reach completed
        wo.transition_to(WorkOrderStatus.ASSIGNED)
        wo.transition_to(WorkOrderStatus.IN_PROGRESS)
        wo.transition_to(WorkOrderStatus.COMPLETED)
        await conn.create_work_order(wo)
        history = await conn.read_maintenance_history("P-201")
        assert len(history) == 1
        assert history[0].id == "WO-DONE"

    @pytest.mark.asyncio
    async def test_not_connected_raises(self) -> None:
        conn = GenericCmmsConnector(data_dir="/tmp/nonexistent")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.read_assets()

    @pytest.mark.asyncio
    async def test_connect_no_source_raises(self) -> None:
        conn = GenericCmmsConnector()
        with pytest.raises(ConnectorError, match="Either"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_schema_mapping(self, tmp_path: Path) -> None:
        """Test that schema mapping renames fields correctly."""
        cmms_dir = tmp_path / "mapped"
        cmms_dir.mkdir()
        assets = [{"equipment_id": "X-1", "equipment_name": "Mapped Asset"}]
        (cmms_dir / "assets.json").write_text(json.dumps(assets))

        mapping = {
            "assets": {
                "equipment_id": "id",
                "equipment_name": "name",
            },
        }
        conn = GenericCmmsConnector(data_dir=cmms_dir, schema_mapping=mapping)
        await conn.connect()
        result = await conn.read_assets()
        assert len(result) == 1
        assert result[0].id == "X-1"
        assert result[0].name == "Mapped Asset"

    def test_capabilities(self) -> None:
        conn = GenericCmmsConnector()
        assert "read_assets" in conn.capabilities
        assert "create_work_order" in conn.capabilities


class TestGenericCmmsConnectorRest:
    """Pre-connect validation for REST mode.

    The full REST path (real HTTP via httpx) is covered in
    ``tests/integration/test_generic_cmms_rest.py`` using ``pytest-httpx``.
    This class keeps only the tests that don't need a mock server.
    """

    @pytest.mark.asyncio
    async def test_rest_connect_no_api_key(self) -> None:
        """REST mode requires an API key — rejected before any HTTP call."""
        conn = GenericCmmsConnector(url="http://example.com/api")
        with pytest.raises(ConnectorAuthError, match="API key"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self) -> None:
        """health_check() returns UNHEALTHY before connect() is called."""
        conn = GenericCmmsConnector(url="http://example.com/api", api_key="key")
        health = await conn.health_check()
        assert health.status.value == "unhealthy"


class TestAuthStrategies:
    """Unit tests for the pluggable authentication strategies.

    These exercise header production directly (no HTTP). Full wiring
    through the connector lives in the integration suite.
    """

    def test_bearer_auth_produces_authorization_header(self) -> None:
        from machina.connectors.cmms import BearerAuth

        headers = BearerAuth(token="secret").apply({})
        assert headers == {"Authorization": "Bearer secret"}

    def test_bearer_auth_preserves_existing_headers(self) -> None:
        from machina.connectors.cmms import BearerAuth

        headers = BearerAuth(token="s").apply({"Accept": "application/json"})
        assert headers["Accept"] == "application/json"
        assert headers["Authorization"] == "Bearer s"

    def test_basic_auth_encodes_credentials(self) -> None:
        import base64

        from machina.connectors.cmms import BasicAuth

        headers = BasicAuth(username="alice", password="p4ss").apply({})
        expected = base64.b64encode(b"alice:p4ss").decode("ascii")
        assert headers == {"Authorization": f"Basic {expected}"}

    def test_api_key_header_auth_default_header(self) -> None:
        from machina.connectors.cmms import ApiKeyHeaderAuth

        headers = ApiKeyHeaderAuth(value="k-123").apply({})
        assert headers == {"X-API-Key": "k-123"}

    def test_api_key_header_auth_custom_header(self) -> None:
        from machina.connectors.cmms import ApiKeyHeaderAuth

        headers = ApiKeyHeaderAuth(header_name="api-token", value="t").apply({})
        assert headers == {"api-token": "t"}

    def test_no_auth_leaves_headers_unchanged(self) -> None:
        from machina.connectors.cmms import NoAuth

        headers = NoAuth().apply({"Accept": "application/json"})
        assert headers == {"Accept": "application/json"}

    def test_no_auth_returns_a_copy(self) -> None:
        """apply() should not mutate the caller's dict."""
        from machina.connectors.cmms import NoAuth

        original: dict[str, str] = {"Accept": "*/*"}
        result = NoAuth().apply(original)
        result["Accept"] = "mutated"
        assert original["Accept"] == "*/*"

    @pytest.mark.asyncio
    async def test_api_key_legacy_param_creates_bearer_auth(self) -> None:
        """Passing ``api_key`` remains equivalent to BearerAuth for back-compat."""
        conn = GenericCmmsConnector(url="http://example.com/api", api_key="legacy")
        assert conn._rest_headers() == {"Authorization": "Bearer legacy"}

    @pytest.mark.asyncio
    async def test_explicit_auth_wins_over_api_key(self) -> None:
        """When both ``auth`` and ``api_key`` are supplied, ``auth`` wins."""
        from machina.connectors.cmms import BasicAuth

        conn = GenericCmmsConnector(
            url="http://example.com/api",
            api_key="ignored",
            auth=BasicAuth(username="u", password="p"),
        )
        headers = conn._rest_headers()
        assert headers["Authorization"].startswith("Basic ")
        assert "Bearer" not in headers["Authorization"]

    @pytest.mark.asyncio
    async def test_no_auth_strategy_is_allowed_in_rest_mode(self) -> None:
        """Explicit NoAuth lets the connector skip the api_key requirement."""
        from machina.connectors.cmms import NoAuth

        conn = GenericCmmsConnector(url="http://example.com/api", auth=NoAuth())
        # Should not raise ConnectorAuthError — NoAuth is a valid strategy.
        assert conn._auth is not None
        assert conn._rest_headers() == {}

    @pytest.mark.asyncio
    async def test_rest_without_auth_still_raises_auth_error(self) -> None:
        """Legacy behaviour preserved: no api_key and no auth → raises."""
        conn = GenericCmmsConnector(url="http://example.com/api")
        with pytest.raises(ConnectorAuthError, match="API key"):
            await conn.connect()


class TestPaginationStrategies:
    """Unit tests for pagination strategies (exercised via fake clients).

    These tests avoid depending on httpx entirely by supplying a small
    mock client object that implements ``async get()``. Real HTTP wiring
    through ``GenericCmmsConnector`` is covered in the integration suite.
    """

    @pytest.mark.asyncio
    async def test_no_pagination_yields_list_response(self) -> None:
        from machina.connectors.cmms import NoPagination

        client = _FakeClient(
            responses=[
                _FakeResponse(
                    200, [{"id": "A"}, {"id": "B"}, {"id": "C"}]
                ),
            ]
        )
        strategy = NoPagination()
        items = [
            item async for item in strategy.iterate(client, "http://x/items", {})
        ]
        assert [item["id"] for item in items] == ["A", "B", "C"]
        # Single request, no pagination query params
        assert client.calls == [("http://x/items", {})]

    @pytest.mark.asyncio
    async def test_no_pagination_with_items_path_extracts_nested_list(self) -> None:
        from machina.connectors.cmms import NoPagination

        client = _FakeClient(
            responses=[
                _FakeResponse(
                    200,
                    {"meta": {"count": 2}, "data": [{"id": "A"}, {"id": "B"}]},
                ),
            ]
        )
        strategy = NoPagination(items_path="data")
        items = [item async for item in strategy.iterate(client, "http://x", {})]
        assert [item["id"] for item in items] == ["A", "B"]

    @pytest.mark.asyncio
    async def test_offset_limit_pagination_walks_pages(self) -> None:
        from machina.connectors.cmms import OffsetLimitPagination

        # Two full pages of 2 items each, then a short page of 1 → stop
        client = _FakeClient(
            responses=[
                _FakeResponse(200, [{"id": "A"}, {"id": "B"}]),
                _FakeResponse(200, [{"id": "C"}, {"id": "D"}]),
                _FakeResponse(200, [{"id": "E"}]),
            ]
        )
        strategy = OffsetLimitPagination(page_size=2)
        items = [item async for item in strategy.iterate(client, "http://x", {})]
        assert [item["id"] for item in items] == ["A", "B", "C", "D", "E"]
        # Inspect offset progression
        offsets = [call[1]["offset"] for call in client.calls]
        assert offsets == ["0", "2", "4"]

    @pytest.mark.asyncio
    async def test_offset_limit_pagination_stops_on_empty_page(self) -> None:
        from machina.connectors.cmms import OffsetLimitPagination

        client = _FakeClient(
            responses=[
                _FakeResponse(200, [{"id": "A"}, {"id": "B"}]),
                _FakeResponse(200, []),
            ]
        )
        strategy = OffsetLimitPagination(page_size=2)
        items = [item async for item in strategy.iterate(client, "http://x", {})]
        assert [item["id"] for item in items] == ["A", "B"]

    @pytest.mark.asyncio
    async def test_offset_limit_pagination_respects_base_params(self) -> None:
        """Base query params (e.g. filters) are preserved across pages."""
        from machina.connectors.cmms import OffsetLimitPagination

        client = _FakeClient(
            responses=[
                _FakeResponse(200, [{"id": "A"}]),  # short page → stop
            ]
        )
        strategy = OffsetLimitPagination(page_size=2)
        items = [
            item
            async for item in strategy.iterate(
                client, "http://x", {}, params={"status": "open"}
            )
        ]
        assert len(items) == 1
        # The filter param should be on every call
        assert client.calls[0][1]["status"] == "open"

    @pytest.mark.asyncio
    async def test_offset_limit_pagination_custom_param_names(self) -> None:
        from machina.connectors.cmms import OffsetLimitPagination

        client = _FakeClient(
            responses=[_FakeResponse(200, [{"id": "A"}])],
        )
        strategy = OffsetLimitPagination(
            limit_param="size", offset_param="start", page_size=10
        )
        _ = [item async for item in strategy.iterate(client, "http://x", {})]
        assert "size" in client.calls[0][1]
        assert "start" in client.calls[0][1]

    @pytest.mark.asyncio
    async def test_page_number_pagination_walks_pages(self) -> None:
        from machina.connectors.cmms import PageNumberPagination

        client = _FakeClient(
            responses=[
                _FakeResponse(200, [{"id": "A"}, {"id": "B"}]),
                _FakeResponse(200, [{"id": "C"}]),  # short → stop
            ]
        )
        strategy = PageNumberPagination(page_size=2)
        items = [item async for item in strategy.iterate(client, "http://x", {})]
        assert [item["id"] for item in items] == ["A", "B", "C"]
        pages = [call[1]["page"] for call in client.calls]
        assert pages == ["1", "2"]

    @pytest.mark.asyncio
    async def test_page_number_pagination_respects_start_page_zero(self) -> None:
        from machina.connectors.cmms import PageNumberPagination

        client = _FakeClient(
            responses=[_FakeResponse(200, [{"id": "A"}])],  # short → stop
        )
        strategy = PageNumberPagination(page_size=5, start_page=0)
        _ = [item async for item in strategy.iterate(client, "http://x", {})]
        assert client.calls[0][1]["page"] == "0"

    @pytest.mark.asyncio
    async def test_cursor_pagination_follows_token_chain(self) -> None:
        from machina.connectors.cmms import CursorPagination

        client = _FakeClient(
            responses=[
                _FakeResponse(
                    200,
                    {"items": [{"id": "A"}, {"id": "B"}], "next_cursor": "cur-1"},
                ),
                _FakeResponse(
                    200,
                    {"items": [{"id": "C"}], "next_cursor": None},
                ),
            ]
        )
        strategy = CursorPagination()
        items = [item async for item in strategy.iterate(client, "http://x", {})]
        assert [item["id"] for item in items] == ["A", "B", "C"]
        # First call has no cursor; second call carries "cur-1"
        assert "cursor" not in client.calls[0][1]
        assert client.calls[1][1]["cursor"] == "cur-1"

    @pytest.mark.asyncio
    async def test_cursor_pagination_stops_on_missing_cursor(self) -> None:
        from machina.connectors.cmms import CursorPagination

        client = _FakeClient(
            responses=[
                _FakeResponse(200, {"items": [{"id": "A"}]}),  # no cursor → stop
            ]
        )
        strategy = CursorPagination()
        items = [item async for item in strategy.iterate(client, "http://x", {})]
        assert [item["id"] for item in items] == ["A"]
        assert len(client.calls) == 1


class TestNestedSchemaMapping:
    """Nested (_fields) schema-mapping mode with JMESPath extraction."""

    @pytest.mark.asyncio
    async def test_nested_mapping_extracts_fields_via_jmespath(
        self, tmp_path: Path
    ) -> None:
        """A JSON file with nested items can be flattened via _fields."""
        cmms_dir = tmp_path / "nested"
        cmms_dir.mkdir()
        assets = [
            {
                "equipment": {"id": "X-1", "display_name": "Pump"},
                "meta": {"criticality_class": "A"},
            },
        ]
        (cmms_dir / "assets.json").write_text(json.dumps(assets))

        mapping = {
            "assets": {
                "_fields": {
                    "id": "equipment.id",
                    "name": "equipment.display_name",
                    "criticality": "meta.criticality_class",
                },
            },
        }
        conn = GenericCmmsConnector(data_dir=cmms_dir, schema_mapping=mapping)
        await conn.connect()
        result = await conn.read_assets()
        assert len(result) == 1
        assert result[0].id == "X-1"
        assert result[0].name == "Pump"
        assert result[0].criticality.value == "A"

    @pytest.mark.asyncio
    async def test_nested_mapping_silently_drops_missing_paths(
        self, tmp_path: Path
    ) -> None:
        """Fields whose JMESPath yields None should simply be dropped."""
        cmms_dir = tmp_path / "partial"
        cmms_dir.mkdir()
        assets = [{"equipment": {"id": "X-1"}}]  # no name nested
        (cmms_dir / "assets.json").write_text(json.dumps(assets))

        mapping = {
            "assets": {
                "_fields": {
                    "id": "equipment.id",
                    "name": "equipment.display_name",  # missing
                },
            },
        }
        conn = GenericCmmsConnector(data_dir=cmms_dir, schema_mapping=mapping)
        await conn.connect()
        result = await conn.read_assets()
        assert len(result) == 1
        assert result[0].id == "X-1"
        # Missing name falls back to the Asset default empty string
        assert result[0].name == ""

    @pytest.mark.asyncio
    async def test_flat_mapping_still_works_unchanged(self, tmp_path: Path) -> None:
        """Legacy flat mapping is untouched by the new _fields support."""
        cmms_dir = tmp_path / "flat"
        cmms_dir.mkdir()
        assets = [{"equipment_id": "Y-1", "equipment_name": "Valve"}]
        (cmms_dir / "assets.json").write_text(json.dumps(assets))

        mapping = {"assets": {"equipment_id": "id", "equipment_name": "name"}}
        conn = GenericCmmsConnector(data_dir=cmms_dir, schema_mapping=mapping)
        await conn.connect()
        result = await conn.read_assets()
        assert result[0].id == "Y-1"
        assert result[0].name == "Valve"


# ---------------------------------------------------------------------------
# Helpers — minimal fakes for exercising pagination strategies without httpx
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Tiny stand-in for httpx.Response used by pagination tests."""

    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self._payload


class _FakeClient:
    """Tiny stand-in for httpx.AsyncClient.

    Records every ``get()`` call as a ``(url, params)`` tuple and returns
    the next response from the configured queue. Enough to exercise
    pagination strategy logic without touching the network or httpx itself.
    """

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self.calls.append((url, dict(params or {})))
        if not self._responses:
            raise AssertionError(f"Unexpected extra GET to {url}")
        return self._responses.pop(0)
