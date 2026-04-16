"""Machina connector for IBM Maximo Manage (EAM).

IBM Maximo exposes maintenance data through OSLC-based REST APIs (also
called JSON APIs). This connector reads assets, work orders, spare parts
(inventory), and preventive-maintenance plans, normalising all responses
into Machina domain entities.

Authentication supports three schemes:

* **API key** — recommended for headless integrations (``apikey`` query
  parameter or ``APIKEY`` header, via :class:`ApiKeyHeaderAuth`).
* **Basic auth** — base64-encoded ``MAXAUTH`` header (on-prem native) or
  standard HTTP ``Authorization: Basic`` (LDAP).
* **Bearer token** — pre-obtained OAuth/LTPA token.

See also:
    https://developer.ibm.com/apis/catalog/maximo--maximo-manage-rest-api/Introduction
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.capabilities import Capability
from machina.connectors.cmms.auth import ApiKeyHeaderAuth, BasicAuth, BearerAuth
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

_AuthUnion = ApiKeyHeaderAuth | BasicAuth | BearerAuth


def _require_httpx() -> Any:
    """Import httpx lazily so the extra is truly optional."""
    try:
        import httpx
    except ImportError as exc:
        raise ConnectorError(
            "httpx is required for MaximoConnector. "
            "Install with: pip install machina-ai[cmms-rest]"
        ) from exc
    return httpx


# ---------------------------------------------------------------------------
# Maximo → Machina entity mapping helpers
# ---------------------------------------------------------------------------

_MAXIMO_PRIORITY_MAP: dict[int, Priority] = {
    1: Priority.EMERGENCY,
    2: Priority.HIGH,
    3: Priority.MEDIUM,
    4: Priority.LOW,
}

_MAXIMO_STATUS_MAP: dict[str, WorkOrderStatus] = {
    "WAPPR": WorkOrderStatus.CREATED,
    "APPR": WorkOrderStatus.ASSIGNED,
    "INPRG": WorkOrderStatus.IN_PROGRESS,
    "COMP": WorkOrderStatus.COMPLETED,
    "CLOSE": WorkOrderStatus.CLOSED,
    "CAN": WorkOrderStatus.CANCELLED,
}

_MAXIMO_WORKTYPE_MAP: dict[str, WorkOrderType] = {
    "CM": WorkOrderType.CORRECTIVE,
    "PM": WorkOrderType.PREVENTIVE,
    "CP": WorkOrderType.PREDICTIVE,
    "EV": WorkOrderType.IMPROVEMENT,
}

_REVERSE_MAXIMO_STATUS: dict[WorkOrderStatus, str] = {
    WorkOrderStatus.CREATED: "WAPPR",
    WorkOrderStatus.ASSIGNED: "APPR",
    WorkOrderStatus.IN_PROGRESS: "INPRG",
    WorkOrderStatus.COMPLETED: "COMP",
    WorkOrderStatus.CLOSED: "CLOSE",
    WorkOrderStatus.CANCELLED: "CAN",
}


def _resolve_asset_type(
    data: dict[str, Any],
    asset_type_map: dict[str, AssetType] | None,
) -> AssetType:
    """Resolve Machina ``AssetType`` from a Maximo MXASSET record.

    Maximo does not expose a canonical category field. When the caller
    supplies an ``asset_type_map`` keyed by ``classstructureid`` (or
    ``assettype``), the resolver performs an exact lookup with a
    fallback to :attr:`AssetType.ROTATING_EQUIPMENT`. Without a map,
    every asset collapses to ``ROTATING_EQUIPMENT`` — the historical
    behaviour.
    """
    if not asset_type_map:
        return AssetType.ROTATING_EQUIPMENT
    key = str(data.get("classstructureid") or data.get("assettype") or "")
    return asset_type_map.get(key, AssetType.ROTATING_EQUIPMENT)


def _parse_asset(
    data: dict[str, Any],
    asset_type_map: dict[str, AssetType] | None = None,
) -> Asset:
    """Convert a Maximo MXASSET JSON object to a Machina :class:`Asset`.

    Args:
        data: Parsed JSON record from the Maximo MXASSET object structure.
        asset_type_map: Optional mapping from Maximo ``classstructureid``
            (or ``assettype``) values to Machina :class:`AssetType`.
            When ``None`` all assets are classified as
            :attr:`AssetType.ROTATING_EQUIPMENT`.
    """
    return Asset(
        id=str(data.get("assetnum", "")),
        name=str(data.get("description", "")),
        type=_resolve_asset_type(data, asset_type_map),
        location=str(data.get("location", "")),
        manufacturer=str(data.get("manufacturer", "")),
        model=str(data.get("modelnum", "")),
        serial_number=str(data.get("serialnum", "")),
        criticality=_maximo_criticality(data.get("priority", 0)),
        parent=data.get("parent") or None,
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "assetnum",
                "description",
                "location",
                "manufacturer",
                "modelnum",
                "serialnum",
                "priority",
                "parent",
            }
        },
    )


def _maximo_criticality(priority_val: Any) -> Criticality:
    """Map Maximo numeric priority (1-3 → A/B/C)."""
    try:
        val = int(priority_val)
    except (TypeError, ValueError):
        return Criticality.C
    if val <= 1:
        return Criticality.A
    if val <= 2:
        return Criticality.B
    return Criticality.C


def _parse_work_order(data: dict[str, Any]) -> WorkOrder:
    """Convert a Maximo MXWO JSON object to a :class:`WorkOrder`."""
    raw_priority = data.get("wopriority", 3)
    try:
        prio_int = int(raw_priority)
    except (TypeError, ValueError):
        prio_int = 3
    priority = _MAXIMO_PRIORITY_MAP.get(prio_int, Priority.MEDIUM)

    raw_status = str(data.get("status", "")).upper()
    status = _MAXIMO_STATUS_MAP.get(raw_status, WorkOrderStatus.CREATED)

    raw_type = str(data.get("worktype", "")).upper()
    wo_type = _MAXIMO_WORKTYPE_MAP.get(raw_type, WorkOrderType.CORRECTIVE)

    now = datetime.now(tz=UTC)
    created = data.get("reportdate", data.get("changedate", ""))
    updated = data.get("changedate", "")

    return WorkOrder(
        id=str(data.get("wonum", "")),
        type=wo_type,
        priority=priority,
        status=status,
        asset_id=str(data.get("assetnum", "")),
        description=str(data.get("description", "")),
        assigned_to=data.get("lead") or data.get("assignedownergroup") or None,
        failure_mode=data.get("failurecode") or None,
        failure_cause=data.get("failureremark") or data.get("problemcode") or None,
        created_at=_parse_datetime(created) if created else now,
        updated_at=_parse_datetime(updated) if updated else now,
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "wonum",
                "wopriority",
                "status",
                "worktype",
                "assetnum",
                "description",
                "lead",
                "assignedownergroup",
                "failurecode",
                "failureremark",
                "problemcode",
                "reportdate",
                "changedate",
            }
        },
    )


def _parse_spare_part(data: dict[str, Any]) -> SparePart:
    """Convert a Maximo MXINVENTORY JSON object to a :class:`SparePart`."""
    return SparePart(
        sku=str(data.get("itemnum", "")),
        name=str(data.get("description", data.get("item", {}).get("description", ""))),
        stock_quantity=int(data.get("curbal", 0)),
        reorder_point=int(data.get("reorder", 0)),
        unit_cost=float(data.get("avgcost", data.get("lastcost", 0.0))),
        warehouse_location=str(data.get("location", "")),
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "itemnum",
                "description",
                "item",
                "curbal",
                "reorder",
                "avgcost",
                "lastcost",
                "location",
            }
        },
    )


def _parse_maintenance_plan(data: dict[str, Any]) -> MaintenancePlan:
    """Convert a Maximo MXPM JSON object to a :class:`MaintenancePlan`."""
    freq_days = int(data.get("frequency", 0))
    return MaintenancePlan(
        id=str(data.get("pmnum", "")),
        asset_id=str(data.get("assetnum", "")),
        name=str(data.get("description", "")),
        interval=Interval(days=freq_days),
        active=str(data.get("status", "")).upper() == "ACTIVE",
    )


def _parse_datetime(value: str) -> datetime:
    """Parse an ISO-8601 date string into a timezone-aware datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class MaximoConnector:
    """Connector for IBM Maximo Manage.

    Provides integration with Maximo's OSLC/JSON REST API for reading
    and creating maintenance data.

    Args:
        url: Base URL of the Maximo instance
            (e.g. ``https://maximo.example.com``).
        auth: Authentication strategy — :class:`ApiKeyHeaderAuth`,
            :class:`BasicAuth`, or :class:`BearerAuth`.
        lean: If ``True`` (default), requests add ``lean=1`` to suppress
            OSLC namespace wrappers in responses.
        asset_type_map: Optional mapping from a Maximo classification
            key (``classstructureid`` preferred, ``assettype`` as
            fallback) to a Machina :class:`AssetType`. When omitted,
            every Maximo asset is mapped to
            :attr:`AssetType.ROTATING_EQUIPMENT`. Use this to honour
            your site's Maximo taxonomy without subclassing the
            connector.

    Example:
        ```python
        from machina.connectors import Maximo
        from machina.connectors.cmms import ApiKeyHeaderAuth
        from machina.domain.asset import AssetType

        connector = Maximo(
            url="https://maximo.example.com",
            auth=ApiKeyHeaderAuth(header_name="apikey", value="my-key"),
            asset_type_map={
                "PUMPS": AssetType.ROTATING_EQUIPMENT,
                "VESSELS": AssetType.STATIC_EQUIPMENT,
                "INSTRUMENTS": AssetType.INSTRUMENT,
            },
        )
        await connector.connect()
        assets = await connector.read_assets()
        ```
    """

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.READ_ASSETS,
            Capability.READ_WORK_ORDERS,
            Capability.CREATE_WORK_ORDER,
            Capability.UPDATE_WORK_ORDER,
            Capability.READ_SPARE_PARTS,
            Capability.READ_MAINTENANCE_PLANS,
        }
    )

    _PAGE_SIZE: ClassVar[int] = 100

    def __init__(
        self,
        *,
        url: str,
        auth: _AuthUnion,
        lean: bool = True,
        asset_type_map: dict[str, AssetType] | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._auth = auth
        self._lean = lean
        self._asset_type_map = asset_type_map
        self._connected = False

    # ------------------------------------------------------------------
    # Connector lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Verify credentials against the Maximo API.

        Performs a lightweight request to the ``whoami`` endpoint.

        Raises:
            ConnectorAuthError: If credentials are invalid.
            ConnectorError: If the server is unreachable.
        """
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await request_with_retry(
                client,
                "GET",
                f"{self.url}/maximo/oslc/whoami",
                headers=self._headers(),
            )
        if resp.status_code == 401:
            raise ConnectorAuthError("Maximo authentication failed")
        if resp.status_code != 200:
            raise ConnectorError(f"Maximo health check failed: HTTP {resp.status_code}")
        self._connected = True
        logger.info("connected", connector="MaximoConnector", url=self.url)

    async def disconnect(self) -> None:
        """Close the connector."""
        self._connected = False
        logger.info("disconnected", connector="MaximoConnector")

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
        """Return all assets from Maximo (MXASSET object structure)."""
        self._ensure_connected()
        raw = await self._oslc_get("mxasset")
        return [_parse_asset(item, self._asset_type_map) for item in raw]

    async def get_asset(self, asset_id: str) -> Asset | None:
        """Look up a single asset by asset number."""
        self._ensure_connected()
        raw = await self._oslc_get(
            "mxasset",
            oslc_where=f'assetnum="{asset_id}"',
            page_size=1,
        )
        return _parse_asset(raw[0], self._asset_type_map) if raw else None

    async def read_work_orders(
        self,
        *,
        asset_id: str = "",
        status: WorkOrderStatus | str = "",
    ) -> list[WorkOrder]:
        """Read work orders, optionally filtered by asset or status.

        Args:
            asset_id: Filter by Maximo asset number.
            status: Filter by status — accepts a :class:`WorkOrderStatus`
                enum (reverse-mapped to Maximo code) or a raw Maximo
                status string for backward compatibility.
        """
        self._ensure_connected()
        clauses: list[str] = []
        if asset_id:
            clauses.append(f'assetnum="{asset_id}"')
        if status:
            maximo_status = (
                _REVERSE_MAXIMO_STATUS.get(status, status.value.upper())
                if isinstance(status, WorkOrderStatus)
                else status.upper()
            )
            clauses.append(f'status="{maximo_status}"')
        where = " and ".join(clauses) if clauses else ""
        raw = await self._oslc_get("mxwo", oslc_where=where)
        return [_parse_work_order(item) for item in raw]

    async def get_work_order(self, work_order_id: str) -> WorkOrder | None:
        """Look up a single work order by work order number."""
        self._ensure_connected()
        raw = await self._oslc_get(
            "mxwo",
            oslc_where=f'wonum="{work_order_id}"',
            page_size=1,
        )
        return _parse_work_order(raw[0]) if raw else None

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Create a new work order in Maximo.

        Args:
            work_order: Machina :class:`WorkOrder` to create.

        Returns:
            The created work order with the server-assigned ``wonum``.
        """
        self._ensure_connected()
        httpx = _require_httpx()
        payload: dict[str, Any] = {
            "description": work_order.description,
            "assetnum": work_order.asset_id,
            "worktype": _reverse_worktype(work_order.type),
            "wopriority": _reverse_priority(work_order.priority),
        }
        if work_order.assigned_to:
            payload["lead"] = work_order.assigned_to
        headers = {**self._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client,
                "POST",
                f"{self.url}/maximo/oslc/os/mxwo",
                headers=headers,
                json=payload,
            )
        if resp.status_code == 401:
            raise ConnectorAuthError("Maximo authentication failed")
        if resp.status_code not in (200, 201):
            raise ConnectorError(f"Maximo create work order failed: HTTP {resp.status_code}")
        body = resp.json()
        logger.info(
            "work_order_created",
            connector="MaximoConnector",
            work_order_id=body.get("wonum"),
            asset_id=work_order.asset_id,
        )
        return _parse_work_order(body)

    async def update_work_order(
        self,
        work_order_id: str,
        *,
        status: WorkOrderStatus | None = None,
        assigned_to: str | None = None,
        description: str | None = None,
    ) -> WorkOrder:
        """Update an existing work order in Maximo via PATCH.

        Only non-``None`` fields are included in the PATCH payload.

        Args:
            work_order_id: Maximo work order number (``wonum``).
            status: New status (reverse-mapped to Maximo code).
            assigned_to: New lead person.
            description: New work order description.

        Returns:
            The updated work order.
        """
        self._ensure_connected()
        httpx = _require_httpx()
        payload: dict[str, Any] = {}
        if status is not None:
            payload["status"] = _reverse_status(status)
        if assigned_to is not None:
            payload["lead"] = assigned_to
        if description is not None:
            payload["description"] = description
        if not payload:
            raise ConnectorError("update_work_order requires at least one field to update")
        headers = {**self._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client,
                "PATCH",
                f"{self.url}/maximo/oslc/os/mxwo/{work_order_id}",
                headers=headers,
                json=payload,
            )
        if resp.status_code == 401:
            raise ConnectorAuthError("Maximo authentication failed")
        if resp.status_code not in (200, 204):
            raise ConnectorError(f"Maximo update work order failed: HTTP {resp.status_code}")
        logger.info(
            "work_order_updated",
            connector="MaximoConnector",
            operation="update_work_order",
            work_order_id=work_order_id,
        )
        updated = await self.get_work_order(work_order_id)
        if updated is None:
            raise ConnectorError(f"Work order {work_order_id} not found after update")
        return updated

    async def close_work_order(self, work_order_id: str) -> WorkOrder:
        """Transition a work order to CLOSED status."""
        return await self.update_work_order(work_order_id, status=WorkOrderStatus.CLOSED)

    async def cancel_work_order(self, work_order_id: str) -> WorkOrder:
        """Transition a work order to CANCELLED status."""
        return await self.update_work_order(work_order_id, status=WorkOrderStatus.CANCELLED)

    async def read_spare_parts(
        self,
        *,
        sku: str = "",
    ) -> list[SparePart]:
        """Read inventory items (spare parts) from Maximo.

        Args:
            sku: Optional Maximo ``itemnum`` to narrow the lookup via an
                OSLC ``where`` clause.

        Note:
            Maximo's ``mxinventory`` object structure does not expose a
            direct asset-compatibility relation, so filtering by asset is
            not supported here. For asset-specific spare parts, consult
            the corresponding work-order job plan or ``mxpmpart``.
        """
        self._ensure_connected()
        where = f'itemnum="{sku}"' if sku else ""
        raw = await self._oslc_get("mxinventory", oslc_where=where)
        return [_parse_spare_part(item) for item in raw]

    async def read_maintenance_plans(self) -> list[MaintenancePlan]:
        """Read preventive-maintenance triggers from Maximo."""
        self._ensure_connected()
        raw = await self._oslc_get("mxpm")
        return [_parse_maintenance_plan(item) for item in raw]

    async def read_maintenance_history(self, asset_id: str) -> list[WorkOrder]:
        """Return completed/closed work orders for an asset."""
        self._ensure_connected()
        where = f'assetnum="{asset_id}" and (status="COMP" or status="CLOSE")'
        raw = await self._oslc_get("mxwo", oslc_where=where)
        return [_parse_work_order(item) for item in raw]

    # ------------------------------------------------------------------
    # Internal: OSLC REST helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Build base HTTP headers."""
        hdrs: dict[str, str] = {"Accept": "application/json"}
        return self._auth.apply(hdrs)

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")

    async def _oslc_get(
        self,
        object_structure: str,
        *,
        oslc_where: str = "",
        oslc_select: str = "",
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages from a Maximo OSLC object structure.

        Maximo paginates OSLC results via a ``responseInfo.nextPage``
        link in each page.

        Args:
            object_structure: The Maximo OS name (e.g. ``mxasset``, ``mxwo``).
            oslc_where: Optional OSLC where clause for server-side filtering.
            oslc_select: Optional comma-separated field list.
            page_size: Results per page (default :attr:`_PAGE_SIZE`).

        Returns:
            Flat list of all result dicts across all pages.
        """
        httpx = _require_httpx()
        size = page_size or self._PAGE_SIZE
        all_items: list[dict[str, Any]] = []
        url: str | None = f"{self.url}/maximo/oslc/os/{object_structure}"
        initial_params: dict[str, str] = {"oslc.pageSize": str(size)}
        if self._lean:
            initial_params["lean"] = "1"
        if oslc_where:
            initial_params["oslc.where"] = oslc_where
        if oslc_select:
            initial_params["oslc.select"] = oslc_select
        params: dict[str, str] | None = initial_params

        async with httpx.AsyncClient(timeout=30.0) as client:
            while url is not None:
                resp = await request_with_retry(
                    client,
                    "GET",
                    url,
                    headers=self._headers(),
                    params=params,
                )
                if resp.status_code == 401:
                    raise ConnectorAuthError("Maximo authentication failed")
                if resp.status_code != 200:
                    raise ConnectorError(
                        f"Maximo GET {object_structure} failed: HTTP {resp.status_code}"
                    )
                body = resp.json()
                members = body.get("member", [])
                all_items.extend(members)
                # Follow OSLC pagination link
                response_info = body.get("responseInfo", {})
                url = response_info.get("nextPage")
                # After the first request, params are embedded in nextPage URL
                params = None
        logger.debug(
            "oslc_get",
            connector="MaximoConnector",
            object_structure=object_structure,
            total=len(all_items),
        )
        return all_items


# ---------------------------------------------------------------------------
# Reverse mapping helpers (Machina → Maximo)
# ---------------------------------------------------------------------------


def _reverse_priority(priority: Priority) -> int:
    """Map Machina priority back to Maximo integer (1-4)."""
    return {
        Priority.EMERGENCY: 1,
        Priority.HIGH: 2,
        Priority.MEDIUM: 3,
        Priority.LOW: 4,
    }.get(priority, 3)


def _reverse_worktype(wo_type: WorkOrderType) -> str:
    """Map Machina work-order type to Maximo work type code."""
    return {
        WorkOrderType.CORRECTIVE: "CM",
        WorkOrderType.PREVENTIVE: "PM",
        WorkOrderType.PREDICTIVE: "CP",
        WorkOrderType.IMPROVEMENT: "EV",
    }.get(wo_type, "CM")


def _reverse_status(status: WorkOrderStatus) -> str:
    """Map Machina work-order status to Maximo status code."""
    return _REVERSE_MAXIMO_STATUS.get(status, "WAPPR")
