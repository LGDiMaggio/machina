"""ExcelCsvConnector — YAML-schema-driven Excel/CSV adapter.

Treats a directory of spreadsheets as a CMMS: reads assets and work
orders, appends new work orders.  Schema mapping is defined in YAML
so the user writes zero Python.
"""

from __future__ import annotations

import asyncio
import csv
import re
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from machina.connectors._entity_builders import dict_to_asset as _dict_to_asset
from machina.connectors._entity_builders import dict_to_work_order as _dict_to_work_order
from machina.connectors.base import ConnectorHealth, ConnectorStatus, sandbox_aware
from machina.connectors.capabilities import Capability

if TYPE_CHECKING:
    from machina.connectors.docs.excel_schema import (
        ColumnMapping,
        ExcelConnectorConfig,
        SheetSchema,
    )
    from machina.domain.asset import Asset
    from machina.domain.work_order import WorkOrder
from machina.exceptions import (
    ConnectorConfigError,
    ConnectorError,
    ConnectorLockedError,
    ConnectorSchemaError,
)

logger = structlog.get_logger(__name__)


# ------------------------------------------------------------------
# Coercer registry — named functions referenced from YAML schemas
# ------------------------------------------------------------------


def _float_it(value: Any) -> float:
    """Parse a float, handling Italian decimal comma."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    return float(s)


def _int_it(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(_float_it(value))


_ITALIAN_DATE_RE = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$")


def _date_parse(value: Any) -> date:
    """Parse a date from multiple formats: dd/mm/yyyy, yyyy-mm-dd, Excel serial."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return _excel_serial_to_date(value)
    s = str(value).strip()
    m = _ITALIAN_DATE_RE.match(s)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = _ISO_DATE_RE.match(s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # Last resort — try ISO parse
    return date.fromisoformat(s)


def _datetime_parse(value: Any) -> datetime:
    """Parse a datetime, delegating to _date_parse for date-only values."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, (int, float)):
        d = _excel_serial_to_date(value)
        return datetime(d.year, d.month, d.day, tzinfo=UTC)
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        d = _date_parse(s)
        return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _excel_serial_to_date(serial: int | float) -> date:
    """Convert an Excel serial date number to a Python date."""
    # Excel epoch is 1899-12-30 (accounting for the Lotus 1-2-3 leap year bug)
    from datetime import timedelta

    base = date(1899, 12, 30)
    return base + timedelta(days=int(serial))


def _bool_it(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "sì", "si", "vero", "x"):
        return True
    if s in ("0", "false", "no", "falso", ""):
        return False
    msg = f"Cannot coerce {value!r} to bool"
    raise ValueError(msg)


def _strip(value: Any) -> str:
    return str(value).strip()


COERCER_REGISTRY: dict[str, Any] = {
    "float_it": _float_it,
    "int_it": _int_it,
    "date_parse": _date_parse,
    "italian_date": _date_parse,
    "datetime_parse": _datetime_parse,
    "bool_it": _bool_it,
    "strip": _strip,
}

_TYPE_COERCERS: dict[str, Any] = {
    "str": str,
    "int": _int_it,
    "float": _float_it,
    "date": _date_parse,
    "datetime": _datetime_parse,
    "bool": _bool_it,
}


def _coerce_cell(value: Any, mapping: ColumnMapping) -> Any:
    """Coerce a single cell value according to its column mapping."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if mapping.required:
            return None  # caller detects and flags the row
        return mapping.default
    if mapping.coerce and mapping.coerce in COERCER_REGISTRY:
        return COERCER_REGISTRY[mapping.coerce](value)
    return _TYPE_COERCERS.get(mapping.type, str)(value)


def _require_openpyxl() -> Any:
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ConnectorError(
            "openpyxl is required for Excel files. Install with: pip install machina-ai[excel]"
        ) from exc
    return openpyxl


# ------------------------------------------------------------------
# Row reading helpers
# ------------------------------------------------------------------


def _read_xlsx_rows(
    path: Path, sheet_name: str, schema: SheetSchema
) -> tuple[list[str], list[dict[str, Any]]]:
    """Read rows from an .xlsx file, returning (headers, list-of-row-dicts)."""
    openpyxl = _require_openpyxl()
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except PermissionError as exc:
        raise ConnectorLockedError(f"File is locked by another process: {path.name}") from exc

    try:
        if sheet_name not in wb.sheetnames:
            raise ConnectorSchemaError(
                f"Sheet '{sheet_name}' not found in {path.name}. Available sheets: {wb.sheetnames}"
            )
        ws = wb[sheet_name]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            return [], []

        headers = [str(h).strip() if h is not None else "" for h in header_row]
        data: list[dict[str, Any]] = []
        for row in rows_iter:
            row_dict = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
            data.append(row_dict)
        return headers, data
    finally:
        wb.close()


def _read_csv_rows(path: Path, schema: SheetSchema) -> tuple[list[str], list[dict[str, Any]]]:
    """Read rows from a CSV file."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        data = list(reader)
    return headers, data


def _validate_headers(headers: list[str], schema: SheetSchema, source: str) -> None:
    """Check that all required columns from the schema exist in the headers."""
    required_columns = {m.column for m in schema.columns if m.required}
    missing = required_columns - set(headers)
    if missing:
        raise ConnectorSchemaError(
            f"Required columns missing from {source}: {sorted(missing)}. "
            f"Available headers: {headers}"
        )


def _rows_to_dicts(
    raw_rows: list[dict[str, Any]],
    schema: SheetSchema,
    source: str,
) -> list[dict[str, Any]]:
    """Convert raw spreadsheet rows to coerced field dicts, skipping broken rows."""
    results: list[dict[str, Any]] = []
    for row_num, raw in enumerate(raw_rows, start=2):  # row 1 is header
        record: dict[str, Any] = {}
        broken = False
        for mapping in schema.columns:
            cell_value = raw.get(mapping.column)
            try:
                coerced = _coerce_cell(cell_value, mapping)
            except (ValueError, TypeError) as exc:
                if mapping.required:
                    logger.warning(
                        "broken_cell",
                        connector="ExcelCsvConnector",
                        source=source,
                        row_num=row_num,
                        column=mapping.column,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    broken = True
                    break
                logger.debug(
                    "optional_cell_coercion_failed",
                    connector="ExcelCsvConnector",
                    source=source,
                    row_num=row_num,
                    column=mapping.column,
                    error=str(exc),
                )
                coerced = mapping.default
            if coerced is None and mapping.required:
                logger.warning(
                    "missing_required_field",
                    connector="ExcelCsvConnector",
                    source=source,
                    row_num=row_num,
                    column=mapping.column,
                    field=mapping.field,
                )
                broken = True
                break
            record[mapping.field] = coerced
        if not broken:
            results.append(record)
    return results


# ------------------------------------------------------------------
# Write helpers
# ------------------------------------------------------------------


def _append_xlsx_row(
    path: Path, sheet_name: str, schema: SheetSchema, row_data: dict[str, Any]
) -> None:
    """Append a single row to an .xlsx file."""
    openpyxl = _require_openpyxl()
    try:
        wb = openpyxl.load_workbook(str(path))
    except PermissionError as exc:
        raise ConnectorLockedError(f"File is locked by another process: {path.name}") from exc
    except FileNotFoundError:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name
        ws.append([m.column for m in schema.columns])

    if sheet_name not in wb.sheetnames:
        wb.create_sheet(sheet_name)
        wb[sheet_name].append([m.column for m in schema.columns])

    ws = wb[sheet_name]
    row_values = []
    for mapping in schema.columns:
        row_values.append(row_data.get(mapping.field))
    ws.append(row_values)
    wb.save(str(path))
    wb.close()


def _append_csv_row(path: Path, schema: SheetSchema, row_data: dict[str, Any]) -> None:
    """Append a single row to a CSV file."""
    file_exists = path.exists() and path.stat().st_size > 0
    columns = [m.column for m in schema.columns]
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if not file_exists:
            writer.writeheader()
        csv_row = {}
        for mapping in schema.columns:
            csv_row[mapping.column] = row_data.get(mapping.field, "")
        writer.writerow(csv_row)


# ------------------------------------------------------------------
# Connector
# ------------------------------------------------------------------


class ExcelCsvConnector:
    """Connector that treats Excel/CSV files as a CMMS substrate.

    Reads assets and work orders from spreadsheet files using a YAML
    schema mapping.  Writes new work orders by appending rows.

    Args:
        config: Parsed connector configuration.

    Example:
        ```python
        from machina.connectors.docs.excel import ExcelCsvConnector
        from machina.connectors.docs.excel_schema import ExcelConnectorConfig

        config = ExcelConnectorConfig.model_validate(yaml.safe_load(open("excel.yaml")))
        connector = ExcelCsvConnector(config=config)
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
        }
    )

    def __init__(self, *, config: ExcelConnectorConfig) -> None:
        self._config = config
        self._connected = False
        self._asset_cache: list[Asset] = []
        self._wo_cache: list[WorkOrder] = []
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Validate schemas against file headers and load initial data."""
        if self._config.asset_registry:
            self._validate_and_load_assets()
        if self._config.work_orders:
            self._validate_and_load_work_orders()
        self._connected = True
        logger.info(
            "connected",
            connector="ExcelCsvConnector",
            assets_loaded=len(self._asset_cache),
            work_orders_loaded=len(self._wo_cache),
        )

    async def disconnect(self) -> None:
        """Release caches."""
        self._asset_cache.clear()
        self._wo_cache.clear()
        self._connected = False

    async def health_check(self) -> ConnectorHealth:
        """Check that configured files are accessible."""
        issues: list[str] = []
        for label, schema in [
            ("asset_registry", self._config.asset_registry),
            ("work_orders", self._config.work_orders),
        ]:
            if schema is None:
                continue
            p = Path(schema.path)
            if not p.exists():
                issues.append(f"{label}: file not found ({p})")
        if issues:
            return ConnectorHealth(
                status=ConnectorStatus.UNHEALTHY,
                message="; ".join(issues),
            )
        return ConnectorHealth(status=ConnectorStatus.HEALTHY, message="All files accessible")

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def read_assets(self) -> list[Asset]:
        """Return assets from the asset registry spreadsheet."""
        if self._config.asset_registry is None:
            return []
        return list(self._asset_cache)

    async def read_work_orders(self) -> list[WorkOrder]:
        """Return work orders from the work-order spreadsheet."""
        if self._config.work_orders is None:
            return []
        return list(self._wo_cache)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    @sandbox_aware
    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Append a new work order row to the spreadsheet."""
        schema = self._config.work_orders
        if schema is None:
            raise ConnectorConfigError("No work_orders schema configured for writing")
        if schema.write_mode is None:
            raise ConnectorConfigError("work_orders schema has no write_mode configured")

        row_data = self._work_order_to_row(work_order, schema)
        path = Path(schema.path)

        async with self._write_lock:
            await asyncio.to_thread(self._write_row, path, schema, row_data)
        self._wo_cache.append(work_order)
        logger.info(
            "work_order_created",
            connector="ExcelCsvConnector",
            work_order_id=work_order.id,
            asset_id=work_order.asset_id,
        )
        return work_order

    @sandbox_aware
    async def update_work_order(self, work_order_id: str, updates: dict[str, Any]) -> WorkOrder:
        """Update a work order in cache and persist to file if xlsx/csv.

        Note: for xlsx files, a full rewrite is performed. For csv, only
        the cache is updated (append-only format).
        """
        for wo in self._wo_cache:
            if wo.id == work_order_id:
                for key, value in updates.items():
                    if hasattr(wo, key):
                        setattr(wo, key, value)
                schema = self._config.work_orders
                if (
                    schema
                    and schema.write_mode
                    and Path(schema.path).suffix.lower() in (".xlsx", ".xls")
                ):
                    async with self._write_lock:
                        await asyncio.to_thread(self._rewrite_work_orders, schema)
                else:
                    logger.warning(
                        "update_cache_only",
                        connector="ExcelCsvConnector",
                        work_order_id=work_order_id,
                        hint="CSV updates are cache-only until next full rewrite",
                    )
                logger.info(
                    "work_order_updated",
                    connector="ExcelCsvConnector",
                    work_order_id=work_order_id,
                )
                return wo
        raise ConnectorError(f"Work order '{work_order_id}' not found in cache")

    def _rewrite_work_orders(self, schema: SheetSchema) -> None:
        """Rewrite all work orders to the xlsx file from cache."""
        openpyxl = _require_openpyxl()
        path = Path(schema.path)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = schema.sheet
        ws.append([m.column for m in schema.columns])
        for wo in self._wo_cache:
            row_data = self._work_order_to_row(wo, schema)
            row_values = [row_data.get(m.field) for m in schema.columns]
            ws.append(row_values)
        wb.save(str(path))
        wb.close()

    # ------------------------------------------------------------------
    # Cache refresh (called by watcher)
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read files and update caches. Called by the watcher on file changes."""
        if self._config.asset_registry:
            self._validate_and_load_assets()
        if self._config.work_orders:
            self._validate_and_load_work_orders()
        logger.info(
            "cache_refreshed",
            connector="ExcelCsvConnector",
            assets=len(self._asset_cache),
            work_orders=len(self._wo_cache),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _validate_and_load_assets(self) -> None:
        schema = self._config.asset_registry
        assert schema is not None
        path = Path(schema.path)
        if not path.exists():
            raise ConnectorConfigError(f"Asset registry file not found: {path}")
        headers, raw_rows = self._read_file(path, schema)
        _validate_headers(headers, schema, str(path))
        dicts = _rows_to_dicts(raw_rows, schema, str(path))
        self._asset_cache = [_dict_to_asset(d) for d in dicts]

    def _validate_and_load_work_orders(self) -> None:
        schema = self._config.work_orders
        assert schema is not None
        path = Path(schema.path)
        if not path.exists():
            if schema.write_mode is not None:
                self._wo_cache = []
                return
            raise ConnectorConfigError(f"Work order file not found: {path}")
        headers, raw_rows = self._read_file(path, schema)
        _validate_headers(headers, schema, str(path))
        dicts = _rows_to_dicts(raw_rows, schema, str(path))
        self._wo_cache = [_dict_to_work_order(d) for d in dicts]

    @staticmethod
    def _read_file(path: Path, schema: SheetSchema) -> tuple[list[str], list[dict[str, Any]]]:
        suffix = path.suffix.lower()
        if suffix in (".xlsx", ".xls"):
            return _read_xlsx_rows(path, schema.sheet, schema)
        if suffix == ".csv":
            return _read_csv_rows(path, schema)
        raise ConnectorConfigError(f"Unsupported file format: {suffix}")

    @staticmethod
    def _write_row(path: Path, schema: SheetSchema, row_data: dict[str, Any]) -> None:
        suffix = path.suffix.lower()
        if suffix in (".xlsx", ".xls"):
            _append_xlsx_row(path, schema.sheet, schema, row_data)
        elif suffix == ".csv":
            _append_csv_row(path, schema, row_data)
        else:
            raise ConnectorConfigError(f"Unsupported file format for writing: {suffix}")

    @staticmethod
    def _work_order_to_row(wo: WorkOrder, schema: SheetSchema) -> dict[str, Any]:
        wo_dict = wo.model_dump()
        row: dict[str, Any] = {}
        for mapping in schema.columns:
            value = wo_dict.get(mapping.field)
            if isinstance(value, (datetime, date)):
                value = value.isoformat()
            elif isinstance(value, StrEnum):
                value = value.value
            row[mapping.field] = value
        return row
