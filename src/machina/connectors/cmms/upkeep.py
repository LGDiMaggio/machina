"""Machina connector for UpKeep CMMS.

UpKeep is a cloud-based CMMS platform with a well-documented REST API
(``/api/v2/``). This connector reads assets, work orders, spare parts,
and preventive-maintenance plans, normalising all responses into Machina
domain entities.

Authentication uses a *Session-Token* header (API key issued from the
UpKeep web UI under **Account Settings → API Tokens**).

The vendor payload ↔ Machina entity mapping lives as pure functions in
:mod:`machina.connectors.cmms.mappers.upkeep` so it can be unit-tested
without HTTP mocks.

See also: https://developers.onupkeep.com/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus, sandbox_aware
from machina.connectors.capabilities import Capability
from machina.connectors.cmms.auth import ApiKeyHeaderAuth
from machina.connectors.cmms.mappers import upkeep as upkeep_mapper
from machina.connectors.cmms.paths import path_segment
from machina.connectors.cmms.retry import request_with_retry
from machina.domain.work_order import (
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)
from machina.exceptions import ConnectorAuthError, ConnectorError, ConnectorSchemaError

if TYPE_CHECKING:
    from machina.domain.asset import Asset
    from machina.domain.maintenance_plan import MaintenancePlan
    from machina.domain.spare_part import SparePart

logger = structlog.get_logger(__name__)

# The most parts read_spare_parts(asset_id=...) fetches for one asset, one
# request each: an asset listing more is refused before any part request.
_MAX_ASSET_PARTS = 100


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


def _raise_for_record_status(status_code: int, kind: str) -> None:
    """Raise for a failed single-record GET, naming only the record kind and status."""
    if status_code == 401:
        raise ConnectorAuthError("UpKeep API key is invalid")
    if status_code != 200:
        raise ConnectorError(f"UpKeep GET {kind} failed: HTTP {status_code}")


def _log_spare_parts_refused(asset_id: str, reason: str, **context: Any) -> None:
    """Log why an asset's spare parts were refused, without part IDs, SKUs or URLs."""
    logger.warning(
        "upkeep_spare_parts_refused",
        connector="UpKeepConnector",
        operation="read_spare_parts",
        asset_id=asset_id,
        reason=reason,
        **context,
    )


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

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.READ_ASSETS,
            Capability.READ_WORK_ORDERS,
            Capability.GET_WORK_ORDER,
            Capability.CREATE_WORK_ORDER,
            Capability.UPDATE_WORK_ORDER,
            Capability.READ_SPARE_PARTS,
            Capability.READ_MAINTENANCE_PLANS,
            Capability.READ_MAINTENANCE_HISTORY,
        }
    )

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
        return [upkeep_mapper.parse_asset(item) for item in raw]

    async def get_asset(self, asset_id: str) -> Asset | None:
        """Look up a single asset by ID."""
        self._ensure_connected()
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client,
                "GET",
                f"{self.url}/api/v2/assets/{path_segment(asset_id)}",
                headers=self._headers(),
            )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ConnectorError(f"UpKeep GET asset failed: HTTP {resp.status_code}")
        body = resp.json()
        result = body.get("result", body)
        return upkeep_mapper.parse_asset(result)

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
                upkeep_mapper.REVERSE_UPKEEP_STATUS.get(status, status.value)
                if isinstance(status, WorkOrderStatus)
                else status
            )
            params["status"] = upkeep_status
        raw = await self._paginated_get("/api/v2/work-orders", params=params)
        return [upkeep_mapper.parse_work_order(item) for item in raw]

    async def get_work_order(self, work_order_id: str) -> WorkOrder | None:
        """Look up a single work order by ID."""
        self._ensure_connected()
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client,
                "GET",
                f"{self.url}/api/v2/work-orders/{path_segment(work_order_id)}",
                headers=self._headers(),
            )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ConnectorError(f"UpKeep GET work order failed: HTTP {resp.status_code}")
        body = resp.json()
        result = body.get("result", body)
        return upkeep_mapper.parse_work_order(result)

    @sandbox_aware
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
            "priority": upkeep_mapper.reverse_priority(work_order.priority),
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
        return upkeep_mapper.parse_work_order(result)

    @sandbox_aware
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
            payload["status"] = upkeep_mapper.reverse_status(status)
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
                f"{self.url}/api/v2/work-orders/{path_segment(work_order_id)}",
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

    @sandbox_aware
    async def close_work_order(self, work_order_id: str) -> WorkOrder:
        """Transition a work order to CLOSED (maps to 'complete' in UpKeep)."""
        return await self.update_work_order(work_order_id, status=WorkOrderStatus.CLOSED)  # type: ignore[no-any-return]

    @sandbox_aware
    async def cancel_work_order(self, work_order_id: str) -> WorkOrder:
        """Transition a work order to CANCELLED (maps to 'onHold' in UpKeep)."""
        return await self.update_work_order(work_order_id, status=WorkOrderStatus.CANCELLED)  # type: ignore[no-any-return]

    async def read_spare_parts(
        self,
        *,
        asset_id: str = "",
        sku: str = "",
    ) -> list[SparePart]:
        """Read spare parts (UpKeep calls them *parts*).

        Without ``asset_id`` the whole parts inventory is paged from
        ``/api/v2/parts``. With it, the asset is read and each part its
        ``parts`` list names is fetched (see Note).

        Args:
            asset_id: Optional UpKeep asset ID: returns only the parts that
                asset lists. Empty means no asset filter.
            sku: Optional SKU / part number to filter the result in-memory
                after fetching. Matches the parsed :attr:`SparePart.sku`,
                which prefers the physical part identifier. With
                ``asset_id``, both filters must hold.

        Raises:
            ConnectorError: If the asset ID cannot be one URL path segment,
                UpKeep has no such asset, the asset lists more than 100
                parts, UpKeep cannot return a part the asset lists (HTTP
                404), or a request answers another error status.
            ConnectorAuthError: If UpKeep rejects the API key.
            ConnectorSchemaError: If UpKeep does not return a well-formed
                record of the asset (another record, or a malformed ``parts``
                list), a part record is malformed, or a part request returns
                another part.

        Note:
            UpKeep's ``/api/v2/parts`` endpoint has no asset filter; the
            asset record (``GET /api/v2/assets/<ID>``) lists the IDs of its
            parts, and each is fetched with ``GET /api/v2/parts/<ID>``: 1 + k
            requests for k listed parts, never the whole inventory. A part
            listed twice is fetched and returned once. Any part that cannot
            be read refuses the whole read rather than returning the others
            as the asset's complete list.
        """
        self._ensure_connected()
        if asset_id:
            parts = await self._read_asset_spare_parts(asset_id)
        else:
            raw = await self._paginated_get("/api/v2/parts")
            parts = [upkeep_mapper.parse_spare_part(item) for item in raw]
        if sku:
            parts = [p for p in parts if p.sku == sku]
        return parts

    async def read_maintenance_plans(self) -> list[MaintenancePlan]:
        """Read preventive-maintenance plans from UpKeep's PM templates.

        Returns one plan per PM schedule: the asset and the recurrence live
        on each schedule, the name and tasks on its template. Legacy PM
        triggers (``/api/v2/preventive-maintenance``) are not read — their
        documented responses carry no asset.
        """
        self._ensure_connected()
        return [
            plan
            for template in await self._read_pm_templates()
            for plan in upkeep_mapper.parse_maintenance_plans(template)
        ]

    async def read_maintenance_history(self, asset_id: str) -> list[WorkOrder]:
        """Return completed work orders for an asset.

        Raises:
            ConnectorError: If ``asset_id`` is missing or blank.
                ``read_work_orders`` reads an empty ``asset_id`` as "no
                filter", so the read would return every completed work order
                in the account as this asset's history.
        """
        self._ensure_connected()
        if asset_id is None or not str(asset_id).strip():
            raise ConnectorError("read_maintenance_history requires an asset_id")
        # As text: a numeric ID such as 0 is falsy, and read_work_orders would
        # drop the filter for it.
        return await self.read_work_orders(asset_id=str(asset_id), status="complete")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Build HTTP headers with auth and content-type."""
        return self._auth.apply({"Content-Type": "application/json"})

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")

    async def _read_asset_spare_parts(self, asset_id: str) -> list[SparePart]:
        """Read the parts an asset lists: the asset, then each listed part.

        The asset's ``parts`` list is checked in full before the first part
        request, and any part that cannot be read refuses the whole read, so
        a partial list is never returned as the asset's complete one.
        """
        asset_path = f"/api/v2/assets/{path_segment(asset_id)}"
        # path_segment sends a numeric ID as its digits, so the record's id is
        # matched against the ID as text.
        wanted_id = str(asset_id)
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client, "GET", f"{self.url}{asset_path}", headers=self._headers()
            )
            if resp.status_code == 404:
                _log_spare_parts_refused(asset_id, "asset_not_found")
                raise ConnectorError("UpKeep asset not found: cannot read its spare parts")
            _raise_for_record_status(resp.status_code, "asset")
            try:
                # path_segment raises ConnectorError for an ID that cannot be
                # one path segment.
                listed = [
                    (part_id, f"/api/v2/parts/{path_segment(part_id)}")
                    for part_id in upkeep_mapper.parse_asset_part_ids(resp.json(), wanted_id)
                ]
            except (ValueError, TypeError, ConnectorError) as exc:
                _log_spare_parts_refused(asset_id, "malformed_asset")
                raise ConnectorSchemaError(
                    "UpKeep did not return a well-formed record of the asset: "
                    "cannot read its spare parts"
                ) from exc
            if len(listed) > _MAX_ASSET_PARTS:
                _log_spare_parts_refused(asset_id, "too_many_asset_parts", part_count=len(listed))
                raise ConnectorError(
                    f"The UpKeep asset lists more than {_MAX_ASSET_PARTS} parts, "
                    "the most read_spare_parts fetches for one asset"
                )
            parts: list[SparePart] = []
            for position, (part_id, part_path) in enumerate(listed):
                resp = await request_with_retry(
                    client, "GET", f"{self.url}{part_path}", headers=self._headers()
                )
                if resp.status_code == 404:
                    _log_spare_parts_refused(asset_id, "listed_part_not_found", position=position)
                    raise ConnectorError(
                        "UpKeep could not return a part the asset lists (HTTP 404): "
                        "its spare parts cannot be read in full, so none are returned"
                    )
                _raise_for_record_status(resp.status_code, "part")
                try:
                    record = upkeep_mapper.parse_part_record(resp.json())
                except (ValueError, TypeError) as exc:
                    _log_spare_parts_refused(asset_id, "malformed_part", position=position)
                    raise ConnectorSchemaError(
                        "UpKeep returned a malformed record for a part the asset lists: "
                        "its spare parts cannot be read in full, so none are returned"
                    ) from exc
                if record["id"] != part_id:
                    _log_spare_parts_refused(asset_id, "part_id_mismatch", position=position)
                    raise ConnectorSchemaError(
                        "UpKeep returned a different part than the one the asset lists: "
                        "its spare parts cannot be read in full, so none are returned"
                    )
                parts.append(upkeep_mapper.parse_spare_part(record))
        return parts

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

    async def _read_pm_templates(self) -> list[dict[str, Any]]:
        """Fetch every PM template, schedules expanded, from ``GET /api/v2/pm``.

        Unlike the offset/limit endpoints, ``/api/v2/pm`` numbers pages from
        1 (``page`` and ``pageSize`` go together) and wraps results in
        ``{"data": [...], "pagination": {...}}``; a page shorter than
        ``pageSize`` is the last. Without ``includes=schedules`` each
        template lists only its schedule IDs.
        """
        httpx = _require_httpx()
        path = "/api/v2/pm"
        templates: list[dict[str, Any]] = []
        page = 1
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                resp = await request_with_retry(
                    client,
                    "GET",
                    f"{self.url}{path}",
                    headers=self._headers(),
                    params={
                        "includes": "schedules",
                        "page": str(page),
                        "pageSize": str(self._PAGE_SIZE),
                    },
                )
                if resp.status_code == 401:
                    raise ConnectorAuthError("UpKeep API key is invalid")
                if resp.status_code != 200:
                    raise ConnectorError(f"UpKeep GET {path} failed: HTTP {resp.status_code}")
                data = resp.json().get("data", [])
                templates.extend(data)
                if len(data) < self._PAGE_SIZE:
                    break
                page += 1
        logger.debug(
            "pm_templates_read",
            connector="UpKeepConnector",
            operation="read_maintenance_plans",
            total=len(templates),
        )
        return templates
