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
from machina.connectors.cmms._http_errors import WRITE_REQUEST_NOT_SENT, rest_errors, status_error
from machina.connectors.cmms.auth import ApiKeyHeaderAuth
from machina.connectors.cmms.filters import spare_part_filters
from machina.connectors.cmms.mappers import upkeep as upkeep_mapper
from machina.connectors.cmms.paths import path_segment
from machina.connectors.cmms.retry import request_with_retry
from machina.connectors.cmms.status import status_not_applied
from machina.domain.work_order import (
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)
from machina.exceptions import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorSchemaError,
    ConnectorUnsupportedFilterError,
)

if TYPE_CHECKING:
    from machina.domain.asset import Asset
    from machina.domain.maintenance_plan import MaintenancePlan
    from machina.domain.spare_part import SparePart

logger = structlog.get_logger(__name__)

# The most parts read_spare_parts(asset_id=...) fetches for one asset, one
# request each: an asset listing more is refused before any part request.
_MAX_ASSET_PARTS = 100

# What a retry of a listed part's request logs in place of its URL, which
# carries the part's ID.
_LISTED_PART_LOG_URL = "/api/v2/parts/<listed part>"

# The errors mapping a vendor value can raise. pydantic's ValidationError is a
# ValueError; ArithmeticError covers int() of an infinite number.
_UNMAPPABLE = (ValueError, TypeError, AttributeError, ArithmeticError)

_MALFORMED_PART = "UpKeep returned a malformed part record: cannot read its spare parts"
_MALFORMED_LISTED_PART = (
    "UpKeep returned a malformed record for a part the asset lists: "
    "its spare parts cannot be read in full, so none are returned"
)


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


def _log_spare_parts_refused(reason: str, **context: Any) -> None:
    """Log why a spare-parts read was refused, without part IDs, SKUs or URLs."""
    logger.warning(
        "upkeep_spare_parts_refused",
        connector="UpKeepConnector",
        operation="read_spare_parts",
        reason=reason,
        **context,
    )


def _map_spare_part(record: Any, message: str, **log_context: Any) -> SparePart:
    """Map an UpKeep part record, refusing the read when a field cannot be mapped."""
    try:
        return upkeep_mapper.parse_spare_part(record)
    except _UNMAPPABLE as exc:
        error_type = type(exc).__name__
    _log_spare_parts_refused("malformed_part", error_type=error_type, **log_context)
    # Raised outside the except block: the mapping error's message quotes the
    # vendor value, and `from None` would still keep it as __context__.
    raise ConnectorSchemaError(message)


