"""Machina connector for UpKeep CMMS.

UpKeep is a cloud-based CMMS platform with a well-documented REST API
(``/api/v2/``). This connector reads assets, work orders, spare parts,
and preventive-maintenance plans, normalising all responses into Machina
domain entities.

Authentication uses a *Session-Token* header (API key issued from the
UpKeep web UI under **Account Settings → API Tokens**).

See also: https://developers.onupkeep.com/
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.cmms.auth import ApiKeyHeaderAuth
from machina.connectors.cmms.retry import request_with_retry
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import (
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)
from machina.exceptions import ConnectorAuthError, ConnectorError

logger = structlog.get_logger(__name__)


def _require_httpx() -> Any:
    """Import httpx lazily so the extra is truly optional."""
    try:
        import httpx
    except ImportError as exc:
        raise ConnectorError(
            "httpx is required for UpKeepConnector. "
            "Install with: pip install machina-ai[cmms-rest]"
        ) from exc
    return httpx


# ---------------------------------------------------------------------------
# UpKeep → Machina entity mapping helpers
# ---------------------------------------------------------------------------

_UPKEEP_PRIORITY_MAP: dict[int, Priority] = {
    # Per the UpKeep REST API v2, priority is a 0-indexed integer where
    # 0 is the lowest and 3 is the highest. See:
    # https://developers.onupkeep.com/#work-orders
    0: Priority.LOW,
    1: Priority.MEDIUM,
    2: Priority.HIGH,
    3: Priority.EMERGENCY,
}

_UPKEEP_STATUS_MAP: dict[str, WorkOrderStatus] = {
    "open": WorkOrderStatus.CREATED,
    "in progress": WorkOrderStatus.IN_PROGRESS,
    "on hold": WorkOrderStatus.ASSIGNED,
    "complete": WorkOrderStatus.COMPLETED,
}

_REVERSE_UPKEEP_STATUS: dict[WorkOrderStatus, str] = {
    WorkOrderStatus.CREATED: "open",
    WorkOrderStatus.ASSIGNED: "on hold",
    WorkOrderStatus.IN_PROGRESS: "in progress",
    WorkOrderStatus.COMPLETED: "complete",
    WorkOrderStatus.CLOSED: "complete",  # UpKeep has no distinct closed state
    WorkOrderStatus.CANCELLED: "on hold",  # UpKeep has no distinct cancelled state
}

_UPKEEP_CATEGORY_MAP: dict[str, AssetType] = {
    "Rotating Equipment": AssetType.ROTATING_EQUIPMENT,
    "Static Equipment": AssetType.STATIC_EQUIPMENT,
    "Instrument": AssetType.INSTRUMENT,
    "Electrical": AssetType.ELECTRICAL,
    "Piping": AssetType.PIPING,
    "HVAC": AssetType.HVAC,
    "Safety": AssetType.SAFETY,
}


def _parse_asset(data: dict[str, Any]) -> Asset:
    """Convert an UpKeep asset JSON object to a Machina :class:`Asset`."""
    category = str(data.get("category", ""))
    return Asset(
        id=str(data.get("id", "")),
        name=str(data.get("name", "")),
        type=_UPKEEP_CATEGORY_MAP.get(category, AssetType.ROTATING_EQUIPMENT),
        location=str(data.get("location", "")),
        manufacturer=str(data.get("make", "")),
        model=str(data.get("model", "")),
        serial_number=str(data.get("serialNumber", "")),
        criticality=Criticality.C,
        parent=data.get("parentAssetId"),
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "id",
                "name",
                "category",
                "location",
                "make",
                "model",
                "serialNumber",
                "parentAssetId",
            }
        },
    )


def _parse_work_order(data: dict[str, Any]) -> WorkOrder:
    """Convert an UpKeep work-order JSON object to a :class:`WorkOrder`."""
    raw_priority = data.get("priority", 1)
    priority = _UPKEEP_PRIORITY_MAP.get(int(raw_priority), Priority.MEDIUM)
    raw_status = str(data.get("status", "open")).lower()
    status = _UPKEEP_STATUS_MAP.get(raw_status, WorkOrderStatus.CREATED)
    wo_type = (
        WorkOrderType.PREVENTIVE
        if data.get("category") == "preventive"
        else WorkOrderType.CORRECTIVE
    )
    created = data.get("createdAt", "")
    updated = data.get("updatedAt", "")
    now = datetime.now(tz=UTC)
    return WorkOrder(
        id=str(data.get("id", "")),
        type=wo_type,
        priority=priority,
        status=status,
        asset_id=str(data.get("assetId") or data.get("asset", "")),
        description=str(data.get("title", "")),
        assigned_to=data.get("assignedToId"),
        created_at=_parse_datetime(created) if created else now,
        updated_at=_parse_datetime(updated) if updated else now,
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "id",
                "priority",
                "status",
                "category",
                "createdAt",
                "updatedAt",
                "assetId",
                "asset",
                "title",
                "assignedToId",
            }
        },
    )


def _parse_spare_part(data: dict[str, Any]) -> SparePart:
    """Convert an UpKeep part JSON object to a :class:`SparePart`.

    Prefers the physical part identifier (``partNumber`` then ``barcode``)
    as the SKU, falling back to UpKeep's internal record ``id`` only
    when neither is provided.
    """
    sku = str(data.get("partNumber") or data.get("barcode") or data.get("id") or "")
    return SparePart(
        sku=sku,
        name=str(data.get("name", "")),
        stock_quantity=int(data.get("quantity", 0)),
        unit_cost=float(data.get("cost", 0.0)),
        warehouse_location=str(data.get("area", "")),
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "id",
                "partNumber",
                "barcode",
                "name",
                "quantity",
                "cost",
                "area",
            }
        },
    )


def _parse_maintenance_plan(data: dict[str, Any]) -> MaintenancePlan:
    """Convert an UpKeep preventive-maintenance JSON to a :class:`MaintenancePlan`."""
    freq_days = int(data.get("frequencyDays", 0))
    return MaintenancePlan(
        id=str(data.get("id", "")),
        asset_id=str(data.get("assetId") or ""),
        name=str(data.get("title", "")),
        interval=Interval(days=freq_days),
        tasks=[str(t) for t in data.get("tasks", [])],
        active=data.get("status", "active") == "active",
    )


def _parse_datetime(value: str) -> datetime:
    """Parse an ISO-8601 date string into a timezone-aware datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class UpKeepConnector:
    """Connector for UpKeep CMMS.

    Provides integration with UpKeep's REST API v2 for reading and
    creating maintenance data.

    Args:
        url: UpKeep API base URL. Defaults to the production endpoint.
        api_key: API token from UpKeep (passed as ``Session-Token`` header).

    Example:
        ```python
        from machina.connectors import UpKeep

        connector = UpKeep(api_key="your-upkeep-api-token")
        await connector.connect()
        assets = await connector.read_assets()
        ```
    """

    capabilities: ClassVar[list[str]] = [
        "read_assets",
        "read_work_orders",
        "create_work_order",
        "update_work_order",
        "read_spare_parts",
        "read_maintenance_plans",
    ]

    _DEFAULT_URL: ClassVar[str] = "https://api.onupkeep.com"
    _PAGE_SIZE: ClassVar[int] = 100

    def __init__(
        self,
        *,
        url: str = "",
        api_key: str = "",
    ) -> None:
        self.url = (url or self._DEFAULT_URL).rstrip("/")
        self._auth = ApiKeyHeaderAuth(header_name="Session-Token", value=api_key)
        self._connected = False

    # ------------------------------------------------------------------
    # Connector lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Verify credentials against the UpKeep API.

        Raises:
            ConnectorAuthError: If the API key is missing or invalid.
            ConnectorError: If the API is unreachable.
        """
        if not self._auth.value:
            raise ConnectorAuthError("UpKeep API key is required")
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await request_with_retry(
                client,
                "GET",
                f"{self.url}/api/v2/users",
                headers=self._headers(),
                params={"limit": "1"},
            )
        if resp.status_code == 401:
            raise ConnectorAuthError("UpKeep API key is invalid")
        if resp.status_code != 200:
            raise ConnectorError(f"UpKeep health check failed: HTTP {resp.status_code}")
        self._connected = True
        logger.info("connected", connector="UpKeepConnector", url=self.url)

    async def disconnect(self) -> None:
        """Close the connector (no persistent connections to clean up)."""
        self._connected = False
        logger.info("disconnected", connector="UpKeepConnector")

    async def health_check(self) -> ConnectorHealth:
        """Return current health status."""
        if not self._connected:
            return ConnectorHealth(
                status=ConnectorStatus.UNHEALTHY,
                message="Not connected",
            )
        return ConnectorHealth(
            status=ConnectorStatus.HEALTHY,
            message="Connected",
            details={"url": self.url},
        )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def read_assets(self) -> list[Asset]:
        """Return all assets from UpKeep."""
        self._ensure_connected()
        raw = await self._paginated_get("/api/v2/assets")
        return [_parse_asset(item) for item in raw]

    async def get_asset(self, asset_id: str) -> Asset | None:
        """Look up a single asset by ID."""
        self._ensure_connected()
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client,
                "GET",
                f"{self.url}/api/v2/assets/{asset_id}",
                headers=self._headers(),
            )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ConnectorError(f"UpKeep GET asset failed: HTTP {resp.status_code}")
        body = resp.json()
        result = body.get("result", body)
        return _parse_asset(result)

    async def read_work_orders(
        self,
        *,
        asset_id: str = "",
        status: WorkOrderStatus | str = "",
    ) -> list[WorkOrder]:
        """Read work orders, optionally filtered by asset or status.

        Args:
            asset_id: Filter by UpKeep asset ID.
            status: Filter by status — accepts a :class:`WorkOrderStatus`
                enum (reverse-mapped to UpKeep's string) or a raw UpKeep
                status string for backward compatibility.
        """
        self._ensure_connected()
        params: dict[str, str] = {}
        if asset_id:
            params["asset"] = asset_id
        if status:
            upkeep_status = (
                _REVERSE_UPKEEP_STATUS.get(status, status.value)
                if isinstance(status, WorkOrderStatus)
                else status
            )
            params["status"] = upkeep_status
        raw = await self._paginated_get("/api/v2/work-orders", params=params)
        return [_parse_work_order(item) for item in raw]

    async def get_work_order(self, work_order_id: str) -> WorkOrder | None:
        """Look up a single work order by ID."""
        self._ensure_connected()
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client,
                "GET",
                f"{self.url}/api/v2/work-orders/{work_order_id}",
                headers=self._headers(),
            )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ConnectorError(f"UpKeep GET work order failed: HTTP {resp.status_code}")
        body = resp.json()
        result = body.get("result", body)
        return _parse_work_order(result)

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Create a new work order in UpKeep.

        Args:
            work_order: Machina :class:`WorkOrder` to create.

        Returns:
            The created work order with the server-assigned ID.
        """
        self._ensure_connected()
        httpx = _require_httpx()
        payload = {
            "title": work_order.description,
            "priority": _reverse_priority(work_order.priority),
            "assetId": work_order.asset_id,
            "category": (
                "preventive" if work_order.type == WorkOrderType.PREVENTIVE else "reactive"
            ),
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client,
                "POST",
                f"{self.url}/api/v2/work-orders",
                headers=self._headers(),
                json=payload,
            )
        if resp.status_code == 401:
            raise ConnectorAuthError("UpKeep API key is invalid")
        if resp.status_code not in (200, 201):
            raise ConnectorError(f"UpKeep create work order failed: HTTP {resp.status_code}")
        body = resp.json()
        result = body.get("result", body)
        logger.info(
            "work_order_created",
            connector="UpKeepConnector",
            work_order_id=result.get("id"),
            asset_id=work_order.asset_id,
        )
        return _parse_work_order(result)

    async def update_work_order(
        self,
        work_order_id: str,
        *,
        status: WorkOrderStatus | None = None,
        assigned_to: str | None = None,
        description: str | None = None,
    ) -> WorkOrder:
        """Update an existing work order in UpKeep via PATCH.

        Only non-``None`` fields are included in the PATCH payload.

        Args:
            work_order_id: UpKeep work order ID.
            status: New status (reverse-mapped to UpKeep string).
            assigned_to: New ``assignedToId``.
            description: New title.

        Returns:
            The updated work order.
        """
        self._ensure_connected()
        httpx = _require_httpx()
        payload: dict[str, Any] = {}
        if status is not None:
            payload["status"] = _reverse_status(status)
        if assigned_to is not None:
            payload["assignedToId"] = assigned_to
        if description is not None:
            payload["title"] = description
        if not payload:
            raise ConnectorError("update_work_order requires at least one field to update")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client,
                "PATCH",
                f"{self.url}/api/v2/work-orders/{work_order_id}",
                headers=self._headers(),
                json=payload,
            )
        if resp.status_code == 401:
            raise ConnectorAuthError("UpKeep API key is invalid")
        if resp.status_code not in (200, 204):
            raise ConnectorError(f"UpKeep update work order failed: HTTP {resp.status_code}")
        logger.info(
            "work_order_updated",
            connector="UpKeepConnector",
            operation="update_work_order",
            work_order_id=work_order_id,
        )
        updated = await self.get_work_order(work_order_id)
        if updated is None:
            raise ConnectorError(f"Work order {work_order_id} not found after update")
        return updated

    async def close_work_order(self, work_order_id: str) -> WorkOrder:
        """Transition a work order to CLOSED (maps to 'complete' in UpKeep)."""
        return await self.update_work_order(work_order_id, status=WorkOrderStatus.CLOSED)

    async def cancel_work_order(self, work_order_id: str) -> WorkOrder:
        """Transition a work order to CANCELLED (maps to 'on hold' in UpKeep)."""
        return await self.update_work_order(work_order_id, status=WorkOrderStatus.CANCELLED)

    async def read_spare_parts(
        self,
        *,
        sku: str = "",
    ) -> list[SparePart]:
        """Read spare parts (UpKeep calls them *parts*).

        Args:
            sku: Optional SKU / part number to filter the result in-memory
                after fetching. Matches the parsed :attr:`SparePart.sku`,
                which prefers the physical part identifier.

        Note:
            UpKeep's ``/api/v2/parts`` endpoint does not expose an
            asset-compatibility relation, so filtering by asset is not
            supported here. Use work-order line items to discover parts
            associated with a specific asset.
        """
        self._ensure_connected()
        raw = await self._paginated_get("/api/v2/parts")
        parts = [_parse_spare_part(item) for item in raw]
        if sku:
            parts = [p for p in parts if p.sku == sku]
        return parts

    async def read_maintenance_plans(self) -> list[MaintenancePlan]:
        """Read preventive-maintenance schedules from UpKeep."""
        self._ensure_connected()
        raw = await self._paginated_get("/api/v2/preventive-maintenance")
        return [_parse_maintenance_plan(item) for item in raw]

    async def read_maintenance_history(self, asset_id: str) -> list[WorkOrder]:
        """Return completed work orders for an asset."""
        wos = await self.read_work_orders(asset_id=asset_id, status="complete")
        return wos

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Build HTTP headers with auth and content-type."""
        return self._auth.apply({"Content-Type": "application/json"})

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")

    async def _paginated_get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages from an UpKeep list endpoint.

        UpKeep uses offset/limit pagination. The response body wraps
        results in ``{"results": [...], "totalCount": N}``.
        """
        httpx = _require_httpx()
        all_items: list[dict[str, Any]] = []
        offset = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                query: dict[str, str] = {
                    "limit": str(self._PAGE_SIZE),
                    "offset": str(offset),
                    **(params or {}),
                }
                resp = await request_with_retry(
                    client,
                    "GET",
                    f"{self.url}{path}",
                    headers=self._headers(),
                    params=query,
                )
                if resp.status_code == 401:
                    raise ConnectorAuthError("UpKeep API key is invalid")
                if resp.status_code != 200:
                    raise ConnectorError(f"UpKeep GET {path} failed: HTTP {resp.status_code}")
                body = resp.json()
                results = body.get("results", [])
                all_items.extend(results)
                if len(results) < self._PAGE_SIZE:
                    break
                offset += self._PAGE_SIZE
        logger.debug(
            "paginated_get",
            connector="UpKeepConnector",
            path=path,
            total=len(all_items),
        )
        return all_items


def _reverse_priority(priority: Priority) -> int:
    """Map Machina priority back to UpKeep integer (0-indexed, 0-3).

    UpKeep's REST API v2 uses a 0-indexed priority scale where 0 is the
    lowest and 3 is the highest. See
    https://developers.onupkeep.com/#work-orders.
    """
    return {
        Priority.LOW: 0,
        Priority.MEDIUM: 1,
        Priority.HIGH: 2,
        Priority.EMERGENCY: 3,
    }.get(priority, 1)


def _reverse_status(status: WorkOrderStatus) -> str:
    """Map Machina work-order status to UpKeep status string."""
    return _REVERSE_UPKEEP_STATUS.get(status, "open")
