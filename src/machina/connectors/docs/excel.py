"""ExcelCsvConnector — YAML-schema-driven Excel/CSV adapter.

Treats a directory of spreadsheets as a CMMS: reads assets and work
orders, appends new work orders.  Schema mapping is defined in YAML
so the user writes zero Python.
"""

from __future__ import annotations

import asyncio
import codecs
import contextvars
import csv
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, TypeVarTuple

import structlog
from pydantic import ValidationError

from machina.connectors._entity_builders import LIST_CELL_DELIMITER
from machina.connectors._entity_builders import dict_to_asset as _dict_to_asset
from machina.connectors._entity_builders import dict_to_failure_mode as _dict_to_failure_mode
from machina.connectors._entity_builders import dict_to_work_order as _dict_to_work_order
from machina.connectors._settings import validate_settings
from machina.connectors.base import ConnectorHealth, ConnectorStatus, sandbox_aware
from machina.connectors.capabilities import Capability

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future

    from machina.connectors.docs.excel_schema import (
        ColumnMapping,
        ExcelConnectorConfig,
        SheetSchema,
    )
    from machina.domain.asset import Asset
    from machina.domain.failure_mode import FailureMode
from machina.domain.work_order import WorkOrder, WorkOrderStatus
from machina.exceptions import (
    ConnectorConfigError,
    ConnectorError,
    ConnectorLockedError,
    ConnectorSchemaError,
)

logger = structlog.get_logger(__name__)

_T = TypeVar("_T")
_Ts = TypeVarTuple("_Ts")

# How long disconnect() waits for file access (a write, a refresh) still running.
_DISCONNECT_WAIT_SEC = 5.0


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


# Leading characters a spreadsheet (Excel/LibreOffice) interprets as the start
# of a formula. A cell value beginning with one of these is a CSV/formula-
# injection vector when the exported file is opened in a spreadsheet app.
_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")


def _guard_formula(value: str) -> str:
    """Neutralize a leading formula trigger by prefixing an apostrophe.

    The apostrophe makes spreadsheets treat the cell as text (and stops
    openpyxl from storing it as a real formula). Reversed by
    :func:`_strip_formula_guard` on read so values round-trip unchanged.

    A value that *already* starts with an apostrophe followed by a trigger
    (or another apostrophe) is also escaped with an extra leading apostrophe,
    so the strip on read is unambiguous and the pair is a true inverse — a
    legitimate ``"'=approved"`` is not corrupted into ``"=approved"``.
    """
    head = value[:1]
    if head in _FORMULA_PREFIXES:
        return "'" + value
    if head == "'" and value[1:2] in (*_FORMULA_PREFIXES, "'"):
        return "'" + value
    return value


def _strip_formula_guard(value: str) -> str:
    """Reverse :func:`_guard_formula` so guarded values read back intact.

    Strips exactly one leading apostrophe when it is followed by a formula
    trigger or another apostrophe — the only shapes :func:`_guard_formula`
    ever produces — leaving genuine values like ``"'note"`` untouched.
    """
    if value[:1] == "'" and value[1:2] in (*_FORMULA_PREFIXES, "'"):
        return value[1:]
    return value


def _coerce_cell(value: Any, mapping: ColumnMapping) -> Any:
    """Coerce a single cell value according to its column mapping."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if mapping.required:
            return None  # caller detects and flags the row
        return mapping.default
    if mapping.coerce and mapping.coerce in COERCER_REGISTRY:
        result = COERCER_REGISTRY[mapping.coerce](value)
    else:
        result = _TYPE_COERCERS.get(mapping.type, str)(value)
    if isinstance(result, str):
        result = _strip_formula_guard(result)
    return result


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


def _xlsx_header(ws: Any) -> list[str]:
    """Return a worksheet's header row, normalized like the read path."""
    if ws.max_row < 1:
        return []
    return [str(c.value).strip() if c.value is not None else "" for c in ws[1]]


