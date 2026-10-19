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

The vendor payload ↔ Machina entity mapping lives as pure functions in
:mod:`machina.connectors.cmms.mappers.maximo` so it can be unit-tested
without HTTP mocks.

Caller-supplied filter values (asset, work order and item numbers, status
codes) reach an ``oslc.where`` clause only through :func:`_oslc_eq`, which
refuses any value Maximo's query syntax would read as more than a literal.

See also:
    https://developer.ibm.com/apis/catalog/maximo--maximo-manage-rest-api/Introduction
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any, ClassVar
from urllib.parse import urljoin, urlsplit

import structlog
from pydantic import Field

from machina.connectors._settings import validate_setting
from machina.connectors.base import ConnectorHealth, ConnectorStatus, sandbox_aware
from machina.connectors.capabilities import Capability
from machina.connectors.cmms.auth import ApiKeyHeaderAuth, BasicAuth, BearerAuth
from machina.connectors.cmms.mappers import maximo as maximo_mapper
from machina.connectors.cmms.paths import path_segment
from machina.connectors.cmms.retry import request_with_retry
from machina.domain.work_order import WorkOrder, WorkOrderStatus
from machina.exceptions import ConnectorAuthError, ConnectorError

if TYPE_CHECKING:
    from machina.domain.asset import Asset, AssetType
    from machina.domain.maintenance_plan import MaintenancePlan
    from machina.domain.spare_part import SparePart

logger = structlog.get_logger(__name__)

_AuthUnion = ApiKeyHeaderAuth | BasicAuth | BearerAuth
# The same union keyed by ``type``, for ``auth`` given as a machina.yaml dict.
_AuthSetting = Annotated[_AuthUnion, Field(discriminator="type")]

# Object-structure names (``mxwo``, ``MXASSET``, a site's own ``ZZ_WO``) are
# letters, digits and ``_``; a name that matches cannot add path segments, dot
# segments or a query to the URL it is placed in.
_OBJECT_STRUCTURE_RE = re.compile(r"[A-Za-z0-9_]+")


