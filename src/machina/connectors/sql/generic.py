"""GenericSqlConnector — YAML-schema-driven adapter for legacy SQL databases.

Connects to AS/400 (DB2 via ODBC), SQL Server, MySQL, PostgreSQL, and
Italian gestionali (Zucchetti/TeamSystem via ODBC or JDBC).  Schema
mapping is defined in YAML so the user writes zero Python.
"""

from __future__ import annotations

import asyncio
import contextlib
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.capabilities import Capability
from machina.connectors.sql.dialect import COERCER_REGISTRY, redact_dsn
from machina.connectors.sql.drivers import connect_jdbc, connect_odbc
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.work_order import (
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)
from machina.exceptions import (
    ConnectorConfigError,
    ConnectorError,
    ConnectorSchemaError,
    ConnectorTransientError,
)

if TYPE_CHECKING:
    from machina.connectors.sql.schema import FieldMapping, SqlConnectorConfig, TableMapping

logger = structlog.get_logger(__name__)

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


def _dict_to_asset(d: dict[str, Any]) -> Asset:
    """Build an Asset from a coerced field dict."""
    return Asset(
        id=str(d.get("id", "")),
        name=str(d.get("name", "")),
        type=d.get("type", AssetType.ROTATING_EQUIPMENT),
        location=str(d.get("location", "")),
        manufacturer=str(d.get("manufacturer", "")),
        model=str(d.get("model", "")),
        serial_number=str(d.get("serial_number", "")),
        install_date=d.get("install_date"),
        criticality=d.get("criticality", Criticality.C),
        parent=d.get("parent"),
        metadata={k: v for k, v in d.items() if k not in Asset.model_fields},
    )


def _dict_to_work_order(d: dict[str, Any]) -> WorkOrder:
    """Build a WorkOrder from a coerced field dict."""
    return WorkOrder(
        id=str(d.get("id", "")),
        type=d.get("type", WorkOrderType.CORRECTIVE),
        priority=d.get("priority", Priority.MEDIUM),
        status=d.get("status", WorkOrderStatus.CREATED),
        asset_id=str(d.get("asset_id", "")),
        description=str(d.get("description", "")),
        assigned_to=d.get("assigned_to"),
        estimated_duration_hours=d.get("estimated_duration_hours"),
        metadata={k: v for k, v in d.items() if k not in WorkOrder.model_fields},
    )


_ENTITY_BUILDERS: dict[str, Any] = {
    "Asset": _dict_to_asset,
    "WorkOrder": _dict_to_work_order,
}


class GenericSqlConnector:
    """Connector for legacy SQL databases via ODBC or JDBC.

    Reads assets and work orders from database tables using a YAML
    column-to-field schema mapping.  Writes new work orders via
    parameterized INSERT.

    Args:
        config: Parsed SQL connector configuration.

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

    def __init__(self, *, config: SqlConnectorConfig) -> None:
        self._config = config
        self._conn: Any = None
        self._connected = False

        caps: set[Capability] = {Capability.READ_ASSETS, Capability.READ_WORK_ORDERS}
        if config.capabilities == "read_write":
            caps |= {Capability.CREATE_WORK_ORDER, Capability.UPDATE_WORK_ORDER}
        self._capabilities = frozenset(caps)

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Instance-level capabilities based on config."""
        return self._capabilities

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the database connection and validate schema mappings."""
        self._conn = await asyncio.to_thread(self._open_connection)
        await asyncio.to_thread(self._validate_schemas)
        self._connected = True
        logger.info(
            "connected",
            connector="GenericSqlConnector",
            driver_type=self._config.driver_type,
            dsn=redact_dsn(self._config.dsn),
        )

    async def disconnect(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._conn.close)
            self._conn = None
        self._connected = False

    async def health_check(self) -> ConnectorHealth:
        """Test the database connection with a simple query."""
        if self._conn is None:
            return ConnectorHealth(
                status=ConnectorStatus.UNHEALTHY,
                message="Not connected",
            )
        try:
            await asyncio.to_thread(self._execute_scalar, "SELECT 1")
            return ConnectorHealth(
                status=ConnectorStatus.HEALTHY,
                message="Database reachable",
            )
        except Exception as exc:
            return ConnectorHealth(
                status=ConnectorStatus.UNHEALTHY,
                message=f"Health check failed: {exc}",
            )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def read_assets(self) -> list[Asset]:
        """Read assets from the configured table mapping."""
        mapping = self._find_mapping("Asset")
        if mapping is None:
            return []
        rows = await self._execute_read(mapping)
        return [_dict_to_asset(r) for r in rows]

    async def read_work_orders(self) -> list[WorkOrder]:
        """Read work orders from the configured table mapping."""
        mapping = self._find_mapping("WorkOrder")
        if mapping is None:
            return []
        rows = await self._execute_read(mapping)
        return [_dict_to_work_order(r) for r in rows]

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Insert a new work order row into the database."""
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
        await asyncio.to_thread(self._execute_insert, mapping, work_order.model_dump())
        logger.info(
            "work_order_created",
            connector="GenericSqlConnector",
            work_order_id=work_order.id,
            asset_id=work_order.asset_id,
        )
        return work_order

    async def update_work_order(self, work_order_id: str, updates: dict[str, Any]) -> WorkOrder:
        """Update a work order — re-reads after update to return fresh state."""
        if Capability.UPDATE_WORK_ORDER not in self._capabilities:
            raise ConnectorConfigError(
                "Write operations not enabled — set capabilities: read_write"
            )
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
            )
        return connect_odbc(self._config.dsn)

    def _validate_schemas(self) -> None:
        """Check that mapped columns exist in query results."""
        cursor = self._conn.cursor()
        try:
            for name, mapping in self._config.tables.items():
                limited_query = f"SELECT * FROM ({mapping.query}) AS _machina_validate WHERE 1=0"
                try:
                    cursor.execute(limited_query)
                except Exception:
                    # Fallback: some DBs don't support subquery aliasing
                    try:
                        cursor.execute(mapping.query)
                    except Exception as exc:
                        raise ConnectorSchemaError(
                            f"Query for table mapping '{name}' failed: {exc}"
                        ) from exc

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

    async def _execute_read(self, mapping: TableMapping) -> list[dict[str, Any]]:
        """Execute a read query with retry on transient errors."""
        retry_cfg = self._config.retry

        for attempt in range(retry_cfg.max_retries + 1):
            try:
                return await asyncio.to_thread(self._read_sync, mapping)
            except Exception as exc:
                if _is_transient(exc) and attempt < retry_cfg.max_retries:
                    backoff = min(
                        retry_cfg.base_backoff * (2**attempt),
                        retry_cfg.max_backoff,
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
                raise
        return []  # unreachable, satisfies mypy

    def _read_sync(self, mapping: TableMapping) -> list[dict[str, Any]]:
        cursor = self._conn.cursor()
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
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, values)
            self._conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                self._conn.rollback()
            raise
        finally:
            cursor.close()

    def _execute_scalar(self, sql: str) -> Any:
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()
