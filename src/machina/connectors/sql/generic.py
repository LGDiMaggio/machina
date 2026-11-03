"""GenericSqlConnector — YAML-schema-driven adapter for legacy SQL databases.

Connects to AS/400 (DB2 via ODBC), SQL Server, MySQL, PostgreSQL, and
Italian gestionali (Zucchetti/TeamSystem via ODBC or JDBC).  Schema
mapping is defined in YAML so the user writes zero Python.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
from concurrent.futures import Future, ThreadPoolExecutor
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, TypeVarTuple

import structlog
from pydantic import ValidationError

from machina.connectors._entity_builders import dict_to_asset as _dict_to_asset
from machina.connectors._entity_builders import dict_to_failure_mode as _dict_to_failure_mode
from machina.connectors._entity_builders import dict_to_work_order as _dict_to_work_order
from machina.connectors._settings import validate_settings
from machina.connectors.base import ConnectorHealth, ConnectorStatus, sandbox_aware
from machina.connectors.capabilities import Capability
from machina.connectors.sql.dialect import COERCER_REGISTRY, redact_dsn
from machina.connectors.sql.drivers import connect_jdbc, connect_odbc
from machina.exceptions import (
    ConnectorConfigError,
    ConnectorError,
    ConnectorSchemaError,
    ConnectorTimeoutError,
    ConnectorTransientError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from machina.connectors.sql.schema import FieldMapping, SqlConnectorConfig, TableMapping
    from machina.domain.asset import Asset
    from machina.domain.failure_mode import FailureMode
    from machina.domain.work_order import WorkOrder, WorkOrderStatus

logger = structlog.get_logger(__name__)

_T = TypeVar("_T")
_Ts = TypeVarTuple("_Ts")

# Transient SQL error codes that warrant retry
_TRANSIENT_ERRORS: frozenset[str] = frozenset(
    {
        "1205",  # SQL Server deadlock
        "-911",  # DB2 timeout / deadlock
        "SQL0913",  # AS/400 row lock
        "40001",  # SQLSTATE serialization failure
        "40P01",  # PostgreSQL deadlock
    }
)


def _is_transient(exc: Exception) -> bool:
    """Check if a database exception is transient and retryable."""
    msg = str(exc)
    return any(code in msg for code in _TRANSIENT_ERRORS)


# A statement stopped by a timeout (query_timeout, or one the driver or the
# database sets). Not retried — see _execute_read and _execute_write.
_TIMEOUT_SQLSTATES: frozenset[str] = frozenset(
    {
        "HYT00",  # ODBC query timeout expired
        "HYT01",  # ODBC connection timeout expired
        "HY008",  # operation cancelled (Db2 CLI on its query timeout; mssql-jdbc)
        "57014",  # processing cancelled (Db2 SQL0952, PostgreSQL)
    }
)
_TIMEOUT_MARKERS: tuple[str, ...] = (
    "TimeoutException",  # JDBC: java.sql.SQLTimeoutException and its subclasses
    "SQL0666",  # IBM i: refused on the optimizer's estimate of its run time
)


def _sqlstate(exc: Exception) -> str:
    """Return the SQLSTATE a driver gives for ``exc``, or ``""``.

    pyodbc passes it as the exception's first argument; jaydebeapi passes the
    Java ``SQLException``, which has ``getSQLState()``.
    """
    first = exc.args[0] if exc.args else None
    if isinstance(first, str):
        return first
    get_sqlstate = getattr(first, "getSQLState", None)
    return str(get_sqlstate() or "") if callable(get_sqlstate) else ""


def _is_timeout(exc: Exception) -> bool:
    """Check if a database exception reports a statement stopped by a timeout."""
    if _sqlstate(exc) in _TIMEOUT_SQLSTATES:
        return True
    msg = str(exc)
    return any(marker in msg for marker in _TIMEOUT_MARKERS)


# How long health_check() and disconnect() wait for a call already using the
# connection when no query_timeout bounds that call.
_FALLBACK_CONNECTION_WAIT = 30.0


def _new_db_thread() -> ThreadPoolExecutor:
    """The single worker thread every use of one connection runs on."""
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="machina-sql")


def _close_quietly(conn: Any) -> None:
    """Close a connection the connector let go while a call may still use it.

    Runs on that connection's worker thread, after that call: a disconnect()
    that stopped waiting for it, or a connect() whose schema check failed.
    The connector has already let the connection go, so a failure to close
    it is ignored.
    """
    with contextlib.suppress(Exception):
        conn.close()


def _log_failure_after_cancel(operation: str, done: Future[Any]) -> None:
    """Log a call that failed after its caller was cancelled: nobody else sees it."""
    if done.cancelled():
        return
    exc = done.exception()
    if exc is not None:
        logger.error(
            "failed_after_cancel",
            connector="GenericSqlConnector",
            operation=operation,
            error_type=type(exc).__name__,
            error=str(exc),
        )


def _jitter() -> float:
    """Return a jitter multiplier in [0.5, 1.5) to decorrelate retries."""
    import random

    return 0.5 + random.random()


def _coerce_value(raw: Any, mapping: FieldMapping, *, codepage: str = "cp037") -> Any:
    """Apply coercion and enum mapping to a single column value."""
    if raw is None:
        return mapping.default

    if mapping.coerce:
        coercer = COERCER_REGISTRY.get(mapping.coerce)
        if coercer is None:
            msg = f"Unknown coercer: {mapping.coerce!r}"
            raise ConnectorConfigError(msg)
        raw = coercer(raw, codepage=codepage) if mapping.coerce == "strip_ebcdic" else coercer(raw)

    if mapping.enum_map:
        key = str(raw).strip()
        if key in mapping.enum_map:
            raw = mapping.enum_map[key]

    return raw


def _row_to_dict(
    row: Any,
    columns: list[str],
    table_mapping: TableMapping,
    *,
    codepage: str = "cp037",
) -> dict[str, Any]:
    """Convert a DB-API row + column names to a coerced field dict."""
    raw_dict = {columns[i]: row[i] for i in range(len(columns))}
    result: dict[str, Any] = {}
    for field_name, field_mapping in table_mapping.fields.items():
        raw_value = raw_dict.get(field_mapping.column)
        result[field_name] = _coerce_value(raw_value, field_mapping, codepage=codepage)
    return result


class GenericSqlConnector:
    """Connector for legacy SQL databases via ODBC or JDBC.

    Reads assets, work orders, and failure-mode catalogs from database
    tables using a YAML column-to-field schema mapping.  Writes new
    work orders via parameterized INSERT.

    Args:
        config: Parsed SQL connector configuration.
        **settings: Alternatively, the same configuration as flat keyword
            arguments (``dsn=``, ``tables=``, ``capabilities=``, ...) — the
            shape a ``machina.yaml`` ``settings`` block carries. Pass either
            ``config`` or settings. Validation errors never echo the DSN.

    Raises:
        ConnectorConfigError: If both forms are given, or the settings do
            not validate.

    Example:
        ```python
        from machina.connectors.sql.generic import GenericSqlConnector
        from machina.connectors.sql.schema import SqlConnectorConfig

        config = SqlConnectorConfig.model_validate(yaml.safe_load(open("sql.yaml")))
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        assets = await connector.read_assets()
        ```
    """

    # Capabilities available regardless of configuration. CREATE_WORK_ORDER
    # and READ_FAILURE_MODES are config-driven and added in __init__ — they
    # are NOT part of the base set. UPDATE_WORK_ORDER is never declared
    # (update_work_order is a stub).
    # Exposed as a ClassVar so framework introspection can read the base set
    # off the class without instantiating the connector (which needs a parsed
    # config). __init__ derives the live capability set from this constant, so
    # the two cannot drift.
    _BASE_CAPABILITIES: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READ_ASSETS, Capability.READ_WORK_ORDERS}
    )

    def __init__(
        self,
        *,
        config: SqlConnectorConfig | dict[str, Any] | None = None,
        **settings: Any,
    ) -> None:
        from machina.connectors.sql.schema import SqlConnectorConfig

        if isinstance(config, dict):
            # ``settings: {config: {...}}`` in YAML — same shape, nested.
            config = validate_settings(SqlConnectorConfig, config)
        if config is None:
            config = validate_settings(SqlConnectorConfig, settings)
        elif settings:
            raise ConnectorConfigError(
                "GenericSqlConnector takes either config= or flat settings, not both"
            )
        self._config = config
        self._conn: Any = None
        self._connected = False
        # One DB-API connection is shared by every caller (the MCP HTTP server
        # serves concurrent requests from one connector), and DB-API
        # connections are not safe to use from several threads at once:
        # every use of the connection runs on this one worker thread, one
        # call at a time (see _run_on_connection). _write_lock additionally
        # holds the idempotency probe and the INSERT together, and
        # _lifecycle_lock keeps connect() and disconnect() from interleaving.
        self._db_thread = _new_db_thread()
        self._write_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        # INSERTs whose caller was cancelled while they still ran: a later
        # create waits for them before its ID probe, even on a new connection
        # after disconnect() retired their worker (see create_work_order).
        self._orphan_inserts: set[Future[Any]] = set()

        caps: set[Capability] = set(self._BASE_CAPABILITIES)
        if config.capabilities == "read_write":
            # Only the INSERT path exists. UPDATE_WORK_ORDER is not declared:
            # update_work_order needs a per-schema UPDATE query that is not
            # implemented, and a declared capability that always raises would
            # still be offered to MCP clients.
            caps.add(Capability.CREATE_WORK_ORDER)
        # Declared only when a FailureMode table mapping is configured, so
        # capability discovery is a true signal of "has a catalog source".
        if any(m.entity == "FailureMode" for m in config.tables.values()):
            caps.add(Capability.READ_FAILURE_MODES)
        self._capabilities = frozenset(caps)

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Instance-level capabilities based on config."""
        return self._capabilities

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the database connection and validate schema mappings.

        If the schema check fails, the connection is closed again, once the
        check is done with it, and the error is raised.
        """
        async with self._lifecycle_lock:
            conn = await asyncio.to_thread(self._open_connection)
            self._conn = conn
            try:
                await self._run_on_connection(self._validate_schemas, conn)
            except BaseException:
                # The check may still be running (a cancelled connect()), so
                # close the connection behind it and give the next connect()
                # a new worker.
                self._abandon_connection()
                raise
            self._connected = True
        logger.info(
            "connected",
            connector="GenericSqlConnector",
            driver_type=self._config.driver_type,
            dsn=redact_dsn(self._config.dsn),
        )

    async def disconnect(self) -> None:
        """Close the database connection.

        Waits at most ``query_timeout`` seconds (30 when unset) for the calls
        still using the connection. Past that, it logs ``sql_disconnect_busy``
        and drops the connection without closing it, since closing it under a
        running statement is not safe (see :meth:`_abandon_connection`). A
        disconnect() cancelled while it waits drops the connection the same
        way before the cancellation propagates.
        """
        async with self._lifecycle_lock:
            if self._conn is not None:
                try:
                    await asyncio.wait_for(
                        self._run_on_connection(self._close_sync), timeout=self._connection_wait
                    )
                except TimeoutError:
                    self._abandon_connection()
                    logger.warning(
                        "sql_disconnect_busy",
                        connector="GenericSqlConnector",
                        operation="disconnect",
                        waited=self._connection_wait,
                        hint="A statement is still running; the connection closes when it ends",
                    )
                except asyncio.CancelledError:
                    self._abandon_connection()
                    self._connected = False
                    raise
                except Exception as exc:
                    # Closing a broken connection may fail; it is dropped anyway.
                    logger.debug(
                        "sql_close_failed",
                        connector="GenericSqlConnector",
                        operation="disconnect",
                        error_type=type(exc).__name__,
                    )
                self._conn = None
            self._connected = False

    async def health_check(self) -> ConnectorHealth:
        """Test the database connection with a simple query.

        Reports the connection busy, instead of queueing behind it, when
        another call still uses it after ``query_timeout`` seconds (30 when
        unset).
        """
        if self._conn is None:
            return ConnectorHealth(
                status=ConnectorStatus.UNHEALTHY,
                message="Not connected",
            )
        try:
            # On timeout, wait_for drops the probe if it is still queued.
            await asyncio.wait_for(
                self._run_on_connection(self._execute_scalar, "SELECT 1"),
                timeout=self._connection_wait,
            )
        except TimeoutError:
            return ConnectorHealth(
                status=ConnectorStatus.UNHEALTHY,
                message=f"Connection busy: still in use after waiting {self._connection_wait:g}s",
            )
        except Exception as exc:
            return ConnectorHealth(
                status=ConnectorStatus.UNHEALTHY,
                message=f"Health check failed: {exc}",
            )
        return ConnectorHealth(
            status=ConnectorStatus.HEALTHY,
            message="Database reachable",
        )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def read_assets(self) -> list[Asset]:
        """Read assets from the configured table mapping.

        When the mapping includes a ``failure_modes`` field, the source
        column is interpreted as a semicolon-delimited failure-mode code
        string and resolved into ``Asset.failure_modes`` (see
        :func:`machina.connectors._entity_builders.split_list_cell`).
        """
        mapping = self._find_mapping("Asset")
        if mapping is None:
            return []
        rows = await self._execute_read(mapping)
        return [_dict_to_asset(r) for r in rows]

    async def get_asset(self, asset_id: str) -> Asset | None:
        """Return one asset from the configured table mapping, or ``None``."""
        for asset in await self.read_assets():
            if asset.id == asset_id:
                return asset
        return None

    async def read_work_orders(
        self,
        *,
        asset_id: str = "",
        status: WorkOrderStatus | str = "",
    ) -> list[WorkOrder]:
        """Read work orders from the configured table mapping.

        Args:
            asset_id: Keep only work orders for this asset.
            status: Keep only work orders in this status (enum or its value).
        """
        mapping = self._find_mapping("WorkOrder")
        if mapping is None:
            return []
        rows = await self._execute_read(mapping)
        wanted_status = str(getattr(status, "value", status) or "")
        return [
            wo
            for wo in (_dict_to_work_order(r) for r in rows)
            if (not asset_id or wo.asset_id == asset_id)
            and (not wanted_status or getattr(wo.status, "value", wo.status) == wanted_status)
        ]

    async def read_failure_modes(self) -> list[FailureMode]:
        """Read the failure-mode catalog from the configured table mapping.

        Returns an empty list when no ``FailureMode`` table mapping is
        configured (in which case ``Capability.READ_FAILURE_MODES`` is
        not declared either).

        Raises:
            ConnectorError: If not connected or the read fails.
            ConnectorSchemaError: If a mapped row produces an invalid
                ``FailureMode`` (e.g. NULL/empty code) — a loud schema
                signal rather than a silently truncated catalog.
        """
        mapping = self._find_mapping("FailureMode")
        if mapping is None:
            return []
        rows = await self._execute_read(mapping)
        modes: list[FailureMode] = []
        for row in rows:
            try:
                modes.append(_dict_to_failure_mode(row))
            except ValidationError as exc:
                raise ConnectorSchemaError(
                    f"FailureMode mapping produced an invalid row {row!r}: {exc}"
                ) from exc
        return modes

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    @sandbox_aware
    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Insert a new work order row into the database.

        Idempotent on the work-order ID within this process: the ID probe and
        the INSERT run under one lock, and when a row with the same ID is
        already readable through the WorkOrder mapping, that record is
        returned and nothing is inserted. The agent runtime and the MCP tools
        derive deterministic IDs and rely on this to collapse retries. Across
        processes, a unique constraint on the ID column is the backstop.
        """
        if Capability.CREATE_WORK_ORDER not in self._capabilities:
            raise ConnectorConfigError(
                "Write operations not enabled — set capabilities: read_write"
            )
        mapping = self._find_mapping("WorkOrder")
        if mapping is None:
            raise ConnectorConfigError("No WorkOrder table mapping configured")
        if not mapping.insert_table:
            raise ConnectorConfigError("No insert_table configured for WorkOrder mapping")
        if not mapping.insert_columns:
            raise ConnectorConfigError("No insert_columns configured for WorkOrder mapping")
        async with self._write_lock:
            if self._orphan_inserts:
                # An INSERT whose caller was cancelled may still commit, on a
                # connection disconnect() already let go: wait for it, or this
                # probe could miss its row and insert the work order twice.
                # asyncio.wait, so cancelling this caller leaves it running.
                await asyncio.wait([asyncio.wrap_future(f) for f in self._orphan_inserts])
            # Probe the mapped rows by ID rather than building every row into
            # a WorkOrder: one legacy row with an unmapped status must not
            # block every create.
            rows = await self._execute_read(mapping)
            match = next((r for r in rows if str(r.get("id", "")) == work_order.id), None)
            if match is not None:
                logger.info(
                    "work_order_create_idempotent_hit",
                    connector="GenericSqlConnector",
                    operation="create_work_order",
                    work_order_id=work_order.id,
                    asset_id=work_order.asset_id,
                )
                try:
                    return _dict_to_work_order(match)
                except (ValidationError, ValueError, TypeError):
                    return work_order
            await self._execute_write(mapping, work_order.model_dump())
        logger.info(
            "work_order_created",
            connector="GenericSqlConnector",
            operation="create_work_order",
            work_order_id=work_order.id,
            asset_id=work_order.asset_id,
        )
        return work_order

    @sandbox_aware
    async def update_work_order(
        self,
        work_order_id: str,
        updates: dict[str, Any] | None = None,
        *,
        status: WorkOrderStatus | None = None,
        assigned_to: str | None = None,
        description: str | None = None,
    ) -> WorkOrder:
        """Update a work order — not implemented for generic SQL schemas.

        Accepts the same dict and keyword forms as the other substrates so
        callers get this explicit error rather than a ``TypeError``.
        ``UPDATE_WORK_ORDER`` is never declared for this connector.
        """
        # introspect: stub — no per-schema UPDATE query is implemented yet, so
        # framework introspection must treat UPDATE_WORK_ORDER as unwired here.
        raise ConnectorError(
            "Generic SQL update_work_order requires a custom UPDATE query "
            "per schema — not yet implemented. Use create_work_order for new records."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_connection(self) -> Any:
        if self._config.driver_type == "jdbc":
            return connect_jdbc(
                self._config.dsn,
                self._config.jdbc_driver_class or "",
                self._config.jdbc_driver_path,
                query_timeout=self._config.query_timeout,
            )
        return connect_odbc(self._config.dsn, query_timeout=self._config.query_timeout)

    @property
    def _connection_wait(self) -> float:
        """Seconds health_check() and disconnect() wait for a busy connection."""
        return self._config.query_timeout or _FALLBACK_CONNECTION_WAIT

    def _abandon_connection(self) -> None:
        """Drop the connection a call is still using, and retire its worker thread.

        Its close is queued on the old worker, behind that call, which then
        shuts down; calls still queued there fail as not connected (see
        :meth:`_run_on_connection`). A later connect() gets a new worker, so
        it does not wait for the statement.
        """
        conn, self._conn = self._conn, None
        old, self._db_thread = self._db_thread, _new_db_thread()
        # None if _close_sync started just as the wait ran out.
        if conn is not None:  # pragma: no branch
            old.submit(_close_quietly, conn)
        old.shutdown(wait=False)

    def _close_sync(self) -> None:
        # Drop the reference before closing, on the connection's own thread,
        # so a call queued behind this one finds no connection, not a closed one.
        conn, self._conn = self._conn, None
        # None if disconnect() stopped waiting and dropped it just as this started.
        if conn is not None:  # pragma: no branch
            conn.close()

    def _validate_schemas(self, conn: Any) -> None:
        """Check that mapped columns exist in query results, on ``conn``."""
        cursor = conn.cursor()
        try:
            for name, mapping in self._config.tables.items():
                limited_query = f"SELECT * FROM ({mapping.query}) AS _machina_validate WHERE 1=0"
                try:
                    cursor.execute(limited_query)
                except Exception as exc:
                    if _is_timeout(exc):
                        raise self._schema_check_timeout(name, exc) from exc
                    # Fallback: some DBs don't support subquery aliasing
                    try:
                        cursor.execute(mapping.query)
                    except Exception as fallback_exc:
                        if _is_timeout(fallback_exc):
                            raise self._schema_check_timeout(name, fallback_exc) from fallback_exc
                        raise ConnectorSchemaError(
                            f"Query for table mapping '{name}' failed: {fallback_exc}"
                        ) from fallback_exc

                result_columns = [desc[0] for desc in (cursor.description or [])]
                for _field_name, field_mapping in mapping.fields.items():
                    if field_mapping.column not in result_columns:
                        raise ConnectorSchemaError(
                            f"Column '{field_mapping.column}' not found in "
                            f"query results for mapping '{name}'. "
                            f"Available columns: {result_columns}"
                        )
        finally:
            cursor.close()

    def _find_mapping(self, entity_type: str) -> TableMapping | None:
        for mapping in self._config.tables.values():
            if mapping.entity == entity_type:
                return mapping
        return None

    async def _run_on_connection(self, func: Callable[[*_Ts], _T], /, *args: *_Ts) -> _T:
        """Run ``func(*args)`` on the connection's worker thread, after earlier calls.

        One thread runs every call, so no two calls overlap, whatever happens
        to their callers. A cancelled caller (an MCP request cancellation, a
        workflow step timeout) cannot stop a call that is already running, and
        the calls behind it wait for it; a call that has not started is dropped.
        Nobody reads the outcome of a call that outlives its caller, so if that
        call fails, the failure is logged as ``failed_after_cancel``. A call
        still queued on a worker that disconnect() retired raises
        ``ConnectorError`` instead of running.
        """
        executor = self._db_thread
        context = contextvars.copy_context()  # as asyncio.to_thread does

        def call() -> _T:
            if executor is not self._db_thread:
                raise ConnectorError("Not connected — the connection was dropped by disconnect()")
            return context.run(func, *args)

        future = executor.submit(call)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            operation = getattr(func, "__name__", "call").strip("_")
            future.add_done_callback(functools.partial(_log_failure_after_cancel, operation))
            is_insert = getattr(func, "__func__", None) is type(self)._execute_insert
            if is_insert and not future.done():
                # Still running: the next create waits for it (create_work_order).
                self._orphan_inserts.add(future)
                future.add_done_callback(self._orphan_inserts.discard)
            raise

    async def _execute_read(self, mapping: TableMapping) -> list[dict[str, Any]]:
        """Execute a read query with retry on transient errors."""
        retry_cfg = self._config.retry

        for attempt in range(retry_cfg.max_retries + 1):
            try:
                return await self._run_on_connection(self._read_sync, mapping)
            except Exception as exc:
                if _is_timeout(exc):
                    # Not retried: the read already held the shared connection
                    # for query_timeout seconds, and a retry would likely wait
                    # on the same lock as long again, with every call queued.
                    self._log_timeout("read", exc)
                    raise ConnectorTimeoutError(f"SQL read timed out: {exc}") from exc
                if _is_transient(exc) and attempt < retry_cfg.max_retries:
                    backoff = (
                        min(
                            retry_cfg.base_backoff * (2**attempt),
                            retry_cfg.max_backoff,
                        )
                        * _jitter()
                    )
                    logger.warning(
                        "sql_transient_retry",
                        connector="GenericSqlConnector",
                        attempt=attempt + 1,
                        max_retries=retry_cfg.max_retries,
                        backoff=backoff,
                        error=str(exc),
                    )
                    await asyncio.sleep(backoff)
                    continue
                if _is_transient(exc):
                    raise ConnectorTransientError(
                        f"Transient SQL error after {retry_cfg.max_retries} retries: {exc}"
                    ) from exc
                if isinstance(exc, ConnectorError):
                    raise
                # Honor the connector contract: callers (e.g. the runtime's
                # failure-mode harvest) key their degraded-operation handling
                # on ConnectorError — a raw driver exception must not escape.
                raise ConnectorError(f"SQL read failed: {exc}") from exc
        return []  # unreachable, satisfies mypy

    async def _execute_write(self, mapping: TableMapping, data: dict[str, Any]) -> None:
        """Execute a write operation with retry on transient errors."""
        retry_cfg = self._config.retry

        for attempt in range(retry_cfg.max_retries + 1):
            try:
                return await self._run_on_connection(self._execute_insert, mapping, data)
            except Exception as exc:
                if _is_timeout(exc):
                    # Never re-sent from this loop: it skips create_work_order's
                    # ID check, and a timeout can come after the row was written
                    # (in commit). A caller's retry goes through that check.
                    self._log_timeout("insert", exc)
                    raise ConnectorTimeoutError(f"SQL write timed out: {exc}") from exc
                if _is_transient(exc) and attempt < retry_cfg.max_retries:
                    backoff = (
                        min(
                            retry_cfg.base_backoff * (2**attempt),
                            retry_cfg.max_backoff,
                        )
                        * _jitter()
                    )
                    logger.warning(
                        "sql_transient_retry",
                        connector="GenericSqlConnector",
                        operation="insert",
                        attempt=attempt + 1,
                        max_retries=retry_cfg.max_retries,
                        backoff=backoff,
                        error=str(exc),
                    )
                    await asyncio.sleep(backoff)
                    continue
                if _is_transient(exc):
                    raise ConnectorTransientError(
                        f"Transient SQL error after {retry_cfg.max_retries} retries: {exc}"
                    ) from exc
                if isinstance(exc, ConnectorError):
                    raise
                # Same contract as _execute_read: callers handle ConnectorError,
                # so a raw driver exception must not escape a write either.
                raise ConnectorError(f"SQL write failed: {exc}") from exc

    def _schema_check_timeout(self, name: str, exc: Exception) -> ConnectorTimeoutError:
        """The error for a schema-check statement stopped by a query timeout."""
        self._log_timeout("validate_schemas", exc)
        return ConnectorTimeoutError(f"SQL schema check for mapping '{name}' timed out: {exc}")

    def _log_timeout(self, operation: str, exc: Exception) -> None:
        # Logged here because callers such as the MCP tools return the error
        # without logging it, and a timeout is how lock contention shows.
        logger.warning(
            "sql_statement_timeout",
            connector="GenericSqlConnector",
            operation=operation,
            query_timeout=self._config.query_timeout,
            error=str(exc),
        )

    # The worker-thread functions below read self._conn once: disconnect()
    # may drop it while they run, and queues its close behind them.

    def _read_sync(self, mapping: TableMapping) -> list[dict[str, Any]]:
        conn = self._conn
        if conn is None:
            raise ConnectorError("Not connected — call connect() before reading")
        cursor = conn.cursor()
        try:
            cursor.execute(mapping.query)
            columns = [desc[0] for desc in (cursor.description or [])]
            results: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                try:
                    d = _row_to_dict(
                        row,
                        columns,
                        mapping,
                        codepage=self._config.ebcdic_codepage,
                    )
                    results.append(d)
                except (ValueError, TypeError) as exc:
                    logger.warning(
                        "broken_row",
                        connector="GenericSqlConnector",
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
            return results
        finally:
            cursor.close()

    def _execute_insert(self, mapping: TableMapping, data: dict[str, Any]) -> None:
        assert mapping.insert_table is not None
        assert mapping.insert_columns is not None

        columns = []
        placeholders = []
        values = []
        for entity_field, db_column in mapping.insert_columns.items():
            columns.append(db_column)
            placeholders.append("?")
            value = data.get(entity_field)
            if isinstance(value, StrEnum):
                value = value.value
            values.append(value)

        sql = (
            f"INSERT INTO {mapping.insert_table} "
            f"({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
        )
        # Commit or roll back on the connection that ran the INSERT.
        conn = self._conn
        if conn is None:
            raise ConnectorError("Not connected — call connect() before writing")
        cursor = conn.cursor()
        try:
            cursor.execute(sql, values)
            conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise
        finally:
            cursor.close()

    def _execute_scalar(self, sql: str) -> Any:
        conn = self._conn
        if conn is None:
            raise ConnectorError("Not connected")
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()