def _oslc_resource_path(object_structure: object, resource_id: object) -> str:
    """Build ``/maximo/oslc/os/{object_structure}/{resource_id}`` for one resource.

    Both values reach this path from MCP-client input, and httpx removes dot
    segments, so ``object_structure`` must be an object-structure name and
    ``resource_id`` goes through :func:`~machina.connectors.cmms.paths.path_segment`:
    neither can address another Maximo endpoint or the collection itself. The
    check needs no connection or request, so the raw vendor tool also runs it
    before its sandbox short-circuit.

    Raises:
        ConnectorError: If ``object_structure`` is not ASCII letters, digits
            and ``_``, or ``resource_id`` could leave its path segment. Neither
            message repeats the value: it reaches the MCP client.
    """
    if not isinstance(object_structure, str) or not _OBJECT_STRUCTURE_RE.fullmatch(
        object_structure
    ):
        raise ConnectorError(
            "Invalid Maximo object structure: expected an object structure name "
            "such as 'mxwo' (ASCII letters, digits and underscores only)"
        )
    try:
        segment = path_segment(resource_id)
    except UnicodeEncodeError:
        # A lone surrogate ("\ud800" in JSON survives some MCP transports) has
        # no UTF-8 form; the codec's own message would quote the character.
        raise ConnectorError(
            "Invalid record ID: an ID must be text that can be sent as UTF-8"
        ) from None
    return f"/maximo/oslc/os/{object_structure}/{segment}"


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


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str | None, int | None]:
    """Return the scheme, host and port of ``url``, the port defaulted by scheme.

    Raises:
        ValueError: If ``url`` cannot be split or its port is not a valid number.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, parts.hostname, parts.port or _DEFAULT_PORTS.get(scheme)


def _next_page_url(next_page: object, current_url: str) -> str | None:
    """Return the URL of the next OSLC page, or ``None`` after the last page.

    Maximo's JSON API gives the link as ``responseInfo.nextPage`` =
    ``{"href": "<url>"}``, the field IBM's own REST clients follow, and leaves
    ``nextPage`` out on the last page. A bare URL string is followed too, and a
    relative link is resolved against ``current_url``.

    Every page is requested with the connector's credentials, so the link must
    keep the scheme, host and port of ``current_url``. Maximo builds its links
    from its own configured address, which behind a proxy can be an internal
    host or plain HTTP. Any other value is refused rather than read as the last
    page, which would return the pages read so far as the whole result.

    Raises:
        ConnectorError: If ``nextPage`` carries no URL, or its URL has another
            scheme, host or port. Neither message repeats the URL, which can
            name an internal host.
    """
    if next_page is None:
        return None
    if isinstance(next_page, dict):
        next_page = next_page.get("href")
    if not isinstance(next_page, str) or not next_page.strip():
        raise ConnectorError("Maximo returned a nextPage link without an href URL")
    try:
        url = urljoin(current_url, next_page.strip())
        same_origin = _origin(url) == _origin(current_url)
    except ValueError:
        same_origin = False
    if not same_origin:
        raise ConnectorError(
            "Maximo returned a nextPage link to another scheme, host or port than "
            "the connector URL; refusing to send the connector's credentials there"
        )
    return url


# Characters an oslc.where string value cannot carry. IBM documents no escape
# syntax inside the quotes, and Maximo maps the clause onto its QBE framework,
# which reads these as query syntax: the literal's own quote and the OSLC escape
# character, the % * ? wildcards (a bare "*" is the not-null test), comma and
# bracket value lists, and the = ! < > ~ operators (~null~ matches empty fields).
_OSLC_RESERVED_CHARS = frozenset('"\\%*?,[]=!<>~')


def _oslc_eq(attribute: str, value: object) -> str:
    """Build the exact-match ``oslc.where`` term ``attribute="value"``.

    Values reach these terms from LLM and MCP-client input. Maximo documents no
    escape inside a where literal, so a value its query syntax could read as
    more than a literal is refused rather than escaped: one containing a
    character of :data:`_OSLC_RESERVED_CHARS` or a non-printable character (a
    server-side trim could remove it in front of an operator), a blank value
    (an empty QBE value constrains nothing, so the term would stop filtering),
    or QBE's ``null`` keyword. Anything else is matched exactly, which is why
    ``_`` passes: it is a wildcard only inside a LIKE match, and only ``%``
    asks for one. A non-string value (a number from a workflow event or model
    JSON) is formatted with ``str()`` first.

    Raises:
        ConnectorError: If the value cannot be expressed as an exact match.
            The message names the offending characters or rule, never the
            value: it can reach the LLM as a tool result.
    """
    text = str(value)
    stripped = text.strip()
    reserved = sorted(set(text) & _OSLC_RESERVED_CHARS)
    if not stripped:
        problem = "is blank"
    elif stripped.lower() == "null":
        problem = "is the QBE keyword 'null'"
    elif reserved:
        problem = "contains " + ", ".join(map(repr, reserved))
    elif not text.isprintable():
        problem = "contains a non-printable character"
    else:
        return f'{attribute}="{text}"'
    raise ConnectorError(
        f"Invalid {attribute} for a Maximo query: the value {problem}. oslc.where has "
        "no escape, so a value to match exactly cannot be blank or 'null', or "
        "contain a non-printable character or any of: " + " ".join(sorted(_OSLC_RESERVED_CHARS))
    )


class MaximoConnector:
    """Connector for IBM Maximo Manage.

    Provides integration with Maximo's OSLC/JSON REST API for reading
    and creating maintenance data.

    Args:
        url: Base URL of the Maximo instance
            (e.g. ``https://maximo.example.com``).
        auth: Authentication strategy — :class:`ApiKeyHeaderAuth`,
            :class:`BasicAuth`, or :class:`BearerAuth`, or the equivalent
            dict selected by ``type`` (``api_key``, ``basic``, ``bearer``) as
            a ``machina.yaml`` settings block carries it.
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
            Capability.GET_WORK_ORDER,
            Capability.CREATE_WORK_ORDER,
            Capability.UPDATE_WORK_ORDER,
            Capability.READ_SPARE_PARTS,
            Capability.READ_MAINTENANCE_PLANS,
            Capability.READ_MAINTENANCE_HISTORY,
        }
    )

    _PAGE_SIZE: ClassVar[int] = 100
    # Cap on rows accumulated across the pages of one read, as SAP PM's
    # _MAX_ODATA_ROWS: paging chunks the fetch but does not bound memory.
    _MAX_OSLC_ROWS: ClassVar[int] = 50_000

    def __init__(
        self,
        *,
        url: str,
        auth: _AuthUnion | dict[str, Any],
        lean: bool = True,
        asset_type_map: dict[str, AssetType] | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        # From machina.yaml, auth arrives as a dict keyed by ``type``
        # (``api_key``, ``basic`` or ``bearer``); validate it.
        self._auth: _AuthUnion = (
            validate_setting(_AuthSetting, auth, "auth") if isinstance(auth, dict) else auth
        )
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
        return [maximo_mapper.parse_asset(item, self._asset_type_map) for item in raw]

    async def get_asset(self, asset_id: str) -> Asset | None:
        """Look up a single asset by asset number."""
        self._ensure_connected()
        raw = await self._oslc_get(
            "mxasset",
            oslc_where=_oslc_eq("assetnum", asset_id),
            page_size=1,
        )
        return maximo_mapper.parse_asset(raw[0], self._asset_type_map) if raw else None

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
            clauses.append(_oslc_eq("assetnum", asset_id))
        if status:
            maximo_status = (
                maximo_mapper.REVERSE_MAXIMO_STATUS.get(status, status.value.upper())
                if isinstance(status, WorkOrderStatus)
                else status.upper()
            )
            clauses.append(_oslc_eq("status", maximo_status))
        where = " and ".join(clauses) if clauses else ""
        raw = await self._oslc_get("mxwo", oslc_where=where)
        return [maximo_mapper.parse_work_order(item) for item in raw]

    async def get_work_order(self, work_order_id: str) -> WorkOrder | None:
        """Look up a single work order by work order number."""
        self._ensure_connected()
        raw = await self._oslc_get(
            "mxwo",
            oslc_where=_oslc_eq("wonum", work_order_id),
            page_size=1,
        )
        return maximo_mapper.parse_work_order(raw[0]) if raw else None

    @sandbox_aware
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
            "worktype": maximo_mapper.reverse_worktype(work_order.type),
            "wopriority": maximo_mapper.reverse_priority(work_order.priority),
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
        return maximo_mapper.parse_work_order(body)

    @sandbox_aware
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
            payload["status"] = maximo_mapper.reverse_status(status)
        if assigned_to is not None:
            payload["lead"] = assigned_to
        if description is not None:
            payload["description"] = description
        if not payload:
            raise ConnectorError("update_work_order requires at least one field to update")
        # The read-back after the PATCH looks the order up through oslc.where:
        # refuse an ID it could not express before writing, not after.
        _oslc_eq("wonum", work_order_id)
        headers = {**self._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client,
                "PATCH",
                f"{self.url}/maximo/oslc/os/mxwo/{path_segment(work_order_id)}",
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

    @sandbox_aware
    async def close_work_order(self, work_order_id: str) -> WorkOrder:
        """Transition a work order to CLOSED status."""
        return await self.update_work_order(work_order_id, status=WorkOrderStatus.CLOSED)  # type: ignore[no-any-return]

    @sandbox_aware
    async def cancel_work_order(self, work_order_id: str) -> WorkOrder:
        """Transition a work order to CANCELLED status."""
        return await self.update_work_order(work_order_id, status=WorkOrderStatus.CANCELLED)  # type: ignore[no-any-return]

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
        where = _oslc_eq("itemnum", sku) if sku else ""
        raw = await self._oslc_get("mxinventory", oslc_where=where)
        return [maximo_mapper.parse_spare_part(item) for item in raw]

    async def read_maintenance_plans(self) -> list[MaintenancePlan]:
        """Read preventive-maintenance triggers from Maximo."""
        self._ensure_connected()
        raw = await self._oslc_get("mxpm")
        return [maximo_mapper.parse_maintenance_plan(item) for item in raw]

    async def read_maintenance_history(self, asset_id: str) -> list[WorkOrder]:
        """Return completed/closed work orders for an asset.

        Raises:
            ConnectorError: If ``asset_id`` is missing or blank. A missing ID
                would otherwise be matched as the text ``"None"``.
        """
        self._ensure_connected()
        if asset_id is None or not str(asset_id).strip():
            raise ConnectorError("read_maintenance_history requires an asset_id")
        # oslc.where has no ``or`` and no grouping parentheses (``and`` is its
        # only boolean operator), so the two statuses are listed through ``in``.
        where = f'{_oslc_eq("assetnum", asset_id)} and status in ["COMP","CLOSE"]'
        raw = await self._oslc_get("mxwo", oslc_where=where)
        return [maximo_mapper.parse_work_order(item) for item in raw]

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
        link in each page (see :func:`_next_page_url`).

        Args:
            object_structure: The Maximo OS name (e.g. ``mxasset``, ``mxwo``).
            oslc_where: Optional OSLC where clause for server-side filtering.
            oslc_select: Optional comma-separated field list.
            page_size: Results per page (default :attr:`_PAGE_SIZE`).

        Returns:
            Flat list of all result dicts across all pages.

        Raises:
            ConnectorError: On an HTTP error, a next-page link that cannot be
                followed or repeats an earlier one, or more than
                :attr:`_MAX_OSLC_ROWS` rows.
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
        followed: set[str] = set()

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
                if len(all_items) > self._MAX_OSLC_ROWS:
                    raise ConnectorError(
                        f"Maximo GET {object_structure} exceeded the "
                        f"{self._MAX_OSLC_ROWS}-row safety cap — refusing to load an "
                        "unbounded result set into memory. Narrow the query with oslc.where."
                    )
                # Follow the OSLC next-page link. It repeats the query, so the
                # first request's params are not sent again.
                url = _next_page_url(body.get("responseInfo", {}).get("nextPage"), url)
                if url in followed:
                    raise ConnectorError(
                        f"Maximo GET {object_structure} returned a nextPage link it had "
                        "already returned — refusing to read the same page again"
                    )
                if url is not None:
                    followed.add(url)
                params = None
        logger.debug(
            "oslc_get",
            connector="MaximoConnector",
            object_structure=object_structure,
            total=len(all_items),
        )
        return all_items

    @sandbox_aware
    async def _patch_resource(
        self, object_structure: str, resource_id: str, attributes: dict[str, Any]
    ) -> Any:
        """PATCH raw attributes onto one resource of an OSLC object structure.

        The write path of the ``maximo_raw_attribute_update`` MCP vendor tool.
        Both values come from MCP-client input; :func:`_oslc_resource_path`
        keeps the PATCH on one resource of one object structure, and checks
        them again here so a direct call is held to the same rule. Like the
        connector's other writes, it needs a connected connector, retries a
        429 and never replays the PATCH after a network error or a 503.

        Args:
            object_structure: OSLC object-structure name, such as ``mxwo``.
            resource_id: Rest ID of the resource, the last segment of its href.
            attributes: Attribute names and new values, sent as the JSON body.

        Returns:
            The final httpx response, unchecked, for the caller to report.

        Raises:
            SandboxViolationError: In sandbox mode, before anything else.
            ConnectorError: If the connector is not connected, or
                ``object_structure`` or ``resource_id`` is refused by
                :func:`_oslc_resource_path`; both before any request.
            httpx.HTTPError: A transport error from the PATCH, re-raised
                without a retry.
        """
        self._ensure_connected()
        url = f"{self.url}{_oslc_resource_path(object_structure, resource_id)}"
        httpx = _require_httpx()
        headers = {**self._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await request_with_retry(client, "PATCH", url, headers=headers, json=attributes)
