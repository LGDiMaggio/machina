"""GenericCmmsConnector — configurable REST adapter for any CMMS.

Works with any REST-based CMMS by mapping JSON responses to Machina
domain entities via a user-supplied schema mapping. Supports pluggable
authentication and pagination strategies, plus a local JSON data source
for demos and quickstarts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar

import jmespath
import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.cmms.auth import (
    ApiKeyHeaderAuth,
    BasicAuth,
    BearerAuth,
    NoAuth,
)
from machina.connectors.cmms.pagination import (
    CursorPagination,
    NoPagination,
    OffsetLimitPagination,
    PageNumberPagination,
)
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.spare_part import SparePart
from machina.domain.work_order import FailureImpact, Priority, WorkOrder, WorkOrderType
from machina.exceptions import ConnectorAuthError, ConnectorError

logger = structlog.get_logger(__name__)

# Plain unions (not Annotated) for use as runtime type annotations.
# The Annotated discriminated unions live in auth.py / pagination.py for
# pydantic serialization purposes.
_AuthUnion = BearerAuth | BasicAuth | ApiKeyHeaderAuth | NoAuth
_PaginationUnion = (
    NoPagination | OffsetLimitPagination | PageNumberPagination | CursorPagination
)


def _require_httpx() -> Any:
    """Import httpx lazily, raising a clear error if the extra is missing."""
    try:
        import httpx
    except ImportError as exc:
        raise ConnectorError(
            "httpx is required for REST mode. Install with: pip install machina-ai[cmms-rest]"
        ) from exc
    return httpx


class GenericCmmsConnector:
    """Configurable connector that wraps any REST-based CMMS.

    Can also be pointed at local JSON files for offline / demo usage.

    Args:
        url: Base URL of the CMMS REST API (optional for local mode).
        api_key: Bearer token for authentication. Legacy shortcut —
            equivalent to ``auth=BearerAuth(token=api_key)``. Ignored when
            ``auth`` is provided.
        data_dir: Path to a directory of JSON files used as a local data source.
        schema_mapping: Dictionary that maps CMMS field names to Machina
            field names. Supports two forms:

            * **Flat rename**: ``{"assets": {"asset_id": "id"}}`` renames
              top-level keys in each raw item.
            * **JMESPath extraction**: ``{"assets": {"_fields":
              {"id": "equipment.id", "name": "meta.display_name"}}}``
              extracts nested fields via JMESPath expressions.
        auth: Authentication strategy for REST mode. Defaults to deriving
            a :class:`BearerAuth` from ``api_key`` when the latter is set.
            Use :class:`NoAuth` explicitly for endpoints that require no
            credentials.
        pagination: Pagination strategy for list-style REST endpoints.
            Defaults to :class:`NoPagination` (single-shot GET) which
            preserves the behaviour of earlier versions.

    Example:
        ```python
        # Local mode with sample data
        cmms = GenericCmmsConnector(data_dir="sample_data/cmms")
        await cmms.connect()
        assets = await cmms.read_assets()

        # REST mode, legacy single-key auth
        cmms = GenericCmmsConnector(
            url="https://cmms.example.com/api",
            api_key="...",
        )

        # REST mode, modern CMMS with Basic auth, offset/limit pagination
        # and nested response format
        from machina.connectors.cmms import (
            BasicAuth,
            OffsetLimitPagination,
        )

        cmms = GenericCmmsConnector(
            url="https://cmms.example.com/api",
            auth=BasicAuth(username="svc", password="..."),
            pagination=OffsetLimitPagination(
                limit_param="size",
                offset_param="start",
                page_size=50,
                items_path="data",
            ),
            schema_mapping={
                "assets": {
                    "_fields": {
                        "id": "equipment.id",
                        "name": "equipment.display_name",
                        "criticality": "meta.criticality_class",
                    },
                },
            },
        )
        ```
    """

    capabilities: ClassVar[list[str]] = [
        "read_assets",
        "read_work_orders",
        "create_work_order",
        "read_spare_parts",
        "read_maintenance_history",
    ]

    def __init__(
        self,
        *,
        url: str = "",
        api_key: str = "",
        data_dir: str | Path = "",
        schema_mapping: dict[str, dict[str, Any]] | None = None,
        auth: _AuthUnion | None = None,
        pagination: _PaginationUnion | None = None,
    ) -> None:
        self.url = url
        self._api_key = api_key
        self._data_dir = Path(data_dir) if data_dir else None
        self._schema_mapping = schema_mapping or {}
        self._connected = False

        # Auth: explicit > api_key shortcut > None (raised at connect in REST mode)
        if auth is not None:
            self._auth: _AuthUnion | None = auth
        elif api_key:
            self._auth = BearerAuth(token=api_key)
        else:
            self._auth = None

        # Pagination: default NoPagination preserves legacy single-shot behaviour
        self._pagination: _PaginationUnion = pagination or NoPagination()

        # In-memory store for local mode
        self._assets: dict[str, Asset] = {}
        self._work_orders: list[WorkOrder] = []
        self._spare_parts: list[SparePart] = []

    # ------------------------------------------------------------------
    # Connector lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish connection or load local data files.

        Raises:
            ConnectorError: If neither ``url`` nor ``data_dir`` is provided.
            ConnectorAuthError: In REST mode, if no authentication strategy
                was supplied.
        """
        if self._data_dir and self._data_dir.exists():
            await self._load_local_data()
        elif self.url:
            await self._verify_rest_connection()
        else:
            msg = "Either 'url' or 'data_dir' must be provided"
            raise ConnectorError(msg)
        self._connected = True
        logger.info(
            "connected",
            connector="GenericCmmsConnector",
            mode="local" if self._data_dir else "rest",
        )

    async def disconnect(self) -> None:
        """Close the connection."""
        self._connected = False
        logger.info("disconnected", connector="GenericCmmsConnector")

    async def health_check(self) -> ConnectorHealth:
        """Check whether the connector is operational."""
        if not self._connected:
            return ConnectorHealth(
                status=ConnectorStatus.UNHEALTHY,
                message="Not connected",
            )
        return ConnectorHealth(
            status=ConnectorStatus.HEALTHY,
            message="Connected",
            details={"mode": "local" if self._data_dir else "rest"},
        )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def read_assets(self) -> list[Asset]:
        """Return all known assets."""
        self._ensure_connected()
        if self._data_dir:
            return list(self._assets.values())
        return await self._rest_read_assets()

    async def get_asset(self, asset_id: str) -> Asset | None:
        """Look up a single asset by ID."""
        self._ensure_connected()
        if self._data_dir:
            return self._assets.get(asset_id)
        assets = await self._rest_read_assets(asset_id=asset_id)
        return assets[0] if assets else None

    async def read_work_orders(
        self,
        *,
        asset_id: str = "",
        status: str = "",
    ) -> list[WorkOrder]:
        """Read work orders, optionally filtered by asset or status."""
        self._ensure_connected()
        if self._data_dir:
            results = self._work_orders
            if asset_id:
                results = [wo for wo in results if wo.asset_id == asset_id]
            if status:
                results = [wo for wo in results if wo.status.value == status]
            return results
        return await self._rest_read_work_orders(asset_id=asset_id, status=status)

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Create a new work order."""
        self._ensure_connected()
        if self._data_dir:
            self._work_orders.append(work_order)
            logger.info(
                "work_order_created",
                connector="GenericCmmsConnector",
                work_order_id=work_order.id,
                asset_id=work_order.asset_id,
            )
            return work_order
        return await self._rest_create_work_order(work_order)

    async def read_spare_parts(
        self,
        *,
        asset_id: str = "",
        sku: str = "",
    ) -> list[SparePart]:
        """Read spare parts, optionally filtered."""
        self._ensure_connected()
        results = self._spare_parts
        if asset_id:
            results = [sp for sp in results if asset_id in sp.compatible_assets]
        if sku:
            results = [sp for sp in results if sp.sku == sku]
        return results

    async def read_maintenance_history(
        self,
        asset_id: str,
    ) -> list[WorkOrder]:
        """Return completed work orders for an asset (maintenance history)."""
        self._ensure_connected()
        return [
            wo
            for wo in self._work_orders
            if wo.asset_id == asset_id and wo.status.value in ("completed", "closed")
        ]

    # ------------------------------------------------------------------
    # Internal: local data loading
    # ------------------------------------------------------------------

    async def _load_local_data(self) -> None:
        """Load assets, work orders, and spare parts from JSON files."""
        assert self._data_dir is not None
        assets_file = self._data_dir / "assets.json"
        work_orders_file = self._data_dir / "work_orders.json"
        spare_parts_file = self._data_dir / "spare_parts.json"

        if assets_file.exists():
            text = await asyncio.to_thread(assets_file.read_text, encoding="utf-8")
            raw = json.loads(text)
            for item in raw:
                mapped = self._apply_mapping("assets", item)
                asset = _parse_asset(mapped)
                self._assets[asset.id] = asset
            logger.debug(
                "loaded_assets",
                connector="GenericCmmsConnector",
                count=len(self._assets),
            )

        if work_orders_file.exists():
            text = await asyncio.to_thread(work_orders_file.read_text, encoding="utf-8")
            raw = json.loads(text)
            for item in raw:
                mapped = self._apply_mapping("work_orders", item)
                self._work_orders.append(_parse_work_order(mapped))
            logger.debug(
                "loaded_work_orders",
                connector="GenericCmmsConnector",
                count=len(self._work_orders),
            )

        if spare_parts_file.exists():
            text = await asyncio.to_thread(spare_parts_file.read_text, encoding="utf-8")
            raw = json.loads(text)
            for item in raw:
                mapped = self._apply_mapping("spare_parts", item)
                self._spare_parts.append(_parse_spare_part(mapped))
            logger.debug(
                "loaded_spare_parts",
                connector="GenericCmmsConnector",
                count=len(self._spare_parts),
            )

    def _apply_mapping(self, entity: str, data: dict[str, Any]) -> dict[str, Any]:
        """Apply schema mapping to a single raw item dict.

        Supports two mapping forms:

        1. **Flat rename (legacy)**: ``{"asset_id": "id"}`` renames top-level
           keys. Any field not mentioned in the mapping is preserved with
           its original key.
        2. **JMESPath extraction**: ``{"_fields": {"id": "equipment.id"}}``
           produces a new dict with only the listed fields, each extracted
           via a JMESPath expression. Missing paths are silently dropped.

        Selection between the two modes is based on the presence of the
        ``_fields`` sentinel key.
        """
        mapping = self._schema_mapping.get(entity, {})
        if not mapping:
            return data
        if "_fields" in mapping:
            fields_map = mapping["_fields"]
            if not isinstance(fields_map, dict):
                return data
            result: dict[str, Any] = {}
            for target_key, path in fields_map.items():
                value = jmespath.search(str(path), data)
                if value is not None:
                    result[str(target_key)] = value
            return result
        # Legacy flat rename mode
        return {mapping.get(k, k): v for k, v in data.items()}

    # ------------------------------------------------------------------
    # Internal: REST API
    # ------------------------------------------------------------------

    def _rest_headers(self) -> dict[str, str]:
        """Return the Authorization headers used for every REST call."""
        if self._auth is None:
            return {}
        return self._auth.apply({})

    def _rest_url(self, *parts: str) -> str:
        """Join the base URL and path parts, stripping trailing slashes."""
        return "/".join([self.url.rstrip("/"), *parts])

    async def _verify_rest_connection(self) -> None:
        """Verify that the REST API is reachable via a health check."""
        if self._auth is None:
            raise ConnectorAuthError(
                "API key or auth strategy is required for REST mode"
            )
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                self._rest_url("health"),
                headers=self._rest_headers(),
            )
        if resp.status_code != 200:
            raise ConnectorError(f"CMMS health check failed: HTTP {resp.status_code}")
        logger.info(
            "rest_connection_verified",
            connector="GenericCmmsConnector",
            url=self.url,
        )

    async def _rest_read_assets(self, *, asset_id: str = "") -> list[Asset]:
        """Fetch assets from the REST API.

        When ``asset_id`` is provided, GETs ``/assets/{id}`` and expects a
        single-object response (pagination bypassed). Otherwise GETs
        ``/assets`` and iterates via the configured pagination strategy.
        """
        httpx = _require_httpx()
        headers = self._rest_headers()
        if asset_id:
            url = self._rest_url("assets", asset_id)
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
            return [_parse_asset(self._apply_mapping("assets", resp.json()))]

        url = self._rest_url("assets")
        results: list[Asset] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            async for raw in self._pagination.iterate(client, url, headers):
                results.append(_parse_asset(self._apply_mapping("assets", raw)))
        return results

    async def _rest_read_work_orders(
        self,
        *,
        asset_id: str = "",
        status: str = "",
    ) -> list[WorkOrder]:
        """Fetch work orders from the REST API.

        Query params ``asset_id`` and ``status`` are forwarded to the server
        when set. Iteration uses the configured pagination strategy.
        """
        httpx = _require_httpx()
        headers = self._rest_headers()
        params: dict[str, str] = {}
        if asset_id:
            params["asset_id"] = asset_id
        if status:
            params["status"] = status

        url = self._rest_url("work_orders")
        results: list[WorkOrder] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            async for raw in self._pagination.iterate(client, url, headers, params=params):
                results.append(_parse_work_order(self._apply_mapping("work_orders", raw)))
        return results

    async def _rest_create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Submit a new work order to the REST API."""
        httpx = _require_httpx()
        headers = {**self._rest_headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self._rest_url("work_orders"),
                headers=headers,
                json=work_order.model_dump(mode="json"),
            )
            resp.raise_for_status()
        return _parse_work_order(self._apply_mapping("work_orders", resp.json()))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")


