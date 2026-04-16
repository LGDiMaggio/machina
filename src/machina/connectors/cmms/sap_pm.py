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
* ``API_BILL_OF_MATERIAL_SRV`` — BOM / spare-part look-up
  (entity set and field names are configurable per SAP version; see
  :class:`SapPmConnector` constructor args).

The vendor payload ↔ Machina entity mapping lives as pure functions in
:mod:`machina.connectors.cmms.mappers.sap_pm` so it can be unit-tested
without HTTP mocks.

See also:
    https://api.sap.com/api/API_EQUIPMENT/overview
    https://api.sap.com/api/API_MAINTENANCEORDER/overview
    https://api.sap.com/api/API_BILL_OF_MATERIAL_SRV/overview
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.capabilities import Capability
from machina.connectors.cmms.auth import BasicAuth, OAuth2ClientCredentials
from machina.connectors.cmms.mappers import sap_pm as sap_mapper
from machina.connectors.cmms.retry import request_with_retry
from machina.domain.work_order import WorkOrder, WorkOrderStatus
from machina.exceptions import ConnectorAuthError, ConnectorError

if TYPE_CHECKING:
    from machina.domain.asset import Asset
    from machina.domain.maintenance_plan import MaintenancePlan
    from machina.domain.spare_part import SparePart

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
        bom_service: OData service group used by :meth:`read_spare_parts`.
            Defaults to ``"API_BILL_OF_MATERIAL_SRV"`` (standard S/4HANA
            Cloud service). Override for on-premise systems with
            different service names.
        bom_entity_set: Entity set inside ``bom_service``. Defaults to
            ``"BillOfMaterialItem"``.
        bom_material_field: Name of the material / component field on
            ``bom_entity_set`` used by the ``sku`` filter. Defaults to
            ``"BillOfMaterialComponent"``.
        bom_equipment_field: Name of the equipment field on
            ``bom_entity_set`` used by the ``asset_id`` filter. Defaults
            to the empty string, which disables server-side filtering by
            asset (because the default ``BillOfMaterialItem`` entity
            does not directly expose an Equipment key). Set this to a
            valid field name if your SAP version exposes one.

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
        sap_client: str = "",
        bom_service: str = "API_BILL_OF_MATERIAL_SRV",
        bom_entity_set: str = "BillOfMaterialItem",
        bom_material_field: str = "BillOfMaterialComponent",
        bom_equipment_field: str = "",
    ) -> None:
        self.url = url.rstrip("/")
        self._auth = auth
        self._sap_client = sap_client
        self._bom_service = bom_service
        self._bom_entity_set = bom_entity_set
        self._bom_material_field = bom_material_field
        self._bom_equipment_field = bom_equipment_field
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

            resp = await request_with_retry(
                client,
                "GET",
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
        return [sap_mapper.parse_asset(item) for item in raw]

    async def get_asset(self, asset_id: str) -> Asset | None:
        """Look up a single equipment record by number."""
        self._ensure_connected()
        raw = await self._odata_get(
            "API_EQUIPMENT",
            "Equipment",
            odata_filter=f"Equipment eq '{asset_id}'",
            top=1,
        )
        return sap_mapper.parse_asset(raw[0]) if raw else None

    async def read_work_orders(
        self,
        *,
        asset_id: str = "",
        status: WorkOrderStatus | str = "",
    ) -> list[WorkOrder]:
        """Read maintenance orders from SAP PM.

        Args:
            asset_id: Filter by equipment number.
            status: Filter by status — accepts a :class:`WorkOrderStatus`
                enum (automatically reverse-mapped to the SAP code) or a
                raw SAP status string like ``"REL"`` for backward
                compatibility.
        """
        self._ensure_connected()
        filters: list[str] = []
        if asset_id:
            filters.append(f"Equipment eq '{asset_id}'")
        if status:
            sap_status = (
                sap_mapper.REVERSE_SAP_STATUS.get(status, status.value)
                if isinstance(status, WorkOrderStatus)
                else status
            )
            filters.append(f"MaintenanceOrderSystemStatus eq '{sap_status}'")
        odata_filter = " and ".join(filters) if filters else ""
        raw = await self._odata_get(
            "API_MAINTENANCEORDER",
            "MaintenanceOrder",
            odata_filter=odata_filter,
        )
        return [sap_mapper.parse_work_order(item) for item in raw]

    async def get_work_order(self, work_order_id: str) -> WorkOrder | None:
        """Look up a single maintenance order by number."""
        self._ensure_connected()
        raw = await self._odata_get(
            "API_MAINTENANCEORDER",
            "MaintenanceOrder",
            odata_filter=f"MaintenanceOrder eq '{work_order_id}'",
            top=1,
        )
        return sap_mapper.parse_work_order(raw[0]) if raw else None

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Create a new maintenance order in SAP PM.

        Args:
            work_order: Machina :class:`WorkOrder` to create.

        Returns:
            The created work order with the server-assigned ID.
        """
        self._ensure_connected()
        payload: dict[str, Any] = {
            "MaintenanceOrderDesc": work_order.description,
            "Equipment": work_order.asset_id,
            "MaintenanceOrderType": sap_mapper.reverse_order_type(work_order.type),
            "MaintPriority": sap_mapper.reverse_priority(work_order.priority),
        }
        if work_order.assigned_to:
            payload["MaintOrdPersonResponsible"] = work_order.assigned_to
        resp = await self._write_with_csrf(
            "POST",
            f"{self.url}/API_MAINTENANCEORDER/MaintenanceOrder",
            payload,
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
            operation="create_work_order",
            work_order_id=result.get("MaintenanceOrder"),
            asset_id=work_order.asset_id,
        )
        return sap_mapper.parse_work_order(result)

    async def update_work_order(
        self,
        work_order_id: str,
        *,
        status: WorkOrderStatus | None = None,
        assigned_to: str | None = None,
        description: str | None = None,
    ) -> WorkOrder:
        """Update an existing maintenance order in SAP PM.

        Only non-``None`` fields are included in the PATCH payload.

        Args:
            work_order_id: SAP maintenance order number.
            status: New :class:`WorkOrderStatus` (reverse-mapped to SAP code).
            assigned_to: New responsible person.
            description: New order description.

        Returns:
            The updated work order.
        """
        self._ensure_connected()
        payload: dict[str, Any] = {}
        if status is not None:
            payload["MaintenanceOrderSystemStatus"] = sap_mapper.reverse_status(status)
        if assigned_to is not None:
            payload["MaintOrdPersonResponsible"] = assigned_to
        if description is not None:
            payload["MaintenanceOrderDesc"] = description
        if not payload:
            raise ConnectorError("update_work_order requires at least one field to update")
        resp = await self._write_with_csrf(
            "PATCH",
            f"{self.url}/API_MAINTENANCEORDER/MaintenanceOrder('{work_order_id}')",
            payload,
        )
        if resp.status_code == 401:
            raise ConnectorAuthError("SAP PM authentication failed")
        if resp.status_code not in (200, 204):
            raise ConnectorError(
                f"SAP PM update maintenance order failed: HTTP {resp.status_code}"
            )
        logger.info(
            "work_order_updated",
            connector="SapPmConnector",
            operation="update_work_order",
            work_order_id=work_order_id,
        )
        # Re-fetch the updated entity to return the full work order.
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
        asset_id: str = "",
        sku: str = "",
    ) -> list[SparePart]:
        """Read material / spare-part data from SAP's BOM service.

        The service group, entity set, and filter field names are
        configured on the connector (see constructor args
        ``bom_service``, ``bom_entity_set``, ``bom_material_field``,
        ``bom_equipment_field``). Defaults target the standard S/4HANA
        ``API_BILL_OF_MATERIAL_SRV/BillOfMaterialItem`` entity.

        Args:
            asset_id: Optional Equipment identifier. Applied as a
                server-side OData ``$filter`` clause only when
                ``bom_equipment_field`` is configured; otherwise logged
                and ignored.
            sku: Optional material number, translated to a server-side
                ``$filter`` on ``bom_material_field``.
        """
        self._ensure_connected()
        filters: list[str] = []
        if asset_id:
            if self._bom_equipment_field:
                filters.append(f"{self._bom_equipment_field} eq '{asset_id}'")
            else:
                logger.warning(
                    "bom_asset_filter_unsupported",
                    connector="SapPmConnector",
                    asset_id=asset_id,
                    bom_entity_set=self._bom_entity_set,
                    message=(
                        "asset_id filter ignored: bom_equipment_field is not "
                        "configured for this BOM service"
                    ),
                )
        if sku:
            filters.append(f"{self._bom_material_field} eq '{sku}'")
        odata_filter = " and ".join(filters) if filters else ""
        raw = await self._odata_get(
            self._bom_service,
            self._bom_entity_set,
            odata_filter=odata_filter,
        )
        return [sap_mapper.parse_spare_part(item) for item in raw]

    async def read_maintenance_plans(self) -> list[MaintenancePlan]:
        """Read preventive-maintenance plans from SAP PM."""
        self._ensure_connected()
        raw = await self._odata_get("API_MAINTENANCEPLAN", "MaintenancePlan")
        return [sap_mapper.parse_maintenance_plan(item) for item in raw]

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
        return [sap_mapper.parse_work_order(item) for item in raw]

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

    async def _write_with_csrf(self, method: str, url: str, payload: dict[str, Any]) -> Any:
        """Execute a write request (POST/PATCH) with CSRF token.

        SAP OData services require a CSRF token tied to the HTTP
        session cookie. This helper performs the token fetch and the
        write within a **single** ``httpx.AsyncClient`` context so that
        the session cookies are shared.

        Raises:
            ConnectorAuthError: If the CSRF fetch or the write returns 401.
            ConnectorError: If the CSRF fetch fails or returns no token.
        """
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Fetch CSRF token
            csrf_resp = await request_with_retry(
                client,
                "GET",
                f"{self.url}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1",
                headers={**self._headers(), "X-CSRF-Token": "Fetch"},
            )
            if csrf_resp.status_code == 401:
                raise ConnectorAuthError("SAP PM authentication failed during CSRF token fetch")
            if csrf_resp.status_code not in (200, 204):
                raise ConnectorError(
                    f"SAP PM CSRF token fetch failed: HTTP {csrf_resp.status_code}"
                )
            token = csrf_resp.headers.get("x-csrf-token", "")
            if not token:
                raise ConnectorError(
                    "SAP PM CSRF token fetch did not return an x-csrf-token header"
                )

            # Step 2: Write with same client (session cookies shared)
            headers = {
                **self._headers(),
                "Content-Type": "application/json",
                "X-CSRF-Token": str(token),
            }
            return await request_with_retry(client, method, url, headers=headers, json=payload)

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
                resp = await request_with_retry(
                    client,
                    "GET",
                    url,
                    headers=self._headers(),
                    params=params,
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