def _map_work_orders(records: list[Any], operation: str, **log_context: Any) -> list[WorkOrder]:
    """Map UpKeep work orders, refusing the read when one has no Machina status.

    A work order on hold, or one without a status UpKeep documents, cannot be
    returned with a status that is not its own (an on-hold one read as
    ASSIGNED, an unknown one as CREATED), and leaving it out would hide it. So
    the whole read is refused; the message and the log give counts, never IDs.
    """
    refused = {"on_hold": 0, "undocumented": 0}
    for record in records:
        reason = upkeep_mapper.status_refusal(
            record.get("status") if isinstance(record, dict) else None
        )
        if reason is not None:
            refused[reason] += 1
    if not any(refused.values()):
        return [upkeep_mapper.parse_work_order(record) for record in records]
    logger.warning(
        "upkeep_work_orders_refused",
        connector="UpKeepConnector",
        operation=operation,
        on_hold=refused["on_hold"],
        undocumented_status=refused["undocumented"],
        **log_context,
    )
    found = []
    if refused["on_hold"]:
        found.append(f"{refused['on_hold']} on hold (onHold), which Machina has no status for")
    if refused["undocumented"]:
        found.append(f"{refused['undocumented']} without a status UpKeep documents")
    advice = "Read the others by status (created, in_progress, completed or closed)"
    if refused["on_hold"]:
        advice += "; a work order on hold can be read again once it is resumed in UpKeep"
    raise ConnectorSchemaError(
        f"UpKeep returned work orders that no Machina status describes: {'; '.join(found)}. "
        "Reading them as another status would misreport them, so no work orders are "
        f"returned. {advice}."
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

    # read_spare_parts(asset_id=...) sends one request per part the asset
    # lists (1 + k), so the agent's per-turn prefetch gives it a deadline.
    spare_parts_by_asset_fans_out: ClassVar[bool] = True

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
            ConnectorTimeoutError: If the API does not answer in time.
            ConnectorConfigError: If ``url`` is malformed.
            ConnectorError: If the API is unreachable.
        """
        if not self._auth.value:
            raise ConnectorAuthError("UpKeep API key is required")
        httpx = _require_httpx()
        with rest_errors("UpKeep", "health check"):
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
        url = f"{self.url}/api/v2/assets/{path_segment(asset_id)}"
        with rest_errors("UpKeep", "GET asset"):
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await request_with_retry(client, "GET", url, headers=self._headers())
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
            status: Filter by status — a :class:`WorkOrderStatus` or one of its
                values such as ``"in_progress"`` (reverse-mapped to UpKeep's
                status), or ``open``, ``inProgress`` or ``complete`` in any
                case, sent as UpKeep spells it (``"inProgress"``). ``completed`` and ``closed``
                both select UpKeep's ``complete``.

        Raises:
            ConnectorUnsupportedFilterError: Before any request, if ``status``
                is ``assigned`` or ``cancelled``, which UpKeep has no status
                for, ``onHold``, whose work orders have no Machina status, or
                any other value that is neither a Machina status nor an
                UpKeep one.
            ConnectorSchemaError: If UpKeep returns a work order on hold, or
                one without a status UpKeep documents: none is returned, as no
                Machina status describes it (see :func:`_map_work_orders`).
        """
        self._ensure_connected()
        params: dict[str, str] = {}
        if asset_id:
            params["asset"] = asset_id
        if status:
            try:
                params["status"] = upkeep_mapper.reverse_status_filter(status)
            except ValueError as exc:
                raise ConnectorUnsupportedFilterError(f"read_work_orders: {exc}") from None
        raw = await self._paginated_get("/api/v2/work-orders", params=params)
        return _map_work_orders(raw, "read_work_orders", asset_id=asset_id or None)

    async def get_work_order(self, work_order_id: str) -> WorkOrder | None:
        """Look up a single work order by ID."""
        self._ensure_connected()
        httpx = _require_httpx()
        url = f"{self.url}/api/v2/work-orders/{path_segment(work_order_id)}"
        with rest_errors("UpKeep", "GET work order"):
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await request_with_retry(client, "GET", url, headers=self._headers())
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                raise ConnectorError(f"UpKeep GET work order failed: HTTP {resp.status_code}")
            body = resp.json()
        result = body.get("result", body)
        return _map_work_orders([result], "get_work_order", work_order_id=work_order_id)[0]

    @sandbox_aware
    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Create a new work order in UpKeep.

        Args:
            work_order: Machina :class:`WorkOrder` to create.

        Returns:
            The created work order with the server-assigned ID.

        Raises:
            ConnectorAuthError: If UpKeep rejects the API key (401) or
                answers 403.
            ConnectorError: If UpKeep refuses the create (another 4xx), or
                it fails before the request is sent (the message says the
                write request was not sent). Any other failure (a 3xx or 5xx, a timeout or a broken
                connection after sending began, a success response that is
                not JSON) leaves the outcome unknown, and the message says
                to check UpKeep before retrying; a timeout is a
                :class:`~machina.exceptions.ConnectorTimeoutError`. A JSON
                success response that cannot be mapped means the work order
                was created, and the message says not to repeat the create.
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
        operation = "create work order"
        with rest_errors("UpKeep", operation, write=True, not_sent=WRITE_REQUEST_NOT_SENT):
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
                raise status_error("UpKeep", operation, resp.status_code, write=True)
            body = resp.json()
        # The work order exists by now: a response that cannot be mapped (no
        # status UpKeep documents, say) must not look like a failed create, or a
        # caller would create it again.
        try:
            result = body.get("result", body)
            logger.info(
                "work_order_created",
                connector="UpKeepConnector",
                work_order_id=result.get("id"),
                asset_id=work_order.asset_id,
            )
            return upkeep_mapper.parse_work_order(result)
        except (*_UNMAPPABLE, AttributeError) as exc:
            error_type = type(exc).__name__
        logger.warning(
            "work_order_create_not_read",
            connector="UpKeepConnector",
            operation="create_work_order",
            asset_id=work_order.asset_id,
            error_type=error_type,
        )
        # Raised outside the except block: a mapping error can quote the vendor value.
        raise ConnectorError(
            "UpKeep created the work order, but its response could not be read — do not "
            "repeat the create; find the work order in UpKeep"
        )

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

        Only non-``None`` fields are included in the PATCH payload. The work
        order is read back afterwards, and a status UpKeep did not apply is
        an error, not a success.

        Args:
            work_order_id: UpKeep work order ID.
            status: New status (reverse-mapped to UpKeep string). ``CLOSED``
                sets ``complete``, UpKeep's one finished state, so the work
                order reads back as ``COMPLETED``. ``ASSIGNED`` and
                ``CANCELLED`` have no UpKeep status and are refused.
            assigned_to: The ID of the user to assign, sent as
                ``assignedToUser``, the field UpKeep documents for it: assigning
                a technician, which UpKeep keeps apart from the status.
            description: New title.

        Returns:
            The updated work order.

        Raises:
            ConnectorError: If no field is given; if ``status`` is
                ``ASSIGNED`` or ``CANCELLED``, which UpKeep has no status for,
                before any request and with no field sent; if UpKeep rejects
                the update (a 4xx; 401 and 403 as ``ConnectorAuthError``) or
                it fails before the request is sent; if the outcome is
                unknown (a 3xx or 5xx, or a timeout or a broken connection
                after sending began), in which case the message says to
                check UpKeep before retrying; if the work order cannot be
                read back after it, in which case the message says the
                update was applied and must not be repeated; or if it reads
                back with another status, or another assignee, than the one
                sent.
        """
        self._ensure_connected()
        httpx = _require_httpx()
        payload: dict[str, Any] = {}
        if status is not None:
            try:
                payload["status"] = upkeep_mapper.reverse_status(status)
            except ValueError as exc:
                raise ConnectorError(str(exc)) from None
        if assigned_to is not None:
            payload["assignedToUser"] = assigned_to
        if description is not None:
            payload["title"] = description
        if not payload:
            raise ConnectorError("update_work_order requires at least one field to update")
        url = f"{self.url}/api/v2/work-orders/{path_segment(work_order_id)}"
        operation = "update work order"
        with rest_errors("UpKeep", operation, write=True, not_sent=WRITE_REQUEST_NOT_SENT):
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await request_with_retry(
                    client, "PATCH", url, headers=self._headers(), json=payload
                )
        if resp.status_code == 401:
            raise ConnectorAuthError("UpKeep API key is invalid")
        if resp.status_code not in (200, 204):
            raise status_error("UpKeep", operation, resp.status_code, write=True)
        logger.info(
            "work_order_updated",
            connector="UpKeepConnector",
            operation="update_work_order",
            work_order_id=work_order_id,
        )
        # Read the work order back. The update has been applied by now: whatever
        # goes wrong next must not look like a failed update, or a caller would
        # send it again.
        try:
            updated = await self.get_work_order(work_order_id)
            if updated is None:
                raise ConnectorError("the work order was not found")
        except Exception as exc:
            logger.warning(
                "work_order_update_not_read_back",
                connector="UpKeepConnector",
                operation="update_work_order",
                work_order_id=work_order_id,
                error_type=type(exc).__name__,
            )
            if isinstance(exc, ConnectorSchemaError):
                # Read back on hold, or with an undocumented status: reading it
                # again would be refused the same way, so do not suggest it.
                raise ConnectorError(
                    f"UpKeep accepted the update of work order {work_order_id}, but the work "
                    "order now has a status no Machina status describes (on hold, for "
                    "instance), so it is not returned and the update cannot be confirmed "
                    "— do not repeat the update; check the work order in UpKeep"
                ) from None
            raise ConnectorError(
                f"UpKeep applied the update to work order {work_order_id}, but reading it "
                "back failed — do not repeat the update; read the work order to check it"
            ) from None
        if "status" in payload:
            # The status UpKeep reports for the value sent: CLOSED reads back
            # as COMPLETED, since both are UpKeep's "complete".
            sent = upkeep_mapper.map_status(payload["status"])
            if updated.status != sent:
                raise status_not_applied(
                    "UpKeepConnector", work_order_id, sent.value, updated.status.value
                )
        # As WorkOrder stores it: stripped, and an empty assignee as none.
        if assigned_to is not None and updated.assigned_to != (assigned_to.strip() or None):
            # An assignee UpKeep accepted but did not set must not read as done.
            logger.warning(
                "work_order_assignee_not_applied",
                connector="UpKeepConnector",
                operation="update_work_order",
                work_order_id=work_order_id,
            )
            raise ConnectorError(
                f"UpKeep accepted the update of work order {work_order_id}, but its assignee "
                "(assignedToUser) does not read back as the one sent: the assignment was not "
                "applied. Read the work order before repeating the update."
            )
        return updated

    @sandbox_aware
    async def close_work_order(self, work_order_id: str) -> WorkOrder:
        """Transition a work order to CLOSED (maps to 'complete' in UpKeep)."""
        return await self.update_work_order(work_order_id, status=WorkOrderStatus.CLOSED)  # type: ignore[no-any-return]

    @sandbox_aware
    async def cancel_work_order(self, work_order_id: str) -> WorkOrder:
        """Refuse: UpKeep has no cancelled status.

        Raises:
            ConnectorError: Always, before any request (see
                :meth:`update_work_order`).
        """
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
            ConnectorError: If a filter is neither ``""`` nor an ID (see
                :func:`~machina.connectors.cmms.filters.spare_part_filters`),
                the asset ID cannot be one URL path segment, UpKeep has no
                such asset, the asset lists more than 100 parts, UpKeep
                cannot return a part the asset lists (HTTP 404), or a request
                answers another error status.
            ConnectorAuthError: If UpKeep rejects the API key.
            ConnectorSchemaError: If UpKeep does not return a well-formed
                record of the asset (another record, or a malformed ``parts``
                list), a part record is malformed or has fields that cannot be
                mapped, or a part request returns another part. No partial
                list is returned.
            ConnectorTimeoutError: If a request still times out after its
                retries; any other transport failure is a ``ConnectorError``.
                No partial list is returned.

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
        asset_id, sku = spare_part_filters(asset_id, sku)
        if asset_id:
            parts = await self._read_asset_spare_parts(asset_id)
        else:
            parts = [
                _map_spare_part(item, _MALFORMED_PART, position=position)
                for position, item in enumerate(await self._paginated_get("/api/v2/parts"))
            ]
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
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=30.0) as client:
            with rest_errors("UpKeep", "GET asset"):
                resp = await request_with_retry(
                    client, "GET", f"{self.url}{asset_path}", headers=self._headers()
                )
            if resp.status_code == 404:
                _log_spare_parts_refused("asset_not_found", asset_id=asset_id)
                raise ConnectorError("UpKeep asset not found: cannot read its spare parts")
            _raise_for_record_status(resp.status_code, "asset")
            try:
                # path_segment raises ConnectorError for an ID that cannot be
                # one path segment.
                listed = [
                    (part_id, f"/api/v2/parts/{path_segment(part_id)}")
                    for part_id in upkeep_mapper.parse_asset_part_ids(resp.json(), asset_id)
                ]
            except (ValueError, TypeError, ConnectorError) as exc:
                _log_spare_parts_refused("malformed_asset", asset_id=asset_id)
                raise ConnectorSchemaError(
                    "UpKeep did not return a well-formed record of the asset: "
                    "cannot read its spare parts"
                ) from exc
            if len(listed) > _MAX_ASSET_PARTS:
                _log_spare_parts_refused(
                    "too_many_asset_parts", asset_id=asset_id, part_count=len(listed)
                )
                raise ConnectorError(
                    f"The UpKeep asset lists more than {_MAX_ASSET_PARTS} parts, "
                    "the most read_spare_parts fetches for one asset"
                )
            parts: list[SparePart] = []
            for position, (part_id, part_path) in enumerate(listed):
                with rest_errors("UpKeep", "GET part"):
                    resp = await request_with_retry(
                        client,
                        "GET",
                        f"{self.url}{part_path}",
                        headers=self._headers(),
                        log_url=_LISTED_PART_LOG_URL,
                    )
                if resp.status_code == 404:
                    _log_spare_parts_refused(
                        "listed_part_not_found", asset_id=asset_id, position=position
                    )
                    raise ConnectorError(
                        "UpKeep could not return a part the asset lists (HTTP 404): "
                        "its spare parts cannot be read in full, so none are returned"
                    )
                _raise_for_record_status(resp.status_code, "part")
                try:
                    returned_id, record = upkeep_mapper.parse_part_record(resp.json())
                except (ValueError, TypeError) as exc:
                    _log_spare_parts_refused(
                        "malformed_part", asset_id=asset_id, position=position
                    )
                    raise ConnectorSchemaError(_MALFORMED_LISTED_PART) from exc
                if returned_id != part_id:
                    _log_spare_parts_refused(
                        "part_id_mismatch", asset_id=asset_id, position=position
                    )
                    raise ConnectorSchemaError(
                        "UpKeep returned a different part than the one the asset lists: "
                        "its spare parts cannot be read in full, so none are returned"
                    )
                parts.append(
                    _map_spare_part(
                        record, _MALFORMED_LISTED_PART, asset_id=asset_id, position=position
                    )
                )
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
        with rest_errors("UpKeep", f"GET {path}"):
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
        with rest_errors("UpKeep", f"GET {path}"):
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