# ---------------------------------------------------------------------------
# Parsing helpers — convert raw dicts to domain entities
# ---------------------------------------------------------------------------


def _parse_asset(data: dict[str, Any]) -> Asset:
    """Parse a dict into an Asset, tolerating missing fields."""
    return Asset(
        id=str(data.get("id", "")),
        name=str(data.get("name", "")),
        type=AssetType(data["type"]) if "type" in data else AssetType.ROTATING_EQUIPMENT,
        location=str(data.get("location", "")),
        manufacturer=str(data.get("manufacturer", "")),
        model=str(data.get("model", "")),
        serial_number=str(data.get("serial_number", "")),
        criticality=Criticality(data["criticality"]) if "criticality" in data else Criticality.C,
        parent=data.get("parent"),
        children=data.get("children", []),
        failure_modes=data.get("failure_modes", []),
        metadata=data.get("metadata", {}),
        equipment_class_code=data.get("equipment_class_code"),
    )


def _parse_work_order(data: dict[str, Any]) -> WorkOrder:
    """Parse a dict into a WorkOrder."""
    return WorkOrder(
        id=str(data.get("id", "")),
        type=WorkOrderType(data["type"]) if "type" in data else WorkOrderType.CORRECTIVE,
        priority=Priority(data["priority"]) if "priority" in data else Priority.MEDIUM,
        asset_id=str(data.get("asset_id", "")),
        description=str(data.get("description", "")),
        failure_mode=data.get("failure_mode"),
        failure_impact=(
            FailureImpact(data["failure_impact"]) if "failure_impact" in data else None
        ),
        failure_cause=data.get("failure_cause"),
    )


def _parse_spare_part(data: dict[str, Any]) -> SparePart:
    """Parse a dict into a SparePart."""
    return SparePart(
        sku=str(data.get("sku", "")),
        name=str(data.get("name", "")),
        manufacturer=str(data.get("manufacturer", "")),
        compatible_assets=data.get("compatible_assets", []),
        stock_quantity=int(data.get("stock_quantity", 0)),
        reorder_point=int(data.get("reorder_point", 0)),
        lead_time_days=int(data.get("lead_time_days", 0)),
        unit_cost=float(data.get("unit_cost", 0.0)),
        warehouse_location=str(data.get("warehouse_location", "")),
    )
