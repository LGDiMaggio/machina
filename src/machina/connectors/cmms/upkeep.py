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
    1: Priority.LOW,
    2: Priority.MEDIUM,
    3: Priority.HIGH,
    4: Priority.EMERGENCY,
}

_UPKEEP_STATUS_MAP: dict[str, WorkOrderStatus] = {
    "open": WorkOrderStatus.CREATED,
    "in progress": WorkOrderStatus.IN_PROGRESS,
    "on hold": WorkOrderStatus.ASSIGNED,
    "complete": WorkOrderStatus.COMPLETED,
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
    raw_priority = data.get("priority", 2)
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
    """Convert an UpKeep part JSON object to a :class:`SparePart`."""
    return SparePart(
        sku=str(data.get("id", "")),
        name=str(data.get("name", "")),
        stock_quantity=int(data.get("quantity", 0)),
        unit_cost=float(data.get("cost", 0.0)),
        warehouse_location=str(data.get("area", "")),
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
            resp = await client.get(
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
            resp = await client.get(
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
        status: str = "",
    ) -> list[WorkOrder]:
        """Read work orders, optionally filtered by asset or status."""
        self._ensure_connected()
        params: dict[str, str] = {}
        if asset_id:
            params["asset"] = asset_id
        if status:
            params["status"] = status
        raw = await self._paginated_get("/api/v2/work-orders", params=params)
        return [_parse_work_order(item) for item in raw]

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
            resp = await client.post(
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

    async def read_spare_parts(
        self,
        *,
        asset_id: str = "",
        sku: str = "",
    ) -> list[SparePart]:
        """Read spare parts (UpKeep calls them *parts*)."""
        self._ensure_connected()
        raw = await self._paginated_get("/api/v2/parts")
        parts = [_parse_spare_part(item) for item in raw]
        if asset_id:
            parts = [p for p in parts if asset_id in p.compatible_assets]
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
                resp = await client.get(
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
    """Map Machina priority back to UpKeep integer (1-4)."""
    return {
        Priority.LOW: 1,
        Priority.MEDIUM: 2,
        Priority.HIGH: 3,
        Priority.EMERGENCY: 4,
    }.get(priority, 2)
