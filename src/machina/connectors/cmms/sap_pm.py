"""Machina connector for SAP Plant Maintenance (SAP PM / S/4HANA).

SAP PM exposes maintenance data via OData v2/v4 REST endpoints. This
connector reads equipment (assets), maintenance orders (work orders),
material BOM items (spare parts), and maintenance plans, normalising all
responses into Machina domain entities.

Authentication supports:

* **OAuth 2.0 Client Credentials** — recommended for SAP S/4HANA Cloud
  and SAP BTP-integrated environments
  (:class:`~machina.connectors.cmms.auth.OAuth2ClientCredentials`).
* **Basic auth** — on-premise SAP systems with HTTP basic enabled
  (:class:`~machina.connectors.cmms.auth.BasicAuth`).

Key SAP OData service groups consumed:

* ``API_EQUIPMENT`` — Equipment master data
* ``API_MAINTENANCEORDER`` — Maintenance orders (work orders)
* ``API_MAINTENANCEPLAN`` — Preventive-maintenance plans
* ``API_BOM_WHERE_USED`` — BOM / spare parts look-up (simplified)

See also:
    https://api.sap.com/api/API_EQUIPMENT/overview
    https://api.sap.com/api/API_MAINTENANCEORDER/overview
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.cmms.auth import BasicAuth, OAuth2ClientCredentials
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

_AuthUnion = OAuth2ClientCredentials | BasicAuth


def _require_httpx() -> Any:
    """Import httpx lazily so the extra is truly optional."""
    try:
        import httpx
    except ImportError as exc:
        raise ConnectorError(
            "httpx is required for SapPmConnector. Install with: pip install machina-ai[cmms-rest]"
        ) from exc
    return httpx


# ---------------------------------------------------------------------------
# SAP → Machina entity mapping helpers
# ---------------------------------------------------------------------------

_SAP_ORDER_TYPE_MAP: dict[str, WorkOrderType] = {
    "PM01": WorkOrderType.CORRECTIVE,
    "PM02": WorkOrderType.PREVENTIVE,
    "PM03": WorkOrderType.PREDICTIVE,
    "PM04": WorkOrderType.IMPROVEMENT,
}

_SAP_PRIORITY_MAP: dict[str, Priority] = {
    "1": Priority.EMERGENCY,
    "2": Priority.HIGH,
    "3": Priority.MEDIUM,
    "4": Priority.LOW,
}

_SAP_STATUS_MAP: dict[str, WorkOrderStatus] = {
    "CRTD": WorkOrderStatus.CREATED,
    "REL": WorkOrderStatus.ASSIGNED,
    "PCNF": WorkOrderStatus.IN_PROGRESS,
    "CNF": WorkOrderStatus.COMPLETED,
    "TECO": WorkOrderStatus.CLOSED,
    "CLSD": WorkOrderStatus.CLOSED,
    "DLFL": WorkOrderStatus.CANCELLED,
}

_SAP_EQUIP_CATEGORY_MAP: dict[str, AssetType] = {
    "M": AssetType.ROTATING_EQUIPMENT,  # Machinery
    "E": AssetType.ELECTRICAL,  # Electrical
    "I": AssetType.INSTRUMENT,  # Instrumentation
    "P": AssetType.PIPING,  # Piping
    "H": AssetType.HVAC,  # HVAC
    "S": AssetType.SAFETY,  # Safety
}


def _parse_asset(data: dict[str, Any]) -> Asset:
    """Convert SAP Equipment OData entity to a Machina :class:`Asset`."""
    cat = str(data.get("EquipmentCategory", ""))
    return Asset(
        id=str(data.get("Equipment", data.get("EquipmentNumber", ""))),
        name=str(data.get("EquipmentName", data.get("Description", ""))),
        type=_SAP_EQUIP_CATEGORY_MAP.get(cat, AssetType.ROTATING_EQUIPMENT),
        location=str(data.get("FunctionalLocation", "")),
        manufacturer=str(data.get("Manufacturer", data.get("ManufacturerPartNmbr", ""))),
        model=str(data.get("ModelNumber", "")),
        serial_number=str(data.get("SerialNumber", data.get("ManufacturerSerialNumber", ""))),
        criticality=_sap_criticality(data.get("ABCIndicator", "")),
        parent=data.get("SuperordinateEquipment") or None,
        equipment_class_code=data.get("EquipmentClassCode") or None,
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "Equipment",
                "EquipmentNumber",
                "EquipmentName",
                "Description",
                "EquipmentCategory",
                "FunctionalLocation",
                "Manufacturer",
                "ManufacturerPartNmbr",
                "ModelNumber",
                "SerialNumber",
                "ManufacturerSerialNumber",
                "ABCIndicator",
                "SuperordinateEquipment",
                "EquipmentClassCode",
            }
        },
    )


def _sap_criticality(abc_indicator: Any) -> Criticality:
    """Map SAP ABC indicator to Machina criticality."""
    val = str(abc_indicator).upper().strip()
    if val == "A":
        return Criticality.A
    if val == "B":
        return Criticality.B
    return Criticality.C


def _parse_work_order(data: dict[str, Any]) -> WorkOrder:
    """Convert SAP MaintenanceOrder OData entity to a :class:`WorkOrder`."""
    raw_type = str(data.get("MaintenanceOrderType", ""))
    wo_type = _SAP_ORDER_TYPE_MAP.get(raw_type, WorkOrderType.CORRECTIVE)

    raw_priority = str(data.get("MaintPriority", "3"))
    priority = _SAP_PRIORITY_MAP.get(raw_priority, Priority.MEDIUM)

    # SAP uses system status; try multiple fields
    sys_status = str(data.get("MaintenanceOrderSystemStatus", data.get("SystemStatus", "")))
    status = _map_sap_status(sys_status)

    now = datetime.now(tz=UTC)
    created = data.get("CreationDate", data.get("MaintOrdBasicStartDate", ""))
    updated = data.get("LastChangeDateTime", data.get("MaintOrdBasicEndDate", ""))

    return WorkOrder(
        id=str(data.get("MaintenanceOrder", data.get("MaintenanceOrderNumber", ""))),
        type=wo_type,
        priority=priority,
        status=status,
        asset_id=str(data.get("Equipment", data.get("EquipmentNumber", ""))),
        description=str(data.get("MaintenanceOrderDesc", data.get("Description", ""))),
        assigned_to=data.get("MaintOrdPersonResponsible") or None,
        created_at=_parse_sap_datetime(created) if created else now,
        updated_at=_parse_sap_datetime(updated) if updated else now,
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "MaintenanceOrder",
                "MaintenanceOrderNumber",
                "MaintenanceOrderType",
                "MaintPriority",
                "MaintenanceOrderSystemStatus",
                "SystemStatus",
                "Equipment",
                "EquipmentNumber",
                "MaintenanceOrderDesc",
                "Description",
                "MaintOrdPersonResponsible",
                "CreationDate",
                "MaintOrdBasicStartDate",
                "LastChangeDateTime",
                "MaintOrdBasicEndDate",
            }
        },
    )


def _map_sap_status(sys_status: str) -> WorkOrderStatus:
    """Map SAP system status string to :class:`WorkOrderStatus`.

    SAP system status can be a compound string like ``"CRTD REL MANC"``.
    We check for known tokens in priority order.
    """
    tokens = sys_status.upper().split()
    # Check in reverse lifecycle order (most progressed wins)
    for token in ("DLFL", "CLSD", "TECO", "CNF", "PCNF", "REL", "CRTD"):
        if token in tokens:
            return _SAP_STATUS_MAP[token]
    # Fallback: try direct lookup of the full string
    return _SAP_STATUS_MAP.get(sys_status, WorkOrderStatus.CREATED)


def _parse_spare_part(data: dict[str, Any]) -> SparePart:
    """Convert SAP material / BOM component data to a :class:`SparePart`."""
    return SparePart(
        sku=str(data.get("Material", data.get("MaterialNumber", ""))),
        name=str(data.get("MaterialDescription", data.get("Description", ""))),
        stock_quantity=int(data.get("AvailableQuantity", data.get("Quantity", 0))),
        unit_cost=float(data.get("StandardPrice", data.get("Price", 0.0))),
        warehouse_location=str(data.get("StorageLocation", data.get("Plant", ""))),
    )


def _parse_maintenance_plan(data: dict[str, Any]) -> MaintenancePlan:
    """Convert SAP MaintenancePlan OData entity to a :class:`MaintenancePlan`."""
    cycle_val = int(data.get("MaintenancePlanCycleValue", data.get("CycleValue", 0)))
    cycle_unit = str(data.get("MaintenancePlanCycleUnit", data.get("CycleUnit", "DAY")))
    interval = _sap_cycle_to_interval(cycle_val, cycle_unit)

    return MaintenancePlan(
        id=str(data.get("MaintenancePlan", data.get("MaintenancePlanNumber", ""))),
        asset_id=str(data.get("Equipment", "")),
        name=str(data.get("MaintenancePlanDesc", data.get("Description", ""))),
        interval=interval,
        active=str(data.get("MaintenancePlanStatus", "")).upper() != "INAC",
    )


def _sap_cycle_to_interval(value: int, unit: str) -> Interval:
    """Map SAP cycle value + unit to a Machina :class:`Interval`."""
    unit_upper = unit.upper().strip()
    if unit_upper in ("DAY", "TAG"):
        return Interval(days=value)
    if unit_upper in ("WK", "WOC"):
        return Interval(weeks=value)
    if unit_upper in ("MON", "MON."):
        return Interval(months=value)
    if unit_upper in ("H", "STD"):
        return Interval(hours=value)
    # Default to days
    return Interval(days=value)


def _parse_sap_datetime(value: str) -> datetime:
    """Parse SAP date/datetime strings into timezone-aware datetime.

    Handles ISO-8601, SAP ``/Date(millis)/`` format, and plain
    ``YYYY-MM-DD`` dates.
    """
    if not value:
        return datetime.now(tz=UTC)
    # SAP JSON /Date(1234567890000)/ format
    if value.startswith("/Date("):
        millis_str = value.replace("/Date(", "").replace(")/", "")
        # Handle timezone offset: /Date(1234567890000+0000)/
        if "+" in millis_str:
            millis_str = millis_str.split("+")[0]
        if "-" in millis_str and millis_str.index("-") > 0:
            millis_str = millis_str.split("-")[0]
        millis = int(millis_str)
        return datetime.fromtimestamp(millis / 1000, tz=UTC)
    # Standard ISO-8601
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        pass
    # Plain date YYYY-MM-DD or YYYYMMDD
    try:
        if len(value) == 8 and value.isdigit():
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return datetime.now(tz=UTC)


class SapPmConnector:
    """Connector for SAP Plant Maintenance (S/4HANA).

    Provides integration with SAP's OData APIs for reading and creating
    maintenance data.

    Args:
        url: Base URL of the SAP OData gateway
            (e.g. ``https://sap.example.com/sap/opu/odata/sap``).
        auth: Authentication strategy — :class:`OAuth2ClientCredentials`
            or :class:`BasicAuth`.
        sap_client: SAP client number (sent as ``sap-client`` header).

    Example:
        ```python
        from machina.connectors import SapPM
        from machina.connectors.cmms import OAuth2ClientCredentials

        connector = SapPM(
            url="https://sap.example.com/sap/opu/odata/sap",
            auth=OAuth2ClientCredentials(
                token_url="https://sap.example.com/oauth/token",
                client_id="my-client",
                client_secret="my-secret",
            ),
            sap_client="100",
        )
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

    _PAGE_SIZE: ClassVar[int] = 100

    def __init__(
        self,
        *,
        url: str,
        auth: _AuthUnion,
        sap_client: str = "",
    ) -> None:
        self.url = url.rstrip("/")
        self._auth = auth
        self._sap_client = sap_client
        self._connected = False

    # ------------------------------------------------------------------
    # Connector lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Verify connection to SAP OData gateway.

        For :class:`OAuth2ClientCredentials`, fetches the access token
        first. Then issues a ``$metadata`` request to verify reachability.

        Raises:
            ConnectorAuthError: If authentication fails.
            ConnectorError: If the OData gateway is unreachable.
        """
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Fetch OAuth2 token if needed
            if isinstance(self._auth, OAuth2ClientCredentials):
                await self._auth.fetch_token(client)

            resp = await client.get(
                f"{self.url}/API_EQUIPMENT/$metadata",
                headers=self._headers(),
            )
        if resp.status_code == 401:
            raise ConnectorAuthError("SAP PM authentication failed")
        if resp.status_code not in (200, 204):
            raise ConnectorError(f"SAP PM health check failed: HTTP {resp.status_code}")
        self._connected = True
        logger.info("connected", connector="SapPmConnector", url=self.url)

    async def disconnect(self) -> None:
        """Close the connector."""
        self._connected = False
        logger.info("disconnected", connector="SapPmConnector")

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
        """Return all equipment master records from SAP PM."""
        self._ensure_connected()
        raw = await self._odata_get("API_EQUIPMENT", "Equipment")
        return [_parse_asset(item) for item in raw]

    async def get_asset(self, asset_id: str) -> Asset | None:
        """Look up a single equipment record by number."""
        self._ensure_connected()
        raw = await self._odata_get(
            "API_EQUIPMENT",
            "Equipment",
            odata_filter=f"Equipment eq '{asset_id}'",
            top=1,
        )
        return _parse_asset(raw[0]) if raw else None

    async def read_work_orders(
        self,
        *,
        asset_id: str = "",
        status: str = "",
    ) -> list[WorkOrder]:
        """Read maintenance orders from SAP PM."""
        self._ensure_connected()
        filters: list[str] = []
        if asset_id:
            filters.append(f"Equipment eq '{asset_id}'")
        if status:
            filters.append(f"MaintenanceOrderSystemStatus eq '{status}'")
        odata_filter = " and ".join(filters) if filters else ""
        raw = await self._odata_get(
            "API_MAINTENANCEORDER",
            "MaintenanceOrder",
            odata_filter=odata_filter,
        )
        return [_parse_work_order(item) for item in raw]

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Create a new maintenance order in SAP PM.

        Args:
            work_order: Machina :class:`WorkOrder` to create.

        Returns:
            The created work order with the server-assigned ID.
        """
        self._ensure_connected()
        httpx = _require_httpx()
        payload: dict[str, Any] = {
            "MaintenanceOrderDesc": work_order.description,
            "Equipment": work_order.asset_id,
            "MaintenanceOrderType": _reverse_order_type(work_order.type),
            "MaintPriority": _reverse_priority(work_order.priority),
        }
        if work_order.assigned_to:
            payload["MaintOrdPersonResponsible"] = work_order.assigned_to
        headers = {
            **self._headers(),
            "Content-Type": "application/json",
            "X-CSRF-Token": await self._fetch_csrf_token(),
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.url}/API_MAINTENANCEORDER/MaintenanceOrder",
                headers=headers,
                json=payload,
            )
        if resp.status_code == 401:
            raise ConnectorAuthError("SAP PM authentication failed")
        if resp.status_code not in (200, 201):
            raise ConnectorError(
                f"SAP PM create maintenance order failed: HTTP {resp.status_code}"
            )
        body = resp.json()
        result = body.get("d", body)
        logger.info(
            "work_order_created",
            connector="SapPmConnector",
            work_order_id=result.get("MaintenanceOrder"),
            asset_id=work_order.asset_id,
        )
        return _parse_work_order(result)

    async def read_spare_parts(
        self,
        *,
        asset_id: str = "",
        sku: str = "",
    ) -> list[SparePart]:
        """Read material / spare part data from SAP."""
        self._ensure_connected()
        filters: list[str] = []
        if asset_id:
            filters.append(f"Equipment eq '{asset_id}'")
        if sku:
            filters.append(f"Material eq '{sku}'")
        odata_filter = " and ".join(filters) if filters else ""
        raw = await self._odata_get(
            "API_EQUIPMENT",
            "EquipmentBOM",
            odata_filter=odata_filter,
        )
        return [_parse_spare_part(item) for item in raw]

    async def read_maintenance_plans(self) -> list[MaintenancePlan]:
        """Read preventive-maintenance plans from SAP PM."""
        self._ensure_connected()
        raw = await self._odata_get("API_MAINTENANCEPLAN", "MaintenancePlan")
        return [_parse_maintenance_plan(item) for item in raw]

    async def read_maintenance_history(self, asset_id: str) -> list[WorkOrder]:
        """Return completed/closed maintenance orders for an asset."""
        self._ensure_connected()
        odata_filter = (
            f"Equipment eq '{asset_id}' and "
            "(MaintenanceOrderSystemStatus eq 'CNF' or "
            "MaintenanceOrderSystemStatus eq 'TECO' or "
            "MaintenanceOrderSystemStatus eq 'CLSD')"
        )
        raw = await self._odata_get(
            "API_MAINTENANCEORDER",
            "MaintenanceOrder",
            odata_filter=odata_filter,
        )
        return [_parse_work_order(item) for item in raw]

    # ------------------------------------------------------------------
    # Internal: OData REST helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Build base HTTP headers, including sap-client if configured."""
        hdrs: dict[str, str] = {"Accept": "application/json"}
        if self._sap_client:
            hdrs["sap-client"] = self._sap_client
        return self._auth.apply(hdrs)

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")

    async def _fetch_csrf_token(self) -> str:
        """Fetch a CSRF token for write operations.

        SAP OData services require a CSRF token (``X-CSRF-Token: Fetch``
        on a GET, then the returned token on POST/PATCH/DELETE).
        """
        httpx = _require_httpx()
        headers = {**self._headers(), "X-CSRF-Token": "Fetch"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self.url}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
                headers=headers,
            )
        return str(resp.headers.get("x-csrf-token", ""))

    async def _odata_get(
        self,
        service: str,
        entity_set: str,
        *,
        odata_filter: str = "",
        odata_select: str = "",
        odata_expand: str = "",
        top: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages from a SAP OData entity set.

        Uses client-driven ``$top`` / ``$skip`` pagination, with fallback
        to server-driven ``__next`` (``@odata.nextLink``) when present.

        Args:
            service: OData service group (e.g. ``API_EQUIPMENT``).
            entity_set: Entity set name (e.g. ``Equipment``).
            odata_filter: Optional ``$filter`` expression.
            odata_select: Optional ``$select`` field list.
            odata_expand: Optional ``$expand`` navigation properties.
            top: Override page size (e.g. ``1`` for single-record lookups).

        Returns:
            Flat list of all result dicts across all pages.
        """
        httpx = _require_httpx()
        page_size = top or self._PAGE_SIZE
        all_items: list[dict[str, Any]] = []
        url: str | None = f"{self.url}/{service}/{entity_set}"
        initial_params: dict[str, str] = {
            "$top": str(page_size),
            "$format": "json",
        }
        if odata_filter:
            initial_params["$filter"] = odata_filter
        if odata_select:
            initial_params["$select"] = odata_select
        if odata_expand:
            initial_params["$expand"] = odata_expand
        params: dict[str, str] | None = initial_params
        skip = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            while url is not None:
                if params is not None and skip > 0:
                    params["$skip"] = str(skip)
                resp = await client.get(
                    url,
                    headers=self._headers(),
                    **({"params": params} if params is not None else {}),
                )
                if resp.status_code == 401:
                    raise ConnectorAuthError("SAP PM authentication failed")
                if resp.status_code != 200:
                    raise ConnectorError(
                        f"SAP PM GET {service}/{entity_set} failed: HTTP {resp.status_code}"
                    )
                body = resp.json()
                # OData v2 wraps in "d": {"results": [...]}
                # OData v4 uses "value": [...]
                d = body.get("d", body)
                results = d.get("results", d.get("value", []))
                if isinstance(results, dict):
                    # Single entity returned (not a list)
                    results = [results]
                all_items.extend(results)

                # Server-driven pagination
                next_link = d.get("__next", body.get("@odata.nextLink"))
                if next_link and (top is None):
                    url = next_link
                    params = None  # nextLink includes all query params
                    skip = 0
                elif top is not None:
                    # Single-shot lookup; don't paginate
                    url = None
                elif len(results) < page_size:
                    url = None
                else:
                    skip += page_size

        logger.debug(
            "odata_get",
            connector="SapPmConnector",
            service=service,
            entity_set=entity_set,
            total=len(all_items),
        )
        return all_items


# ---------------------------------------------------------------------------
# Reverse mapping helpers (Machina → SAP)
# ---------------------------------------------------------------------------


def _reverse_priority(priority: Priority) -> str:
    """Map Machina priority to SAP priority code."""
    return {
        Priority.EMERGENCY: "1",
        Priority.HIGH: "2",
        Priority.MEDIUM: "3",
        Priority.LOW: "4",
    }.get(priority, "3")


def _reverse_order_type(wo_type: WorkOrderType) -> str:
    """Map Machina work-order type to SAP order type."""
    return {
        WorkOrderType.CORRECTIVE: "PM01",
        WorkOrderType.PREVENTIVE: "PM02",
        WorkOrderType.PREDICTIVE: "PM03",
        WorkOrderType.IMPROVEMENT: "PM04",
    }.get(wo_type, "PM01")
