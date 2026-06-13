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
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from machina.connectors.cmms.generic_schema import GenericCmmsYamlConfig

import jmespath
import structlog
from pydantic import ValidationError

from machina.connectors.base import ConnectorHealth, ConnectorStatus, sandbox_aware
from machina.connectors.capabilities import Capability
from machina.connectors.cmms.auth import (
    ApiKeyHeaderAuth,
    BasicAuth,
    BearerAuth,
    NoAuth,
)
from machina.connectors.cmms.generic_coercers import (
    COERCER_REGISTRY as _YAML_COERCERS,
)
from machina.connectors.cmms.generic_coercers import resolve_path
from machina.connectors.cmms.pagination import (
    CursorPagination,
    NoPagination,
    OffsetLimitPagination,
    PageNumberPagination,
)
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.failure_mode import FailureMode
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import (
    FailureImpact,
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)
from machina.exceptions import ConnectorAuthError, ConnectorError

logger = structlog.get_logger(__name__)

# Plain unions (not Annotated) for use as runtime type annotations.
# The Annotated discriminated unions live in auth.py / pagination.py for
# pydantic serialization purposes.
_AuthUnion = BearerAuth | BasicAuth | ApiKeyHeaderAuth | NoAuth
_PaginationUnion = NoPagination | OffsetLimitPagination | PageNumberPagination | CursorPagination


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

    _BASE_CAPABILITIES: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.READ_ASSETS,
            Capability.READ_WORK_ORDERS,
            Capability.CREATE_WORK_ORDER,
            Capability.READ_SPARE_PARTS,
            Capability.READ_MAINTENANCE_HISTORY,
        }
    )

    # Maps optional capabilities to the endpoint config key that enables them.
    _OPTIONAL_CAPABILITIES: ClassVar[dict[Capability, str]] = {
        Capability.GET_WORK_ORDER: "get_work_order",
        Capability.UPDATE_WORK_ORDER: "update_work_order",
        Capability.CLOSE_WORK_ORDER: "update_work_order",
        Capability.CANCEL_WORK_ORDER: "update_work_order",
        Capability.READ_MAINTENANCE_PLANS: "read_maintenance_plans",
    }

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Return capabilities based on configuration.

        Base capabilities are always available. Optional capabilities
        are added when running in local mode (all supported) or when
        the corresponding endpoint is configured in REST mode.
        ``READ_FAILURE_MODES`` is stricter: it is declared only when an
        actual catalog source exists (the local ``failure_modes.json``
        file or a configured REST endpoint), so the capability is a true
        signal of "has a catalog" — not merely "running in local mode".
        """
        caps = set(self._BASE_CAPABILITIES)
        for cap, endpoint_key in self._OPTIONAL_CAPABILITIES.items():
            if self._data_dir or endpoint_key in self._endpoints:
                caps.add(cap)
        if self._has_fm_source:
            caps.add(Capability.READ_FAILURE_MODES)
        return frozenset(caps)

    @staticmethod
    def _detect_failure_mode_source(data_dir: Path | None) -> bool:
        """True when a local failure-mode catalog source is present.

        Local mode only: ``read_failure_modes()`` serves the catalog
        loaded from ``failure_modes.json``. REST mode never declares the
        capability — there is no REST fetch implemented yet, and
        declaring a source the read path cannot serve would produce a
        capability that harvests an empty catalog forever.
        """
        return data_dir is not None and (data_dir / "failure_modes.json").exists()

    def __init__(
        self,
        *,
        url: str = "",
        api_key: str = "",
        data_dir: str | Path = "",
        schema_mapping: dict[str, dict[str, Any]] | None = None,
        auth: _AuthUnion | None = None,
        pagination: _PaginationUnion | None = None,
        endpoints: dict[str, dict[str, Any]] | None = None,
        yaml_mapping: GenericCmmsYamlConfig | None = None,
    ) -> None:
        self.url = url
        self._api_key = api_key
        self._data_dir = Path(data_dir) if data_dir else None
        self._schema_mapping = schema_mapping or {}
        self._connected = False
        self._endpoints = endpoints or {}
        self._yaml_mapping = yaml_mapping
        # Snapshot the failure-mode source presence once (refreshed at
        # connect) — a per-access filesystem stat in the capabilities
        # property would let the declared capability flip mid-session
        # while the loaded catalog stays a connect-time snapshot.
        self._has_fm_source = self._detect_failure_mode_source(self._data_dir)

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
        self._maintenance_plans: list[MaintenancePlan] = []
        self._failure_modes: list[FailureMode] = []
        # Serialises the read-check-mutate-persist sequence in local mode so
        # concurrent create/update calls (e.g. via asyncio.gather in AgentTeam)
        # cannot race on the in-memory list or the file write.
        self._local_write_lock = asyncio.Lock()

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

    async def read_failure_modes(self) -> list[FailureMode]:
        """Return all known failure modes.

        In local mode the result reflects what ``failure_modes.json``
        contained at :meth:`connect` time. The runtime's harvest treats
        the not-connected error as "this provider contributes nothing"
        rather than aborting.

        Returns:
            The failure-mode catalog loaded at connect time.

        Raises:
            ConnectorError: If called before :meth:`connect`.
        """
        self._ensure_connected()
        return list(self._failure_modes)

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

    @sandbox_aware
    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Create a new work order.

        In local mode the operation is idempotent on the work-order ID:
        re-creating a WO whose ID already exists returns the existing
        record rather than appending a duplicate. New work orders are
        persisted back to ``work_orders.json`` so changes survive process
        restarts (skipped when an inbound ``schema_mapping`` is configured;
        see :meth:`_persist_work_orders`).
        """
        self._ensure_connected()
        if self._data_dir:
            async with self._local_write_lock:
                existing = next((wo for wo in self._work_orders if wo.id == work_order.id), None)
                if existing is not None:
                    logger.info(
                        "work_order_create_idempotent_hit",
                        connector="GenericCmmsConnector",
                        operation="create_work_order",
                        work_order_id=work_order.id,
                        asset_id=work_order.asset_id,
                    )
                    return existing
                self._work_orders.append(work_order)
                try:
                    await self._persist_work_orders()
                except Exception:
                    # Persist failed (disk full, serialization error): roll the
                    # in-memory append back so the list stays consistent with
                    # disk. Otherwise the WO lives in memory but not on disk —
                    # lost on restart, and the idempotency guard above would
                    # return it as "existing" for a record never durably stored.
                    self._work_orders.pop()
                    raise
            logger.info(
                "work_order_created",
                connector="GenericCmmsConnector",
                operation="create_work_order",
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
    # Work-order lifecycle & maintenance plans
    # ------------------------------------------------------------------

    async def get_work_order(self, work_order_id: str) -> WorkOrder | None:
        """Look up a single work order by ID."""
        self._ensure_connected()
        if self._data_dir:
            for wo in self._work_orders:
                if wo.id == work_order_id:
                    return wo
            return None
        return await self._rest_get_work_order(work_order_id)

    @sandbox_aware
    async def update_work_order(
        self,
        work_order_id: str,
        *,
        status: WorkOrderStatus | None = None,
        assigned_to: str | None = None,
        description: str | None = None,
    ) -> WorkOrder:
        """Update an existing work order.

        In local mode the in-memory work order is mutated directly.
        In REST mode the configured endpoint is called and the work
        order is re-fetched to return fresh state.

        Raises:
            ConnectorError: If the work order is not found, or the
                endpoint is not configured in REST mode.
        """
        self._ensure_connected()
        if self._data_dir:
            async with self._local_write_lock:
                # Snapshot the pre-update state so a persist failure can be
                # rolled back — _local_update_work_order mutates in place, and
                # without rollback the in-memory object would diverge from disk
                # (new state in memory, old state on disk reloaded at restart).
                idx = next(
                    (i for i, wo in enumerate(self._work_orders) if wo.id == work_order_id),
                    None,
                )
                before = self._work_orders[idx].model_copy(deep=True) if idx is not None else None
                updated = self._local_update_work_order(
                    work_order_id,
                    status=status,
                    assigned_to=assigned_to,
                    description=description,
                )
                try:
                    await self._persist_work_orders()
                except Exception:
                    if idx is not None and before is not None:
                        self._work_orders[idx] = before
                    raise
            return updated
        return await self._rest_update_work_order(
            work_order_id,
            status=status,
            assigned_to=assigned_to,
            description=description,
        )

    async def close_work_order(self, work_order_id: str) -> WorkOrder:
        """Transition a work order to CLOSED status."""
        return await self.update_work_order(work_order_id, status=WorkOrderStatus.CLOSED)  # type: ignore[no-any-return]

    async def cancel_work_order(self, work_order_id: str) -> WorkOrder:
        """Transition a work order to CANCELLED status."""
        return await self.update_work_order(work_order_id, status=WorkOrderStatus.CANCELLED)  # type: ignore[no-any-return]

    async def read_maintenance_plans(self) -> list[MaintenancePlan]:
        """Read preventive-maintenance plans.

        In local mode returns plans loaded from ``maintenance_plans.json``.
        In REST mode fetches from the configured endpoint with pagination.
        """
        self._ensure_connected()
        if self._data_dir:
            return list(self._maintenance_plans)
        return await self._rest_read_maintenance_plans()

    # ------------------------------------------------------------------
    # Internal: local work-order updates
    # ------------------------------------------------------------------

    def _local_update_work_order(
        self,
        work_order_id: str,
        *,
        status: WorkOrderStatus | None = None,
        assigned_to: str | None = None,
        description: str | None = None,
    ) -> WorkOrder:
        """Mutate an in-memory work order."""
        for wo in self._work_orders:
            if wo.id == work_order_id:
                if status is not None:
                    try:
                        wo.transition_to(status)
                    except ValueError as exc:
                        raise ConnectorError(str(exc)) from exc
                if assigned_to is not None:
                    wo.assigned_to = assigned_to
                if description is not None:
                    wo.description = description
                logger.info(
                    "work_order_updated",
                    connector="GenericCmmsConnector",
                    operation="update_work_order",
                    work_order_id=work_order_id,
                    asset_id=wo.asset_id,
                )
                return wo
        raise ConnectorError(f"Work order {work_order_id} not found")

    async def _persist_work_orders(self) -> None:
        """Write the in-memory work orders back to ``work_orders.json``.

        Local mode is the demo / offline data source: without write-back,
        ``create_work_order`` and ``update_work_order`` only mutate the
        in-memory list and changes vanish when the process exits — the
        "LIVE mode doesn't change the files" surprise.

        Persistence is skipped when an inbound ``schema_mapping`` /
        ``yaml_mapping`` is configured: the on-disk file is then in the
        external CMMS shape, and writing the domain shape (``model_dump``)
        back would not round-trip through :meth:`_apply_mapping` on the
        next load. Native-format local files (the quickstart case) write
        back faithfully.
        """
        if self._data_dir is None:
            return
        if self._schema_mapping or self._yaml_mapping is not None:
            # INFO (not DEBUG): a create/update in mapped-local mode logs
            # work_order_created at INFO, so the skip must be equally visible —
            # otherwise the write silently never reaches disk.
            logger.info(
                "local_persist_skipped_mapping",
                connector="GenericCmmsConnector",
                operation="_persist_work_orders",
                file="work_orders.json",
            )
            return
        path = self._data_dir / "work_orders.json"
        payload = [wo.model_dump(mode="json") for wo in self._work_orders]
        # Write to a temp sibling then atomically replace, so a crash mid-write
        # cannot truncate work_orders.json and break the next connect(). Clean up
        # the temp file if the write or replace fails (e.g. a Windows replace on
        # an open handle) so it doesn't linger and confuse the next write.
        tmp = path.with_name(path.name + ".tmp")
        try:
            await asyncio.to_thread(
                tmp.write_text,
                json.dumps(payload, indent=2, default=str),
                encoding="utf-8",
            )
            await asyncio.to_thread(tmp.replace, path)
        except Exception:
            await asyncio.to_thread(tmp.unlink, True)
            raise

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
            # Native (unmapped) files are the ones _persist_work_orders writes via
            # model_dump(mode="json"); model_validate is its lossless inverse, so
            # timestamps, spare_parts, requested_skills and metadata survive a
            # write→restart→reload cycle. Fall back to the tolerant parser for
            # hand-written files that omit or mistype fields.
            native = not self._schema_mapping and self._yaml_mapping is None
            for item in raw:
                if native:
                    try:
                        self._work_orders.append(WorkOrder.model_validate(item))
                        continue
                    except ValidationError as exc:
                        # Native-format file failed strict validation — a sign of
                        # corruption or a hand-edit. Log (don't swallow silently)
                        # then degrade to the tolerant parser below.
                        logger.debug(
                            "work_order_native_parse_fallback",
                            connector="GenericCmmsConnector",
                            operation="_load_local_data",
                            work_order_id=item.get("id", "?") if isinstance(item, dict) else "?",
                            error=str(exc),
                        )
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

        maintenance_plans_file = self._data_dir / "maintenance_plans.json"
        if maintenance_plans_file.exists():
            text = await asyncio.to_thread(maintenance_plans_file.read_text, encoding="utf-8")
            raw = json.loads(text)
            for item in raw:
                mapped = self._apply_mapping("maintenance_plans", item)
                self._maintenance_plans.append(_parse_maintenance_plan(mapped))
            logger.debug(
                "loaded_maintenance_plans",
                connector="GenericCmmsConnector",
                count=len(self._maintenance_plans),
            )

        failure_modes_file = self._data_dir / "failure_modes.json"
        # Re-snapshot at load so the declared capability and the loaded
        # catalog stay consistent for the lifetime of the connection.
        self._has_fm_source = failure_modes_file.exists()
        if self._has_fm_source:
            text = await asyncio.to_thread(failure_modes_file.read_text, encoding="utf-8")
            raw = json.loads(text)
            for item in raw:
                self._failure_modes.append(FailureMode(**item))
            logger.debug(
                "loaded_failure_modes",
                connector="GenericCmmsConnector",
                count=len(self._failure_modes),
            )

    def _apply_mapping(self, entity: str, data: dict[str, Any]) -> dict[str, Any]:
        """Apply schema mapping to a single raw item dict.

        Supports three mapping forms:

        1. **YAML mapper (v0.3+)**: ``yaml_mapping`` is set — uses the
           declarative YAML schema with typed coercers and JSONPath-lite.
        2. **Flat rename (legacy)**: ``{"asset_id": "id"}`` renames top-level
           keys. Any field not mentioned in the mapping is preserved with
           its original key.
        3. **JMESPath extraction**: ``{"_fields": {"id": "equipment.id"}}``
           produces a new dict with only the listed fields, each extracted
           via a JMESPath expression. Missing paths are silently dropped.
        """
        if self._yaml_mapping is not None:
            return self._apply_yaml_mapping(entity, data)

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

    _ENTITY_KEY_MAP: ClassVar[dict[str, str]] = {
        "assets": "asset",
        "work_orders": "work_order",
        "spare_parts": "spare_part",
        "maintenance_plans": "maintenance_plan",
    }

    def _apply_yaml_mapping(self, entity: str, data: dict[str, Any]) -> dict[str, Any]:
        """Apply the YAML mapper to a single raw dict."""
        assert self._yaml_mapping is not None
        key = self._ENTITY_KEY_MAP.get(entity, entity)
        entity_mapping = self._yaml_mapping.mapping.get(key)
        if entity_mapping is None:
            return data
        return _yaml_map_row(entity_mapping, data)

    def _yaml_reverse_map(self, entity: str, domain_data: dict[str, Any]) -> dict[str, Any]:
        """Reverse-map domain fields to external API fields for writes."""
        assert self._yaml_mapping is not None
        key = self._ENTITY_KEY_MAP.get(entity, entity)
        entity_mapping = self._yaml_mapping.mapping.get(key)
        if entity_mapping is None or entity_mapping.reverse_fields is None:
            return domain_data
        return _yaml_reverse_row(entity_mapping, domain_data)

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
            raise ConnectorAuthError("API key or auth strategy is required for REST mode")
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
        payload = work_order.model_dump(mode="json")
        if self._yaml_mapping is not None:
            payload = self._yaml_reverse_map("work_order", payload)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self._rest_url("work_orders"),
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
        return _parse_work_order(self._apply_mapping("work_orders", resp.json()))

    async def _rest_get_work_order(self, work_order_id: str) -> WorkOrder | None:
        """Fetch a single work order from the REST API."""
        config = self._require_endpoint("get_work_order")
        httpx = _require_httpx()
        path = config["path"].replace("{id}", work_order_id)
        headers = self._rest_headers()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(self._rest_url(path), headers=headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
        return _parse_work_order(self._apply_mapping("work_orders", resp.json()))

    async def _rest_update_work_order(
        self,
        work_order_id: str,
        *,
        status: WorkOrderStatus | None = None,
        assigned_to: str | None = None,
        description: str | None = None,
    ) -> WorkOrder:
        """Update a work order via the REST API and re-fetch."""
        config = self._require_endpoint("update_work_order")
        httpx = _require_httpx()
        path = config["path"].replace("{id}", work_order_id)
        method = config.get("method", "PATCH")
        field_map: dict[str, str] = config.get("field_map", {})

        payload: dict[str, Any] = {}
        if status is not None:
            payload["status"] = status.value
        if assigned_to is not None:
            payload["assigned_to"] = assigned_to
        if description is not None:
            payload["description"] = description

        # Apply field mapping to payload keys
        if field_map:
            payload = {field_map.get(k, k): v for k, v in payload.items()}

        headers = {**self._rest_headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, self._rest_url(path), headers=headers, json=payload
            )
            resp.raise_for_status()

        logger.info(
            "work_order_updated",
            connector="GenericCmmsConnector",
            operation="update_work_order",
            work_order_id=work_order_id,
        )
        # Re-fetch if get_work_order is configured; otherwise parse the
        # PATCH response directly so update works without a separate GET
        # endpoint.
        if "get_work_order" in self._endpoints:
            updated = await self.get_work_order(work_order_id)
            if updated is None:
                raise ConnectorError(f"Work order {work_order_id} not found after update")
            return updated
        if resp.content:
            return _parse_work_order(self._apply_mapping("work_orders", resp.json()))
        # No response body and no get endpoint — return a minimal WO
        return WorkOrder(
            id=work_order_id,
            type=WorkOrderType.CORRECTIVE,
            asset_id="",
            status=status or WorkOrderStatus.CREATED,
        )

    async def _rest_read_maintenance_plans(self) -> list[MaintenancePlan]:
        """Fetch maintenance plans from the REST API."""
        config = self._require_endpoint("read_maintenance_plans")
        httpx = _require_httpx()
        path = config["path"]
        headers = self._rest_headers()
        results: list[MaintenancePlan] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            async for raw in self._pagination.iterate(client, self._rest_url(path), headers):
                results.append(
                    _parse_maintenance_plan(self._apply_mapping("maintenance_plans", raw))
                )
        return results

    # ------------------------------------------------------------------
    # Endpoint configuration helpers
    # ------------------------------------------------------------------

    def _require_endpoint(self, operation: str) -> dict[str, Any]:
        """Return the endpoint config for an operation or raise.

        Raises:
            ConnectorError: With an actionable message when the endpoint
                is not configured.
        """
        config = self._endpoints.get(operation)
        if config is None:
            logger.warning(
                "endpoint_not_configured",
                connector="GenericCmmsConnector",
                operation=operation,
            )
            raise ConnectorError(
                f"{operation} is not configured for this CMMS. "
                f"Add an '{operation}' entry to the 'endpoints' parameter."
            )
        return config

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
    # ``data.get(key) is not None`` so an explicit ``null`` is treated like a
    # missing field rather than passed to an enum constructor — mirrors
    # _parse_work_order and keeps assets safe if they are ever persisted.
    return Asset(
        id=str(data.get("id", "")),
        name=str(data.get("name", "")),
        type=AssetType(data["type"])
        if data.get("type") is not None
        else AssetType.ROTATING_EQUIPMENT,
        location=str(data.get("location", "")),
        manufacturer=str(data.get("manufacturer", "")),
        model=str(data.get("model", "")),
        serial_number=str(data.get("serial_number", "")),
        criticality=(
            Criticality(data["criticality"])
            if data.get("criticality") is not None
            else Criticality.C
        ),
        parent=data.get("parent"),
        children=data.get("children", []),
        failure_modes=data.get("failure_modes", []),
        metadata=data.get("metadata", {}),
        equipment_class_code=data.get("equipment_class_code"),
    )


def _parse_work_order(data: dict[str, Any]) -> WorkOrder:
    """Parse a dict into a WorkOrder."""
    # ``data.get(key) is not None`` (not ``key in data``) so an explicit
    # ``null`` — which ``model_dump`` writes for every optional field during
    # local-mode persistence — is treated like a missing field instead of being
    # passed to an enum constructor (``FailureImpact(None)`` would raise).
    # ``is not None`` (not bare truthiness) so a valid falsy value would still
    # be honoured; enum string values are never falsy, but this states intent.
    return WorkOrder(
        id=str(data.get("id", "")),
        type=WorkOrderType(data["type"])
        if data.get("type") is not None
        else WorkOrderType.CORRECTIVE,
        priority=Priority(data["priority"])
        if data.get("priority") is not None
        else Priority.MEDIUM,
        # status / assigned_to are read so lifecycle changes round-trip through
        # local-mode persistence (write-back of work_orders.json). Absent in
        # legacy files, defaulting to CREATED — the pre-existing behaviour.
        status=(
            WorkOrderStatus(data["status"])
            if data.get("status") is not None
            else WorkOrderStatus.CREATED
        ),
        asset_id=str(data.get("asset_id", "")),
        description=str(data.get("description", "")),
        failure_mode=data.get("failure_mode"),
        assigned_to=data.get("assigned_to"),
        failure_impact=(
            FailureImpact(data["failure_impact"])
            if data.get("failure_impact") is not None
            else None
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


def _parse_maintenance_plan(data: dict[str, Any]) -> MaintenancePlan:
    """Parse a dict into a MaintenancePlan, tolerating missing fields."""
    interval_data = data.get("interval", {})
    if isinstance(interval_data, dict):
        interval = Interval(
            days=int(interval_data.get("days", 0)),
            weeks=int(interval_data.get("weeks", 0)),
            months=int(interval_data.get("months", 0)),
            hours=int(interval_data.get("hours", 0)),
        )
    else:
        interval = Interval(days=int(interval_data) if interval_data else 0)
    return MaintenancePlan(
        id=str(data.get("id", "")),
        asset_id=str(data.get("asset_id", "")),
        name=str(data.get("name", "")),
        interval=interval,
        tasks=data.get("tasks", []),
        active=data.get("active", True),
    )


# ---------------------------------------------------------------------------
# YAML mapper engine — declarative dict → entity mapping
# ---------------------------------------------------------------------------


def _yaml_map_row(
    entity_mapping: Any,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Map a raw API dict to a domain-field dict using a YAML EntityMapping."""
    from machina.connectors.cmms.generic_schema import FieldSpec

    result: dict[str, Any] = {}
    for field_name, spec in entity_mapping.fields.items():
        if isinstance(spec, dict):
            # Nested metadata group
            nested: dict[str, Any] = {}
            for sub_key, sub_spec in spec.items():
                if isinstance(sub_spec, FieldSpec):
                    val = _yaml_coerce_field(sub_spec, raw)
                    if val is not None:
                        nested[sub_key] = val
            result[field_name] = nested
        elif isinstance(spec, FieldSpec):
            val = _yaml_coerce_field(spec, raw)
            if val is None and spec.required:
                return {}  # skip this row
            result[field_name] = val
    return result


def _yaml_coerce_field(spec: Any, raw: dict[str, Any]) -> Any:
    """Resolve and coerce a single field from a raw dict."""
    value = resolve_path(raw, spec.source)
    if value is None:
        return spec.default

    if spec.coerce:
        coercer = _YAML_COERCERS.get(spec.coerce)
        if coercer is not None:
            kwargs: dict[str, Any] = {}
            if spec.enum_map:
                kwargs["enum_map"] = spec.enum_map
            if spec.default is not None:
                kwargs["default"] = spec.default
            if spec.pattern:
                kwargs["pattern"] = spec.pattern
            value = coercer(value, **kwargs)
        elif spec.coerce == "enum_map" and spec.enum_map:
            from machina.connectors.cmms.generic_coercers import coerce_enum_map

            value = coerce_enum_map(value, enum_map=spec.enum_map, default=spec.default)

    return value


def _yaml_reverse_row(entity_mapping: Any, domain_data: dict[str, Any]) -> dict[str, Any]:
    """Reverse-map domain fields to external API fields for a write payload."""
    from machina.connectors.cmms.generic_schema import ReverseFieldSpec

    result: dict[str, Any] = {}
    assert entity_mapping.reverse_fields is not None
    for domain_field, spec in entity_mapping.reverse_fields.items():
        value = domain_data.get(domain_field)
        if isinstance(spec, str):
            result[spec] = value
        elif isinstance(spec, ReverseFieldSpec):
            if spec.reverse_enum_map and value is not None:
                str_val = str(value)
                value = spec.reverse_enum_map.get(str_val, value)
            result[spec.target] = value
    return result