def _save_xlsx_atomically(wb: Any, path: Path) -> None:
    """Save to a temp sibling, then replace the target, so a crash cannot truncate it."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        wb.save(str(tmp))
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _log_unwritten_fields(path: Path, fields: list[str]) -> None:
    if fields:
        logger.warning(
            "work_order_fields_not_persisted",
            connector="ExcelCsvConnector",
            operation="create_work_order",
            file=path.name,
            fields=fields,
            hint="the file has no column for these mapped fields",
        )


def _append_xlsx_row(
    path: Path, sheet_name: str, schema: SheetSchema, row_data: dict[str, Any]
) -> None:
    """Append one row to an .xlsx sheet, placing each value under its header.

    Values go to the column whose header names them, whatever the column
    order in the file, and other sheets, rows and columns are left as they
    are. A missing file or sheet is created with the schema's header row.
    """
    openpyxl = _require_openpyxl()
    try:
        wb = openpyxl.load_workbook(str(path))
    except PermissionError as exc:
        raise _file_write_error(exc, path) from exc
    except FileNotFoundError:
        wb = openpyxl.Workbook()
        wb.active.title = sheet_name

    unwritten: list[str] = []
    try:
        if sheet_name not in wb.sheetnames:
            wb.create_sheet(sheet_name)
        ws = wb[sheet_name]
        header = _xlsx_header(ws)
        if not any(header):
            header = [m.column for m in schema.columns]
            for col, name in enumerate(header, start=1):
                ws.cell(row=1, column=col, value=name)
        row_values: list[Any] = [None] * len(header)
        for mapping in schema.columns:
            value = row_data.get(mapping.field)
            if mapping.column in header:
                row_values[header.index(mapping.column)] = value
            elif value not in (None, ""):
                unwritten.append(mapping.field)
        ws.append(row_values)
        _save_xlsx_atomically(wb, path)
    finally:
        wb.close()
    _log_unwritten_fields(path, unwritten)


def _append_csv_row(path: Path, schema: SheetSchema, row_data: dict[str, Any]) -> None:
    """Append one row to a CSV file, placing each value under its header.

    An existing file keeps its own column order and extra columns; a missing
    or empty file is started with the schema's header row.
    """
    header: list[str] = []
    needs_newline = False
    if path.exists() and path.stat().st_size > 0:
        with path.open(newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f), [])
        with path.open("rb") as fb:
            fb.seek(-1, 2)
            needs_newline = fb.read(1) not in (b"\n", b"\r")
    write_header = not header
    if write_header:
        header = [m.column for m in schema.columns]
    row = dict.fromkeys(header, "")
    unwritten = []
    for mapping in schema.columns:
        value = row_data.get(mapping.field, "")
        if mapping.column in row:
            row[mapping.column] = value
        elif value not in (None, ""):
            unwritten.append(mapping.field)
    with path.open("a", newline="", encoding="utf-8") as f:
        if needs_newline:
            f.write("\r\n")
        writer = csv.DictWriter(f, fieldnames=header)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    _log_unwritten_fields(path, unwritten)


def _update_xlsx_row(
    path: Path, schema: SheetSchema, id_column: str, work_order_id: str, values: dict[str, Any]
) -> None:
    """Set the given column values on the row of one work order, in place.

    Loads the workbook, finds the row whose ``id_column`` holds
    ``work_order_id``, writes only the cells in ``values`` (column header →
    value) and saves atomically. Other sheets, rows and columns are left as
    they are.

    Raises:
        ConnectorSchemaError: If the sheet or a needed column is missing.
        ConnectorError: If no row holds the work order.
    """
    openpyxl = _require_openpyxl()
    try:
        wb = openpyxl.load_workbook(str(path))
    except PermissionError as exc:
        raise _file_write_error(exc, path) from exc
    try:
        if schema.sheet not in wb.sheetnames:
            raise ConnectorSchemaError(f"Sheet '{schema.sheet}' not found in {path.name}")
        ws = wb[schema.sheet]
        index = {name: col for col, name in enumerate(_xlsx_header(ws), start=1) if name}
        for column in (id_column, *values):
            if column not in index:
                raise ConnectorSchemaError(f"Column '{column}' not found in {path.name}")
        target = None
        for row in range(2, ws.max_row + 1):
            cell = ws.cell(row=row, column=index[id_column]).value
            if cell is not None and _strip_formula_guard(str(cell).strip()) == work_order_id:
                target = row
                break
        if target is None:
            raise ConnectorError(f"Work order '{work_order_id}' not found in {path.name}")
        for column, value in values.items():
            ws.cell(row=target, column=index[column], value=value)
        _save_xlsx_atomically(wb, path)
    finally:
        wb.close()


def _update_csv_row(
    path: Path, id_column: str, work_order_id: str, values: dict[str, Any]
) -> None:
    """CSV twin of :func:`_update_xlsx_row`: rewrite the file with one row changed.

    Every other row and column is written back unchanged, via a temp sibling
    and an atomic replace.
    """
    with path.open("rb") as fb:
        # Excel on Windows reads BOM-less UTF-8 as the ANSI code page and
        # garbles accented text, so a file that had a BOM keeps it.
        has_bom = fb.read(len(codecs.BOM_UTF8)) == codecs.BOM_UTF8
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ConnectorError(f"Work order '{work_order_id}' not found in {path.name}")
    header = rows[0]
    index = {name: col for col, name in enumerate(header)}
    for column in (id_column, *values):
        if column not in index:
            raise ConnectorSchemaError(f"Column '{column}' not found in {path.name}")
    target = next(
        (
            row
            for row in rows[1:]
            if len(row) > index[id_column]
            and _strip_formula_guard(row[index[id_column]].strip()) == work_order_id
        ),
        None,
    )
    if target is None:
        raise ConnectorError(f"Work order '{work_order_id}' not found in {path.name}")
    target.extend([""] * (len(header) - len(target)))
    for column, value in values.items():
        target[index[column]] = "" if value is None else value
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8-sig" if has_bom else "utf-8") as f:
            csv.writer(f).writerows(rows)
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _file_write_error(exc: OSError, path: Path) -> ConnectorError:
    """Map a write-side OS error onto the connector error contract."""
    if isinstance(exc, PermissionError):
        return ConnectorLockedError(
            f"Cannot write {path.name}: it is open in another program, "
            "or this process lacks write permission"
        )
    return ConnectorError(f"Could not write {path.name}: {exc.strerror or exc}")


# ------------------------------------------------------------------
# Connector
# ------------------------------------------------------------------


class ExcelCsvConnector:
    """Connector that treats Excel/CSV files as a CMMS substrate.

    Reads assets, work orders, and (optionally) a failure-mode catalog
    from spreadsheet files using a YAML schema mapping.  Writes new work
    orders by appending rows.  Multi-valued cells (asset failure-code
    linkage, failure-mode list fields) use a semicolon-delimited string,
    e.g. ``"BEAR-WEAR-01;SEAL-LEAK-01"``.

    Args:
        config: Parsed connector configuration.
        **settings: Alternatively, the same configuration as flat keyword
            arguments (``asset_registry=``, ``work_orders=``,
            ``failure_modes=``, ``watcher=``) — the shape a ``machina.yaml``
            ``settings`` block carries. Pass either ``config`` or settings.

    Raises:
        ConnectorConfigError: If both forms are given, or the settings do
            not validate.

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

    # Capabilities available regardless of configuration. The write capabilities
    # (CREATE_WORK_ORDER / UPDATE_WORK_ORDER) and READ_FAILURE_MODES are
    # config-driven and added in __init__ — they are NOT part of the base set.
    # A declared-but-unserviceable write would advertise a capability the
    # connector raises on, so writes are gated behind a configured, writable
    # work_orders sheet (mirroring how GenericSqlConnector gates its writes).
    _BASE_CAPABILITIES: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.READ_ASSETS,
            Capability.READ_WORK_ORDERS,
        }
    )

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Return capabilities based on configuration.

        Base capabilities (asset/work-order reads) are always available.
        ``CREATE_WORK_ORDER`` / ``UPDATE_WORK_ORDER`` are declared only when a
        ``work_orders`` sheet with a ``write_mode`` is configured — otherwise
        the write methods raise ``ConnectorConfigError``, so an undeclared
        write is the honest signal. ``READ_FAILURE_MODES`` is declared only
        when a ``failure_modes`` sheet is configured. Unconfigured means
        not-declared, so capability discovery is a true signal of what this
        connector can actually serve.
        """
        return self._capabilities

    def __init__(
        self,
        *,
        config: ExcelConnectorConfig | dict[str, Any] | None = None,
        **settings: Any,
    ) -> None:
        from machina.connectors.docs.excel_schema import ExcelConnectorConfig

        if isinstance(config, dict):
            # ``settings: {config: {...}}`` in YAML — same shape, nested.
            config = validate_settings(ExcelConnectorConfig, config)
        if config is None:
            config = validate_settings(ExcelConnectorConfig, settings)
        elif settings:
            raise ConnectorConfigError(
                "ExcelCsvConnector takes either config= or flat settings, not both"
            )
        self._config = config
        caps = set(self._BASE_CAPABILITIES)
        # Writes are serviceable only with a writable work_orders sheet: both
        # create_work_order and durable update_work_order require a write_mode.
        if config.work_orders is not None and config.work_orders.write_mode is not None:
            caps |= {Capability.CREATE_WORK_ORDER, Capability.UPDATE_WORK_ORDER}
        if config.failure_modes is not None:
            caps.add(Capability.READ_FAILURE_MODES)
        self._capabilities = frozenset(caps)
        self._connected = False
        self._asset_cache: list[Asset] = []
        self._wo_cache: list[WorkOrder] = []
        self._fm_cache: list[FailureMode] = []
        # Every file access runs on this one thread, one at a time: the loads
        # of connect() and refresh(), and every work-order write (see
        # _run_on_file_thread). A write whose caller was cancelled still holds
        # the file until it is done, and the next access waits for it.
        self._file_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="machina-excel")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Validate schemas against file headers and load initial data.

        The files are read on the connector's file thread, in turn with
        writes and refreshes, so the event loop runs on meanwhile.
        """
        await self._run_on_file_thread(self._load_sheets)
        self._connected = True
        logger.info(
            "connected",
            connector="ExcelCsvConnector",
            assets_loaded=len(self._asset_cache),
            work_orders_loaded=len(self._wo_cache),
            failure_modes_loaded=len(self._fm_cache),
        )

    async def disconnect(self) -> None:
        """Wait for file access still running, then release caches.

        A write keeps running when its caller is cancelled. ``disconnect()``
        waits up to 5 seconds for it, and for a refresh in progress, so that
        once it returns the connector is not using its files; past that, it
        logs ``file_access_still_running`` and goes on.
        """
        try:
            await asyncio.wait_for(
                self._run_on_file_thread(lambda: None), timeout=_DISCONNECT_WAIT_SEC
            )
        except TimeoutError:
            logger.warning(
                "file_access_still_running",
                connector="ExcelCsvConnector",
                operation="disconnect",
                timeout_sec=_DISCONNECT_WAIT_SEC,
            )
        finally:
            # New lists, not clear(): a write still running holds the old ones.
            self._asset_cache, self._wo_cache, self._fm_cache = [], [], []
            self._connected = False

    async def health_check(self) -> ConnectorHealth:
        """Check that configured files are accessible."""
        issues: list[str] = []
        for label, schema in [
            ("asset_registry", self._config.asset_registry),
            ("work_orders", self._config.work_orders),
            ("failure_modes", self._config.failure_modes),
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
        """Return assets from the asset registry spreadsheet.

        Raises:
            ConnectorError: If a registry sheet is configured but the
                connector is not connected (or failed to), so a caller never
                mistakes "not loaded" for "no assets".
        """
        if self._config.asset_registry is None:
            return []
        if not self._connected:
            raise ConnectorError("Not connected — call connect() before reading")
        return list(self._asset_cache)

    async def get_asset(self, asset_id: str) -> Asset | None:
        """Return one asset from the registry spreadsheet, or ``None``.

        Raises:
            ConnectorError: If the connector is not connected, so a caller
                never mistakes "not loaded yet" for "no such asset".
        """
        if not self._connected:
            raise ConnectorError("Not connected — call connect() before reading")
        for asset in self._asset_cache:
            if asset.id == asset_id:
                return asset
        return None

    async def read_work_orders(
        self,
        *,
        asset_id: str = "",
        status: WorkOrderStatus | str = "",
    ) -> list[WorkOrder]:
        """Return work orders from the work-order spreadsheet.

        Args:
            asset_id: Keep only work orders for this asset.
            status: Keep only work orders in this status (enum or its value).

        Raises:
            ConnectorError: If a work-order sheet is configured but the
                connector is not connected.
        """
        if self._config.work_orders is None:
            return []
        if not self._connected:
            raise ConnectorError("Not connected — call connect() before reading")
        wanted_status = str(getattr(status, "value", status) or "")
        return [
            wo
            for wo in self._wo_cache
            if (not asset_id or wo.asset_id == asset_id)
            and (not wanted_status or getattr(wo.status, "value", wo.status) == wanted_status)
        ]

    async def read_failure_modes(self) -> list[FailureMode]:
        """Return failure modes from the failure-modes spreadsheet.

        Returns an empty list when no ``failure_modes`` sheet is
        configured (the capability is then not declared either).

        Raises:
            ConnectorError: If a sheet is configured but the connector is
                not connected — matching the cross-substrate harvest
                contract, so a configured catalog never silently reads
                as "no failure-mode data configured".
        """
        if self._config.failure_modes is None:
            return []
        if not self._connected:
            raise ConnectorError("Not connected — call connect() before reading")
        return list(self._fm_cache)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    @sandbox_aware
    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        """Append a new work order row to the spreadsheet.

        The sheet is re-read first, so rows added or removed by other
        programs since ``connect()`` count. Idempotent on the work-order ID:
        re-creating a work order whose ID is already in the sheet returns the
        existing record instead of appending a duplicate row. The agent
        runtime and the MCP tools derive deterministic IDs and rely on this
        to collapse retries. The new row's values go under the matching
        column headers, whatever the column order in the file.

        Writes run one at a time. Cancelling the caller does not stop a write
        that has started: it still finishes, and the next write waits for it,
        so a retry after a cancellation finds the row instead of adding it
        twice.

        Raises:
            ConnectorConfigError: If no writable work-orders sheet is configured.
            ConnectorLockedError: If the file is open in another program.
            ConnectorError: If the file cannot be read or written.
        """
        schema = self._config.work_orders
        if schema is None:
            raise ConnectorConfigError("No work_orders schema configured for writing")
        if schema.write_mode is None:
            raise ConnectorConfigError("work_orders schema has no write_mode configured")

        row_data = self._work_order_to_row(work_order, schema)
        return await self._run_on_file_thread(
            self._append_unless_present, schema, work_order, row_data
        )

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
        """Update a work order and persist the changed cells to the file.

        Accepts the changes as an ``updates`` dict, as keyword arguments (the
        shape the MCP tools use), or both; keyword values override dict
        entries. A status change goes through :meth:`WorkOrder.transition_to`,
        so only the work-order lifecycle's allowed transitions succeed; asking
        for the status the work order already has changes nothing, so a
        retried update succeeds.

        When ``write_mode`` is configured, the sheet is re-read and only the
        changed cells of the work order's row are written, in place: other
        rows, columns and sheets are left as they are, and the write goes
        through a temp file and an atomic replace. A field that is not a
        ``WorkOrder`` field, a change of ``id``, and a change to a field that
        no column is mapped to are refused rather than silently dropped. If
        the write fails, the cached work order is left as it was. When no
        ``write_mode`` is set, the update is kept in cache only. As with
        :meth:`create_work_order`, a write that has started finishes even if
        the caller is cancelled, and the next write waits for it.

        Raises:
            ConnectorError: If the work order is unknown, the status is not a
                valid or allowed transition, a changed field has no column, or
                the file cannot be written (:class:`ConnectorLockedError` when
                it is open elsewhere).
        """
        changes = dict(updates or {})
        for key, value in (
            ("status", status),
            ("assigned_to", assigned_to),
            ("description", description),
        ):
            if value is not None:
                changes[key] = value
        new_status: WorkOrderStatus | None = None
        if "status" in changes:
            raw_status = changes.pop("status")
            try:
                new_status = WorkOrderStatus(getattr(raw_status, "value", raw_status))
            except ValueError as exc:
                raise ConnectorError(f"Invalid work order status {raw_status!r}") from exc
        unknown = sorted(key for key in changes if key not in WorkOrder.model_fields)
        if unknown:
            raise ConnectorError(f"Unknown work order field(s): {', '.join(unknown)}")
        if "id" in changes and changes["id"] != work_order_id:
            raise ConnectorError("A work order's id cannot be changed")

        schema = self._config.work_orders
        persist = schema is not None and schema.write_mode is not None
        return await self._run_on_file_thread(
            self._apply_update, work_order_id, new_status, changes, schema if persist else None
        )

    def _append_unless_present(
        self, schema: SheetSchema, work_order: WorkOrder, row_data: dict[str, Any]
    ) -> WorkOrder:
        """Append a work order's row unless the sheet already holds its ID.

        Returns the stored record when it does, else ``work_order``. Runs on
        the file thread: the re-read, the ID check, the append, the cache
        update and the log line are one step that no other write can split
        and that cancelling the caller does not cut short.
        """
        path = Path(schema.path)
        # The file, not the connect-time cache, is the source of truth.
        self._reload_work_orders_for_write(path)
        cache = self._wo_cache  # the list just loaded, even if disconnect() swaps it
        existing = next((wo for wo in cache if wo.id == work_order.id), None)
        if existing is not None:
            logger.info(
                "work_order_create_idempotent_hit",
                connector="ExcelCsvConnector",
                operation="create_work_order",
                work_order_id=work_order.id,
                asset_id=work_order.asset_id,
            )
            return existing
        try:
            self._write_row(path, schema, row_data)
        except OSError as exc:
            raise _file_write_error(exc, path) from exc
        cache.append(work_order)
        logger.info(
            "work_order_created",
            connector="ExcelCsvConnector",
            operation="create_work_order",
            work_order_id=work_order.id,
            asset_id=work_order.asset_id,
        )
        return work_order

    def _apply_update(
        self,
        work_order_id: str,
        new_status: WorkOrderStatus | None,
        changes: dict[str, Any],
        schema: SheetSchema | None,
    ) -> WorkOrder:
        """Update a copy of the cached work order, write it, then cache it.

        ``schema`` is the sheet whose row receives the changed cells, or
        ``None`` to keep the update in cache only. Runs on the file thread:
        the re-read, the change, the row rewrite, the cache update and the
        log lines are one step that no other write can split and that
        cancelling the caller does not cut short. If any part fails, the
        cached work order is left as it was.
        """
        if schema is not None:
            self._reload_work_orders_for_write(Path(schema.path))
        cache = self._wo_cache  # the list just loaded, even if disconnect() swaps it
        idx = next((i for i, wo in enumerate(cache) if wo.id == work_order_id), None)
        if idx is None:
            raise ConnectorError(f"Work order '{work_order_id}' not found")
        wo = cache[idx].model_copy(deep=True)
        changed: set[str] = set()
        if new_status is not None and new_status != wo.status:
            try:
                wo.transition_to(new_status)
            except ValueError as exc:
                raise ConnectorError(str(exc)) from exc
            changed |= {"status", "updated_at"}
        for key, value in changes.items():
            setattr(wo, key, value)
            changed.add(key)
        if schema is not None and changed:
            try:
                self._update_row_in_file(schema, wo, changed)
            except OSError as exc:
                raise _file_write_error(exc, Path(schema.path)) from exc
        cache[idx] = wo
        if schema is None:
            logger.warning(
                "update_not_persisted",
                connector="ExcelCsvConnector",
                operation="update_work_order",
                work_order_id=work_order_id,
                asset_id=wo.asset_id,
                hint="no write_mode configured — update kept in cache only",
            )
        logger.info(
            "work_order_updated",
            connector="ExcelCsvConnector",
            operation="update_work_order",
            work_order_id=work_order_id,
            asset_id=wo.asset_id,
        )
        return wo

    def _update_row_in_file(self, schema: SheetSchema, wo: WorkOrder, fields: set[str]) -> None:
        """Write the given fields of one work order to its row, in place.

        Raises:
            ConnectorError: If a changed field has no mapped column, the
                schema maps no ID column, or the row is not in the file.
        """
        by_field = {m.field: m.column for m in schema.columns}
        # updated_at is written when mapped and skipped otherwise; every other
        # changed field must have a column, or the change would be lost.
        unmapped = sorted(f for f in fields if f not in by_field and f != "updated_at")
        if unmapped:
            raise ConnectorError(
                f"Cannot persist {', '.join(unmapped)} to {Path(schema.path).name}: "
                "no column is mapped to it in the work_orders schema"
            )
        if "id" not in by_field:
            raise ConnectorSchemaError("The work_orders schema maps no column to 'id'")
        row = self._work_order_to_row(wo, schema)
        values = {by_field[f]: row.get(f) for f in sorted(fields) if f in by_field}
        path = Path(schema.path)
        if path.suffix.lower() == ".csv":
            _update_csv_row(path, by_field["id"], wo.id, values)
        else:
            _update_xlsx_row(path, schema, by_field["id"], wo.id, values)

    # ------------------------------------------------------------------
    # Cache refresh (called by watcher)
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read files and update caches. Called by the watcher on file changes.

        All-or-nothing: if any sheet fails to load mid-refresh (file
        mid-save, locked, header change), every cache is restored to its
        pre-refresh snapshot so assets and the failure-mode catalog never
        end up mutually inconsistent.

        The files are read on the connector's file thread, in turn with
        writes: a refresh waits for a write already running or queued, and a
        later write waits for the refresh. The call blocks until the refresh
        is done, which is how ``FileWatcher.stop()`` waits for it; from a
        coroutine, run it with ``asyncio.to_thread``. Never call it from the
        file thread itself (code run by ``_run_on_file_thread``): it would
        wait for its own turn forever.
        """
        self._submit_to_file_thread(self._reload_sheets).result()

    def _reload_sheets(self) -> None:
        """Body of :meth:`refresh`, on the file thread: reload, all-or-nothing."""
        snapshot = (
            list(self._asset_cache),
            list(self._wo_cache),
            list(self._fm_cache),
        )
        try:
            self._load_sheets()
        except Exception:
            self._asset_cache, self._wo_cache, self._fm_cache = snapshot
            raise
        logger.info(
            "cache_refreshed",
            connector="ExcelCsvConnector",
            assets=len(self._asset_cache),
            work_orders=len(self._wo_cache),
            failure_modes=len(self._fm_cache),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_sheets(self) -> None:
        """Load every configured sheet into its cache. Runs on the file thread."""
        if self._config.asset_registry:
            self._validate_and_load_assets()
        if self._config.work_orders:
            self._validate_and_load_work_orders()
        if self._config.failure_modes:
            self._validate_and_load_failure_modes()

    def _load_sheet_dicts(self, schema: SheetSchema, label: str) -> list[dict[str, Any]]:
        """Shared exists-check → read → validate → parse pipeline for one sheet."""
        for col in schema.columns:
            if col.coerce and col.coerce not in COERCER_REGISTRY:
                raise ConnectorConfigError(
                    f"Unknown coerce '{col.coerce}' for column '{col.column}' "
                    f"({label}) — known coercers: {sorted(COERCER_REGISTRY)}. "
                    "For plain type conversion use the 'type' field instead."
                )
        path = Path(schema.path)
        if not path.exists():
            raise ConnectorConfigError(f"{label} file not found: {path}")
        headers, raw_rows = self._read_file(path, schema)
        _validate_headers(headers, schema, str(path))
        return _rows_to_dicts(raw_rows, schema, str(path))

    def _validate_and_load_assets(self) -> None:
        schema = self._config.asset_registry
        assert schema is not None
        dicts = self._load_sheet_dicts(schema, "Asset registry")
        self._asset_cache = [_dict_to_asset(d) for d in dicts]

    def _validate_and_load_failure_modes(self) -> None:
        schema = self._config.failure_modes
        assert schema is not None
        dicts = self._load_sheet_dicts(schema, "Failure modes")
        self._fm_cache = [_dict_to_failure_mode(d) for d in dicts]

    def _validate_and_load_work_orders(self) -> None:
        schema = self._config.work_orders
        assert schema is not None
        if not Path(schema.path).exists() and schema.write_mode is not None:
            self._wo_cache = []
            return
        dicts = self._load_sheet_dicts(schema, "Work order")
        work_orders: list[WorkOrder] = []
        for d in dicts:
            try:
                work_orders.append(_dict_to_work_order(d))
            except ValidationError as exc:
                # A row typed by hand with, e.g., an unknown status must not make
                # the whole sheet unreadable — nor block every later write.
                logger.warning(
                    "invalid_work_order_row_skipped",
                    connector="ExcelCsvConnector",
                    source=Path(schema.path).name,
                    work_order_id=str(d.get("id", "")),
                    fields=sorted({str(err["loc"][0]) for err in exc.errors() if err["loc"]}),
                )
        self._wo_cache = work_orders

    async def _run_on_file_thread(self, func: Callable[[*_Ts], _T], /, *args: *_Ts) -> _T:
        """Run ``func(*args)`` on the connector's file thread, after earlier calls.

        One thread runs every call, so no two calls overlap, whatever happens
        to their callers. A cancelled caller (an MCP request cancellation, a
        workflow step timeout) cannot stop a call that is already running, and
        the calls behind it wait for it; a call still queued when the
        cancellation reaches the thread pool, on the event loop's next
        iteration, is dropped.
        """
        return await asyncio.wrap_future(self._submit_to_file_thread(func, *args))

    def _submit_to_file_thread(self, func: Callable[[*_Ts], _T], /, *args: *_Ts) -> Future[_T]:
        """Queue ``func(*args)`` on the file thread, in a copy of the caller's context."""
        context = contextvars.copy_context()  # as asyncio.to_thread does
        return self._file_thread.submit(lambda: context.run(func, *args))

    def _reload_work_orders_for_write(self, path: Path) -> None:
        """Re-read the work-order sheet at the start of a write, on the file thread.

        Raises:
            ConnectorLockedError: If the file is open in another program.
            ConnectorError: If the file cannot be read (unreadable, corrupt).
        """
        try:
            self._validate_and_load_work_orders()
        except ConnectorError:
            raise
        except OSError as exc:
            raise _file_write_error(exc, path) from exc
        except Exception as exc:
            raise ConnectorError(f"Cannot read {path.name} before writing to it: {exc}") from exc

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
            elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                # The multi-value cell encoding the read side splits on.
                value = LIST_CELL_DELIMITER.join(value)
            if isinstance(value, str):
                value = _guard_formula(value)
            row[mapping.field] = value
        return row
