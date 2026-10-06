"""Tests for ExcelCsvConnector — read, write, coercion edge cases."""

from __future__ import annotations

import asyncio
import codecs
import logging
import shutil
import threading
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, MagicMock

import pytest

pytest.importorskip("openpyxl")

from machina.connectors.capabilities import Capability
from machina.connectors.docs import excel
from machina.connectors.docs.excel import (
    COERCER_REGISTRY,
    ExcelCsvConnector,
    _bool_it,
    _coerce_cell,
    _date_parse,
    _datetime_parse,
    _excel_serial_to_date,
    _float_it,
    _guard_formula,
    _int_it,
    _strip_formula_guard,
)
from machina.connectors.docs.excel_schema import (
    ColumnMapping,
    ExcelConnectorConfig,
    SheetSchema,
)
from machina.domain.asset import AssetType, Criticality
from machina.domain.work_order import Priority, WorkOrder, WorkOrderStatus, WorkOrderType
from machina.exceptions import (
    ConnectorConfigError,
    ConnectorError,
    ConnectorLockedError,
    ConnectorSchemaError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "excel"


# ------------------------------------------------------------------
# Coercer unit tests
# ------------------------------------------------------------------


class TestFloatIt:
    def test_int_input(self) -> None:
        assert _float_it(42) == 42.0

    def test_float_input(self) -> None:
        assert _float_it(3.14) == 3.14

    def test_italian_decimal_comma(self) -> None:
        assert _float_it("3,14") == 3.14

    def test_normal_dot(self) -> None:
        assert _float_it("3.14") == 3.14

    def test_whitespace(self) -> None:
        assert _float_it("  42.5  ") == 42.5

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            _float_it("not a number")


class TestIntIt:
    def test_int(self) -> None:
        assert _int_it(42) == 42

    def test_float_truncates(self) -> None:
        assert _int_it(3.9) == 3

    def test_string(self) -> None:
        assert _int_it("7") == 7


class TestDateParse:
    def test_italian_dd_mm_yyyy(self) -> None:
        assert _date_parse("15/03/2019") == date(2019, 3, 15)

    def test_iso_yyyy_mm_dd(self) -> None:
        assert _date_parse("2020-06-22") == date(2020, 6, 22)

    def test_dd_dot_mm_dot_yyyy(self) -> None:
        assert _date_parse("15.03.2019") == date(2019, 3, 15)

    def test_dd_dash_mm_dash_yyyy(self) -> None:
        assert _date_parse("15-03-2019") == date(2019, 3, 15)

    def test_excel_serial(self) -> None:
        assert _date_parse(43831) == date(2020, 1, 1)

    def test_datetime_input(self) -> None:
        dt = datetime(2020, 6, 22, 14, 30)
        assert _date_parse(dt) == date(2020, 6, 22)

    def test_date_input(self) -> None:
        d = date(2020, 6, 22)
        assert _date_parse(d) == d

    def test_invalid_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _date_parse("not-a-date")


class TestDatetimeParse:
    def test_iso_string(self) -> None:
        result = _datetime_parse("2020-06-22T14:30:00")
        assert result.year == 2020
        assert result.month == 6
        assert result.tzinfo is not None

    def test_date_only_string(self) -> None:
        result = _datetime_parse("15/03/2019")
        assert result.date() == date(2019, 3, 15)

    def test_datetime_input(self) -> None:
        dt = datetime(2020, 1, 1, 12, 0)
        result = _datetime_parse(dt)
        assert result.tzinfo is not None


class TestExcelSerialToDate:
    def test_epoch(self) -> None:
        assert _excel_serial_to_date(1) == date(1899, 12, 31)

    def test_known_date(self) -> None:
        assert _excel_serial_to_date(43831) == date(2020, 1, 1)

    def test_recent_date(self) -> None:
        assert _excel_serial_to_date(45000) == date(2023, 3, 15)


class TestBoolIt:
    @pytest.mark.parametrize("val", ["1", "true", "yes", "sì", "si", "vero", "x", "True", "YES"])
    def test_truthy(self, val: str) -> None:
        assert _bool_it(val) is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "falso", "", "False", "NO"])
    def test_falsy(self, val: str) -> None:
        assert _bool_it(val) is False

    def test_bool_passthrough(self) -> None:
        assert _bool_it(True) is True
        assert _bool_it(False) is False

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot coerce"):
            _bool_it("maybe")


class TestCoerceCell:
    def test_empty_string_non_required_returns_default(self) -> None:
        m = ColumnMapping(column="X", field="x", default="fallback")
        assert _coerce_cell("", m) == "fallback"

    def test_none_non_required_returns_default(self) -> None:
        m = ColumnMapping(column="X", field="x", default=0)
        assert _coerce_cell(None, m) == 0

    def test_empty_required_returns_none(self) -> None:
        m = ColumnMapping(column="X", field="x", required=True)
        assert _coerce_cell("", m) is None

    def test_named_coercer(self) -> None:
        m = ColumnMapping(column="X", field="x", coerce="float_it")
        assert _coerce_cell("3,14", m) == 3.14

    def test_type_coercion_fallback(self) -> None:
        m = ColumnMapping(column="X", field="x", type="float")
        assert _coerce_cell("42.5", m) == 42.5


class TestCoercerRegistry:
    def test_all_registered(self) -> None:
        expected = {
            "float_it",
            "int_it",
            "date_parse",
            "italian_date",
            "datetime_parse",
            "bool_it",
            "strip",
        }
        assert expected == set(COERCER_REGISTRY.keys())


# ------------------------------------------------------------------
# Connector integration with fixtures
# ------------------------------------------------------------------


def _asset_schema() -> SheetSchema:
    return SheetSchema(
        path=str(FIXTURES / "pmi_asset_registry_sample.xlsx"),
        sheet="Macchine",
        columns=[
            ColumnMapping(column="Codice", field="id", required=True),
            ColumnMapping(column="Nome", field="name", required=True),
            ColumnMapping(column="Tipo", field="type"),
            ColumnMapping(column="Ubicazione", field="location"),
            ColumnMapping(column="Costruttore", field="manufacturer"),
            ColumnMapping(column="Modello", field="model"),
            ColumnMapping(column="Matricola", field="serial_number"),
            ColumnMapping(column="Data Installazione", field="install_date", coerce="date_parse"),
            ColumnMapping(column="Criticità", field="criticality"),
            ColumnMapping(column="Padre", field="parent"),
        ],
    )


def _wo_schema(path: str) -> SheetSchema:
    return SheetSchema(
        path=path,
        sheet="OdL",
        columns=[
            ColumnMapping(column="ID", field="id", required=True),
            ColumnMapping(column="Tipo", field="type"),
            ColumnMapping(column="Priorità", field="priority"),
            ColumnMapping(column="Stato", field="status"),
            ColumnMapping(column="Codice Asset", field="asset_id", required=True),
            ColumnMapping(column="Descrizione", field="description"),
            ColumnMapping(column="Assegnato a", field="assigned_to"),
            ColumnMapping(
                column="Durata stimata (ore)", field="estimated_duration_hours", coerce="float_it"
            ),
            ColumnMapping(column="Data creazione", field="created_at", coerce="datetime_parse"),
        ],
        write_mode="append",
    )


@pytest.fixture()
def wo_file(tmp_path: Path) -> Path:
    """Copy blank workorders fixture to a temp dir for write tests."""
    src = FIXTURES / "workorders_blank.xlsx"
    dst = tmp_path / "workorders.xlsx"
    shutil.copy2(src, dst)
    return dst


class TestReadAssets:
    @pytest.mark.asyncio
    async def test_read_fixture(self) -> None:
        config = ExcelConnectorConfig(asset_registry=_asset_schema())
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assets = await conn.read_assets()
        assert len(assets) >= 50
        first = assets[0]
        assert first.id == "P-001"
        assert first.name == "Pompa centrifuga linea 1"
        assert first.type == AssetType.ROTATING_EQUIPMENT
        assert first.criticality == Criticality.A
        assert first.install_date == date(2019, 3, 15)

    @pytest.mark.asyncio
    async def test_mixed_date_formats(self) -> None:
        config = ExcelConnectorConfig(asset_registry=_asset_schema())
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assets = await conn.read_assets()
        by_id = {a.id: a for a in assets}
        # Italian dd/mm/yyyy
        assert by_id["P-001"].install_date == date(2019, 3, 15)
        # ISO yyyy-mm-dd
        assert by_id["P-002"].install_date == date(2020, 6, 22)

    @pytest.mark.asyncio
    async def test_excel_serial_date(self) -> None:
        config = ExcelConnectorConfig(asset_registry=_asset_schema())
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assets = await conn.read_assets()
        by_id = {a.id: a for a in assets}
        assert by_id["PE-001"].install_date == date(2020, 1, 1)

    @pytest.mark.asyncio
    async def test_missing_optional_date(self) -> None:
        """Rows with empty date fields should parse with install_date=None."""
        config = ExcelConnectorConfig(asset_registry=_asset_schema())
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assets = await conn.read_assets()
        by_id = {a.id: a for a in assets}
        assert by_id["TA-002"].install_date is None

    @pytest.mark.asyncio
    async def test_whitespace_stripped(self) -> None:
        config = ExcelConnectorConfig(asset_registry=_asset_schema())
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assets = await conn.read_assets()
        by_id = {a.id: a for a in assets}
        # Asset has "  SP-001 " in the fixture — pydantic str_strip_whitespace handles it
        assert "SP-001" in by_id

    @pytest.mark.asyncio
    async def test_parent_field(self) -> None:
        config = ExcelConnectorConfig(asset_registry=_asset_schema())
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assets = await conn.read_assets()
        by_id = {a.id: a for a in assets}
        assert by_id["C-002"].parent == "C-001"

    @pytest.mark.asyncio
    async def test_no_asset_registry_returns_empty(self) -> None:
        config = ExcelConnectorConfig(
            work_orders=_wo_schema(str(FIXTURES / "workorders_blank.xlsx")),
        )
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assert await conn.read_assets() == []


class TestWriteCapabilityDeclaration:
    """Write capabilities are declared only when a writable work_orders sheet is
    configured — a declared-but-unserviceable write is worse than an undeclared
    one, and would advertise a capability the connector raises on. Mirrors how
    GenericSqlConnector gates its writes behind ``capabilities: read_write``.
    """

    def test_read_capabilities_always_present(self) -> None:
        config = ExcelConnectorConfig(asset_registry=_asset_schema())
        conn = ExcelCsvConnector(config=config)
        assert Capability.READ_ASSETS in conn.capabilities
        assert Capability.READ_WORK_ORDERS in conn.capabilities

    def test_writes_absent_when_no_work_orders_schema(self) -> None:
        config = ExcelConnectorConfig(asset_registry=_asset_schema())
        conn = ExcelCsvConnector(config=config)
        assert Capability.CREATE_WORK_ORDER not in conn.capabilities
        assert Capability.UPDATE_WORK_ORDER not in conn.capabilities

    def test_writes_absent_when_work_orders_read_only(self, tmp_path: Path) -> None:
        schema = SheetSchema(
            path=str(tmp_path / "odl.csv"),
            columns=[
                ColumnMapping(column="ID", field="id", required=True),
                ColumnMapping(column="Codice Asset", field="asset_id", required=True),
            ],
            write_mode=None,
        )
        config = ExcelConnectorConfig(work_orders=schema)
        conn = ExcelCsvConnector(config=config)
        assert Capability.CREATE_WORK_ORDER not in conn.capabilities
        assert Capability.UPDATE_WORK_ORDER not in conn.capabilities
        # Reads are still guaranteed regardless of write configuration.
        assert Capability.READ_WORK_ORDERS in conn.capabilities

    def test_writes_present_when_write_mode_configured(self, tmp_path: Path) -> None:
        config = ExcelConnectorConfig(work_orders=_wo_schema(str(tmp_path / "odl.csv")))
        conn = ExcelCsvConnector(config=config)
        assert Capability.CREATE_WORK_ORDER in conn.capabilities
        assert Capability.UPDATE_WORK_ORDER in conn.capabilities


class TestWriteWorkOrder:
    @pytest.mark.asyncio
    async def test_append_work_order(self, wo_file: Path) -> None:
        config = ExcelConnectorConfig(work_orders=_wo_schema(str(wo_file)))
        conn = ExcelCsvConnector(config=config)
        await conn.connect()

        wo = WorkOrder(
            id="WO-2026-001",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            status=WorkOrderStatus.CREATED,
            asset_id="P-001",
            description="Sostituzione guarnizione pompa",
            assigned_to="Marco Rossi",
            estimated_duration_hours=2.5,
        )
        result = await conn.create_work_order(wo)
        assert result.id == "WO-2026-001"

        # Verify it was written to the file
        import openpyxl

        wb = openpyxl.load_workbook(str(wo_file))
        ws = wb["OdL"]
        rows = list(ws.iter_rows(values_only=True))
        assert len(rows) == 2  # header + 1 data row
        data_row = rows[1]
        assert data_row[0] == "WO-2026-001"
        assert data_row[4] == "P-001"
        wb.close()

    @pytest.mark.asyncio
    async def test_append_multiple(self, wo_file: Path) -> None:
        config = ExcelConnectorConfig(work_orders=_wo_schema(str(wo_file)))
        conn = ExcelCsvConnector(config=config)
        await conn.connect()

        for i in range(3):
            wo = WorkOrder(
                id=f"WO-2026-{i:03d}",
                type=WorkOrderType.CORRECTIVE,
                priority=Priority.MEDIUM,
                asset_id="P-001",
            )
            await conn.create_work_order(wo)

        wos = await conn.read_work_orders()
        assert len(wos) == 3

    @pytest.mark.asyncio
    async def test_no_write_mode_raises(self, tmp_path: Path) -> None:
        import openpyxl

        f = tmp_path / "readonly.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "OdL"
        ws.append(["ID"])
        wb.save(str(f))
        wb.close()

        schema = SheetSchema(
            path=str(f),
            sheet="OdL",
            columns=[ColumnMapping(column="ID", field="id", required=True)],
            write_mode=None,
        )
        config = ExcelConnectorConfig(work_orders=schema)
        conn = ExcelCsvConnector(config=config)
        await conn.connect()

        wo = WorkOrder(id="WO-1", type=WorkOrderType.CORRECTIVE, asset_id="X")
        with pytest.raises(ConnectorConfigError, match="write_mode"):
            await conn.create_work_order(wo)


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_missing_file_raises(self) -> None:
        schema = SheetSchema(
            path="/nonexistent/assets.xlsx",
            sheet="Macchine",
            columns=[ColumnMapping(column="Codice", field="id", required=True)],
        )
        config = ExcelConnectorConfig(asset_registry=schema)
        conn = ExcelCsvConnector(config=config)
        with pytest.raises(ConnectorConfigError, match="not found"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_missing_required_column_raises(self, tmp_path: Path) -> None:
        """Schema expects a column that doesn't exist in the file."""
        import openpyxl

        f = tmp_path / "bad.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws.append(["WrongColumn"])
        ws.append(["value"])
        wb.save(str(f))
        wb.close()

        schema = SheetSchema(
            path=str(f),
            columns=[ColumnMapping(column="ExpectedColumn", field="id", required=True)],
        )
        config = ExcelConnectorConfig(asset_registry=schema)
        conn = ExcelCsvConnector(config=config)
        with pytest.raises(ConnectorSchemaError, match="Required columns missing"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_wrong_sheet_name_raises(self, tmp_path: Path) -> None:
        import openpyxl

        f = tmp_path / "sheets.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "RealSheet"
        ws.append(["Col"])
        wb.save(str(f))
        wb.close()

        schema = SheetSchema(
            path=str(f),
            sheet="WrongSheet",
            columns=[ColumnMapping(column="Col", field="id")],
        )
        config = ExcelConnectorConfig(asset_registry=schema)
        conn = ExcelCsvConnector(config=config)
        with pytest.raises(ConnectorSchemaError, match="WrongSheet"):
            await conn.connect()


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self) -> None:
        config = ExcelConnectorConfig(asset_registry=_asset_schema())
        conn = ExcelCsvConnector(config=config)
        health = await conn.health_check()
        assert health.status.value == "healthy"

    @pytest.mark.asyncio
    async def test_unhealthy_missing_file(self) -> None:
        schema = SheetSchema(
            path="/nonexistent/file.xlsx",
            columns=[ColumnMapping(column="X", field="id")],
        )
        config = ExcelConnectorConfig(asset_registry=schema)
        conn = ExcelCsvConnector(config=config)
        health = await conn.health_check()
        assert health.status.value == "unhealthy"


class TestCsvSupport:
    @pytest.mark.asyncio
    async def test_read_csv(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "assets.csv"
        csv_file.write_text(
            "Codice,Nome,Tipo\nP-001,Pompa 1,rotating_equipment\nP-002,Pompa 2,static_equipment\n",
            encoding="utf-8",
        )
        schema = SheetSchema(
            path=str(csv_file),
            columns=[
                ColumnMapping(column="Codice", field="id", required=True),
                ColumnMapping(column="Nome", field="name", required=True),
                ColumnMapping(column="Tipo", field="type"),
            ],
        )
        config = ExcelConnectorConfig(asset_registry=schema)
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assets = await conn.read_assets()
        assert len(assets) == 2
        assert assets[0].id == "P-001"

    @pytest.mark.asyncio
    async def test_write_csv(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "odl.csv"
        schema = SheetSchema(
            path=str(csv_file),
            sheet="ignored",
            columns=[
                ColumnMapping(column="ID", field="id", required=True),
                ColumnMapping(column="Codice Asset", field="asset_id", required=True),
                ColumnMapping(column="Descrizione", field="description"),
            ],
            write_mode="append",
        )
        config = ExcelConnectorConfig(work_orders=schema)
        conn = ExcelCsvConnector(config=config)
        await conn.connect()

        wo = WorkOrder(
            id="WO-001", type=WorkOrderType.CORRECTIVE, asset_id="P-001", description="Test"
        )
        await conn.create_work_order(wo)

        content = csv_file.read_text(encoding="utf-8")
        assert "WO-001" in content
        assert "P-001" in content

    @pytest.mark.asyncio
    async def test_update_csv_persists_to_disk(self, tmp_path: Path) -> None:
        """A CSV-backed update must be written to disk, not just the cache —
        otherwise the change is lost on restart (the cache-only bug)."""
        csv_file = tmp_path / "odl.csv"
        schema = SheetSchema(
            path=str(csv_file),
            sheet="ignored",
            columns=[
                ColumnMapping(column="ID", field="id", required=True),
                ColumnMapping(column="Codice Asset", field="asset_id", required=True),
                ColumnMapping(column="Descrizione", field="description"),
            ],
            write_mode="append",
        )
        config = ExcelConnectorConfig(work_orders=schema)
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        await conn.create_work_order(
            WorkOrder(
                id="WO-001", type=WorkOrderType.CORRECTIVE, asset_id="P-001", description="old"
            )
        )
        await conn.update_work_order("WO-001", {"description": "new description"})

        # The on-disk file reflects the update (no duplicate row).
        content = csv_file.read_text(encoding="utf-8-sig")
        assert "new description" in content
        assert "old" not in content
        assert content.count("WO-001") == 1

        # A fresh connector reloads the updated value.
        conn2 = ExcelCsvConnector(config=config)
        await conn2.connect()
        reloaded = await conn2.read_work_orders()
        assert len(reloaded) == 1
        assert reloaded[0].description == "new description"

    @pytest.mark.asyncio
    async def test_csv_rewrite_atomic_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the rewrite fails mid-way, the original file is left intact (the
        rewrite writes a temp sibling then atomically replaces the target)."""
        csv_file = tmp_path / "odl.csv"
        schema = SheetSchema(
            path=str(csv_file),
            sheet="ignored",
            columns=[
                ColumnMapping(column="ID", field="id", required=True),
                ColumnMapping(column="Codice Asset", field="asset_id", required=True),
                ColumnMapping(column="Descrizione", field="description"),
            ],
            write_mode="append",
        )
        config = ExcelConnectorConfig(work_orders=schema)
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        await conn.create_work_order(
            WorkOrder(
                id="WO-001", type=WorkOrderType.CORRECTIVE, asset_id="P-001", description="orig"
            )
        )
        before = csv_file.read_text(encoding="utf-8-sig")

        def _boom(wo: object, schema: object) -> dict[str, object]:
            raise RuntimeError("disk full mid-rewrite")

        monkeypatch.setattr(conn, "_work_order_to_row", _boom)
        with pytest.raises(RuntimeError):
            await conn.update_work_order("WO-001", {"description": "new"})

        # Original file unchanged — not truncated by the failed rewrite.
        assert csv_file.read_text(encoding="utf-8-sig") == before

    @pytest.mark.asyncio
    async def test_csv_formula_injection_neutralized_and_roundtrips(self, tmp_path: Path) -> None:
        """A field starting with a formula trigger is written with a leading
        apostrophe (so a spreadsheet treats it as text), and round-trips back
        to its original value through a fresh connector read."""
        csv_file = tmp_path / "odl.csv"
        schema = SheetSchema(
            path=str(csv_file),
            sheet="ignored",
            columns=[
                ColumnMapping(column="ID", field="id", required=True),
                ColumnMapping(column="Codice Asset", field="asset_id", required=True),
                ColumnMapping(column="Descrizione", field="description"),
            ],
            write_mode="append",
        )
        config = ExcelConnectorConfig(work_orders=schema)
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        await conn.create_work_order(
            WorkOrder(
                id="WO-001",
                type=WorkOrderType.CORRECTIVE,
                asset_id="P-001",
                description="=SUM(A1:A9)+cmd",
            )
        )
        # On disk the trigger is neutralized with a leading apostrophe.
        on_disk = csv_file.read_text(encoding="utf-8-sig")
        assert "'=SUM(A1:A9)+cmd" in on_disk
        assert ",=SUM" not in on_disk  # raw formula must not be present unguarded

        # A fresh connector reads back the original value (clean round-trip).
        conn2 = ExcelCsvConnector(config=config)
        await conn2.connect()
        reloaded = await conn2.read_work_orders()
        assert reloaded[0].description == "=SUM(A1:A9)+cmd"

    @pytest.mark.asyncio
    async def test_xlsx_formula_injection_neutralized_and_roundtrips(self, tmp_path: Path) -> None:
        """xlsx write neutralizes formula triggers (also stops openpyxl from
        storing the value as a real formula) and round-trips."""
        import openpyxl

        xlsx_file = tmp_path / "odl.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "OdL"
        ws.append(["ID", "Codice Asset", "Descrizione"])
        wb.save(str(xlsx_file))
        wb.close()
        schema = SheetSchema(
            path=str(xlsx_file),
            sheet="OdL",
            columns=[
                ColumnMapping(column="ID", field="id", required=True),
                ColumnMapping(column="Codice Asset", field="asset_id", required=True),
                ColumnMapping(column="Descrizione", field="description"),
            ],
            write_mode="append",
        )
        config = ExcelConnectorConfig(work_orders=schema)
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        await conn.create_work_order(
            WorkOrder(
                id="WO-001",
                type=WorkOrderType.CORRECTIVE,
                asset_id="P-001",
                description="=HYPERLINK(0)",
            )
        )
        wb2 = openpyxl.load_workbook(str(xlsx_file))
        cell = wb2["OdL"].cell(row=2, column=3)
        wb2.close()
        # Stored as a text string (leading apostrophe), not a formula.
        assert cell.data_type != "f"
        assert cell.value == "'=HYPERLINK(0)"

        conn2 = ExcelCsvConnector(config=config)
        await conn2.connect()
        reloaded = await conn2.read_work_orders()
        assert reloaded[0].description == "=HYPERLINK(0)"


def _flat_settings(tmp_path: Path) -> dict[str, object]:
    """The connector settings exactly as a ``machina.yaml`` entry carries them."""
    assets = tmp_path / "assets.csv"
    assets.write_text("Codice,Nome\nP-201,Pompa A\nC-3,Caldaia\n", encoding="utf-8")
    return {
        "asset_registry": {
            "path": str(assets),
            "columns": [
                {"column": "Codice", "field": "id", "required": True},
                {"column": "Nome", "field": "name", "required": True},
            ],
        },
        "work_orders": {
            "path": str(tmp_path / "odl.csv"),
            "write_mode": "append",
            "columns": [
                {"column": "ID", "field": "id", "required": True},
                {"column": "Codice Asset", "field": "asset_id", "required": True},
                {"column": "Descrizione", "field": "description"},
                {"column": "Stato", "field": "status"},
                {"column": "Assegnato a", "field": "assigned_to"},
            ],
        },
    }


async def _connected_with_work_orders(tmp_path: Path) -> ExcelCsvConnector:
    conn = ExcelCsvConnector(**_flat_settings(tmp_path))
    await conn.connect()
    for wo_id, asset_id in (("WO-1", "P-201"), ("WO-2", "C-3"), ("WO-3", "P-201")):
        await conn.create_work_order(
            WorkOrder(id=wo_id, type=WorkOrderType.CORRECTIVE, asset_id=asset_id, description="x")
        )
    return conn


class TestYamlSettingsAndCallContract:
    """Built from flat YAML settings; honours the agent/MCP call shapes."""

    @pytest.mark.asyncio
    async def test_flat_settings_build_the_connector(self, tmp_path: Path) -> None:
        conn = ExcelCsvConnector(**_flat_settings(tmp_path))
        await conn.connect()
        assert [a.id for a in await conn.read_assets()] == ["P-201", "C-3"]
        assert Capability.CREATE_WORK_ORDER in conn.capabilities

    def test_config_and_flat_settings_together_are_refused(self, tmp_path: Path) -> None:
        settings = _flat_settings(tmp_path)
        config = ExcelConnectorConfig.model_validate(settings)
        with pytest.raises(ConnectorConfigError, match="either"):
            ExcelCsvConnector(config=config, **settings)

    def test_invalid_flat_settings_raise_config_error(self) -> None:
        with pytest.raises(ConnectorConfigError, match="ExcelConnectorConfig"):
            ExcelCsvConnector(watcher={"enabled": False})  # no sheet configured

    @pytest.mark.asyncio
    async def test_get_asset(self, tmp_path: Path) -> None:
        conn = ExcelCsvConnector(**_flat_settings(tmp_path))
        await conn.connect()
        asset = await conn.get_asset("C-3")
        assert asset is not None
        assert asset.name == "Caldaia"
        assert await conn.get_asset("NOPE") is None

    @pytest.mark.asyncio
    async def test_read_work_orders_filters(self, tmp_path: Path) -> None:
        conn = await _connected_with_work_orders(tmp_path)
        await conn.update_work_order("WO-3", status=WorkOrderStatus.CANCELLED)

        assert [wo.id for wo in await conn.read_work_orders(asset_id="P-201")] == ["WO-1", "WO-3"]
        assert [wo.id for wo in await conn.read_work_orders(status="cancelled")] == ["WO-3"]
        open_on_pump = await conn.read_work_orders(
            asset_id="P-201", status=WorkOrderStatus.CREATED
        )
        assert [wo.id for wo in open_on_pump] == ["WO-1"]
        assert len(await conn.read_work_orders()) == 3

    @pytest.mark.asyncio
    async def test_update_work_order_keyword_and_dict_forms(self, tmp_path: Path) -> None:
        conn = await _connected_with_work_orders(tmp_path)

        updated = await conn.update_work_order(
            "WO-1", status=WorkOrderStatus.ASSIGNED, assigned_to="Mario Rossi"
        )
        assert updated.status == WorkOrderStatus.ASSIGNED
        assert updated.assigned_to == "Mario Rossi"

        # Dict form: the status string is coerced to the enum, not stored raw.
        updated = await conn.update_work_order(
            "WO-1", {"description": "new", "status": "in_progress"}
        )
        assert updated.description == "new"
        assert updated.status is WorkOrderStatus.IN_PROGRESS
        assert updated.assigned_to == "Mario Rossi"

    @pytest.mark.asyncio
    async def test_illegal_transition_is_refused_and_leaves_the_record(
        self, tmp_path: Path
    ) -> None:
        conn = await _connected_with_work_orders(tmp_path)
        with pytest.raises(ConnectorError, match="Cannot transition"):
            await conn.update_work_order("WO-1", status=WorkOrderStatus.CLOSED)
        (wo,) = [w for w in await conn.read_work_orders() if w.id == "WO-1"]
        assert wo.status is WorkOrderStatus.CREATED

    @pytest.mark.asyncio
    async def test_unknown_status_string_is_refused(self, tmp_path: Path) -> None:
        conn = await _connected_with_work_orders(tmp_path)
        with pytest.raises(ConnectorError, match="Invalid work order status"):
            await conn.update_work_order("WO-1", {"status": "done-ish"})

    @pytest.mark.asyncio
    async def test_failed_rewrite_rolls_the_cache_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = await _connected_with_work_orders(tmp_path)

        def _locked(schema: object, wo: object, fields: object) -> None:
            raise PermissionError("workbook open in another program")

        monkeypatch.setattr(conn, "_update_row_in_file", _locked)
        with pytest.raises(ConnectorLockedError):
            await conn.update_work_order("WO-1", description="changed")
        (wo,) = [w for w in await conn.read_work_orders() if w.id == "WO-1"]
        assert wo.description == "x"

    @pytest.mark.asyncio
    async def test_create_is_idempotent_on_the_id(self, tmp_path: Path) -> None:
        conn = await _connected_with_work_orders(tmp_path)
        again = await conn.create_work_order(
            WorkOrder(id="WO-1", type=WorkOrderType.CORRECTIVE, asset_id="P-201", description="y")
        )
        assert again.description == "x"  # the stored record, not the retry
        assert len(await conn.read_work_orders()) == 3
        content = (tmp_path / "odl.csv").read_text(encoding="utf-8-sig")
        assert content.count("WO-1") == 1

    @pytest.mark.asyncio
    async def test_get_asset_before_connect_is_an_error(self, tmp_path: Path) -> None:
        conn = ExcelCsvConnector(**_flat_settings(tmp_path))
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.get_asset("P-201")

    def test_config_given_as_a_dict_is_validated(self, tmp_path: Path) -> None:
        conn = ExcelCsvConnector(config=_flat_settings(tmp_path))
        assert Capability.CREATE_WORK_ORDER in conn.capabilities

    def test_unknown_setting_is_refused(self, tmp_path: Path) -> None:
        settings = _flat_settings(tmp_path)
        settings["file_path"] = "data/asset_registry.xlsx"  # an old, never-valid key
        with pytest.raises(ConnectorConfigError, match="file_path"):
            ExcelCsvConnector(**settings)

    @pytest.mark.asyncio
    async def test_same_status_update_is_a_no_op(self, tmp_path: Path) -> None:
        """A retried status update succeeds instead of failing the transition."""
        conn = await _connected_with_work_orders(tmp_path)
        await conn.update_work_order("WO-1", status=WorkOrderStatus.ASSIGNED)
        again = await conn.update_work_order("WO-1", status=WorkOrderStatus.ASSIGNED)
        assert again.status is WorkOrderStatus.ASSIGNED

    @pytest.mark.asyncio
    async def test_update_of_an_unmapped_field_is_refused(self, tmp_path: Path) -> None:
        settings = _flat_settings(tmp_path)
        columns = settings["work_orders"]["columns"]  # type: ignore[index]
        settings["work_orders"]["columns"] = [  # type: ignore[index]
            c for c in columns if c["field"] != "assigned_to"
        ]
        conn = ExcelCsvConnector(**settings)
        await conn.connect()
        await conn.create_work_order(
            WorkOrder(id="WO-1", type=WorkOrderType.CORRECTIVE, asset_id="P-201", description="x")
        )

        with pytest.raises(ConnectorError, match="Cannot persist assigned_to"):
            await conn.update_work_order("WO-1", assigned_to="Mario Rossi")
        (wo,) = await conn.read_work_orders()
        assert wo.assigned_to is None

    @pytest.mark.asyncio
    async def test_reads_before_connect_are_errors(self, tmp_path: Path) -> None:
        conn = ExcelCsvConnector(**_flat_settings(tmp_path))
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.read_assets()
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.read_work_orders()

    @pytest.mark.asyncio
    async def test_create_maps_a_locked_file_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = await _connected_with_work_orders(tmp_path)
        wo = WorkOrder(id="WO-9", type=WorkOrderType.CORRECTIVE, asset_id="C-3", description="z")

        def _locked(path: object, schema: object, row: object) -> None:
            raise PermissionError("workbook open in another program")

        monkeypatch.setattr(conn, "_write_row", _locked)
        with pytest.raises(ConnectorLockedError):
            await conn.create_work_order(wo)
        assert "WO-9" not in [w.id for w in await conn.read_work_orders()]

        monkeypatch.undo()
        await conn.create_work_order(wo)
        content = (tmp_path / "odl.csv").read_text(encoding="utf-8-sig")
        assert content.count("WO-9") == 1

    @pytest.mark.asyncio
    async def test_create_maps_other_os_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = await _connected_with_work_orders(tmp_path)

        def _disk_full(path: object, schema: object, row: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(conn, "_write_row", _disk_full)
        with pytest.raises(ConnectorError, match="Could not write"):
            await conn.create_work_order(
                WorkOrder(id="WO-9", type=WorkOrderType.CORRECTIVE, asset_id="C-3")
            )

    @pytest.mark.asyncio
    async def test_a_file_that_cannot_be_reread_is_mapped_on_both_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Writes re-read the sheet first; a locked file fails there, not later."""
        conn = await _connected_with_work_orders(tmp_path)

        def _locked() -> None:
            raise PermissionError("workbook open in another program")

        monkeypatch.setattr(conn, "_validate_and_load_work_orders", _locked)
        with pytest.raises(ConnectorLockedError):
            await conn.create_work_order(
                WorkOrder(id="WO-9", type=WorkOrderType.CORRECTIVE, asset_id="C-3")
            )
        with pytest.raises(ConnectorLockedError):
            await conn.update_work_order("WO-1", description="changed")

    @pytest.mark.asyncio
    async def test_list_fields_round_trip_as_delimited_cells(self, tmp_path: Path) -> None:
        settings = _flat_settings(tmp_path)
        settings["work_orders"]["columns"].append(  # type: ignore[index]
            {"column": "Competenze", "field": "requested_skills"}
        )
        conn = ExcelCsvConnector(**settings)
        await conn.connect()
        await conn.create_work_order(
            WorkOrder(
                id="WO-9",
                type=WorkOrderType.CORRECTIVE,
                asset_id="P-201",
                requested_skills=["mechanical", "hydraulics"],
            )
        )
        assert "mechanical;hydraulics" in (tmp_path / "odl.csv").read_text(encoding="utf-8-sig")

        reread = ExcelCsvConnector(**settings)
        await reread.connect()
        (wo,) = await reread.read_work_orders()
        assert wo.requested_skills == ["mechanical", "hydraulics"]

    @pytest.mark.asyncio
    async def test_unknown_field_and_id_change_are_refused(self, tmp_path: Path) -> None:
        conn = await _connected_with_work_orders(tmp_path)
        with pytest.raises(ConnectorError, match="Unknown work order field"):
            await conn.update_work_order("WO-1", {"descrption": "typo"})
        with pytest.raises(ConnectorError, match="id cannot be changed"):
            await conn.update_work_order("WO-1", {"id": "WO-X"})
        (wo,) = [w for w in await conn.read_work_orders() if w.id == "WO-1"]
        assert wo.description == "x"

    @pytest.mark.asyncio
    async def test_update_of_an_unknown_work_order_is_refused(self, tmp_path: Path) -> None:
        conn = await _connected_with_work_orders(tmp_path)
        with pytest.raises(ConnectorError, match="'WO-404' not found"):
            await conn.update_work_order("WO-404", description="x")

    @pytest.mark.asyncio
    async def test_update_without_write_mode_changes_the_cache_only(self, tmp_path: Path) -> None:
        orders = tmp_path / "odl.csv"
        orders.write_text(
            "ID,Codice Asset,Descrizione,Stato,Assegnato a\r\nWO-1,P-201,x,created,\r\n",
            encoding="utf-8",
        )
        settings = _flat_settings(tmp_path)
        settings["work_orders"]["write_mode"] = None  # type: ignore[index]
        conn = ExcelCsvConnector(**settings)
        await conn.connect()
        before = orders.read_bytes()

        updated = await conn.update_work_order("WO-1", assigned_to="Rossi")

        assert updated.assigned_to == "Rossi"
        assert [w.assigned_to for w in await conn.read_work_orders()] == ["Rossi"]
        assert orders.read_bytes() == before

    @pytest.mark.asyncio
    async def test_a_row_typed_with_an_invalid_status_is_skipped(self, tmp_path: Path) -> None:
        """One bad hand-typed row neither breaks reads nor blocks later writes."""
        conn = await _connected_with_work_orders(tmp_path)
        with (tmp_path / "odl.csv").open("a", encoding="utf-8", newline="") as f:
            f.write("WO-BAD,P-201,typed by hand,aperto,\r\n")

        await conn.create_work_order(
            WorkOrder(id="WO-9", type=WorkOrderType.CORRECTIVE, asset_id="C-3")
        )
        await conn.update_work_order("WO-1", description="still writable")

        ids = [wo.id for wo in await conn.read_work_orders()]
        assert ids == ["WO-1", "WO-2", "WO-3", "WO-9"]
        assert "WO-BAD" in (tmp_path / "odl.csv").read_text(encoding="utf-8-sig")

    @pytest.mark.asyncio
    async def test_an_unreadable_file_on_reread_is_a_connector_error(self, tmp_path: Path) -> None:
        conn = await _connected_with_work_orders(tmp_path)
        (tmp_path / "odl.csv").write_bytes(b"ID,Codice Asset\r\n\xff\xfe broken\r\n")
        with pytest.raises(ConnectorError, match="before writing"):
            await conn.create_work_order(
                WorkOrder(id="WO-9", type=WorkOrderType.CORRECTIVE, asset_id="C-3")
            )

    @pytest.mark.asyncio
    async def test_csv_update_keeps_a_utf8_bom(self, tmp_path: Path) -> None:
        """Excel on Windows needs the BOM to read accented text as UTF-8."""
        orders = tmp_path / "odl.csv"
        orders.write_bytes(
            codecs.BOM_UTF8
            + "ID,Codice Asset,Descrizione,Stato,Assegnato a\r\n"
            "WO-1,P-201,Criticità alta,created,\r\n".encode()
        )
        conn = ExcelCsvConnector(**_flat_settings(tmp_path))
        await conn.connect()

        await conn.update_work_order("WO-1", assigned_to="Rossi")

        content = orders.read_bytes()
        assert content.startswith(codecs.BOM_UTF8)
        assert "Criticità alta" in content.decode("utf-8-sig")


class TestWritesPreserveTheFile:
    """Writes touch only their own row: other sheets, rows and columns survive."""

    @staticmethod
    def _workbook(path: Path) -> None:
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "OdL"
        ws.append(["ID", "Note", "Codice Asset", "Stato", "Descrizione"])
        ws.append(["WO-1", "keep me", "P-201", "created", "first"])
        ws.append(["WO-2", "and me", "C-3", "created", "second"])
        other = wb.create_sheet("Asset")
        other.append(["Codice", "Nome"])
        other.append(["P-201", "Pompa A"])
        wb.save(str(path))
        wb.close()

    @staticmethod
    def _config(path: Path) -> ExcelConnectorConfig:
        return ExcelConnectorConfig(
            work_orders=SheetSchema(
                path=str(path),
                sheet="OdL",
                write_mode="append",
                columns=[
                    ColumnMapping(column="ID", field="id", required=True),
                    ColumnMapping(column="Codice Asset", field="asset_id", required=True),
                    ColumnMapping(column="Stato", field="status"),
                    ColumnMapping(column="Descrizione", field="description"),
                ],
            )
        )

    @pytest.mark.asyncio
    async def test_xlsx_update_keeps_other_sheets_rows_and_columns(self, tmp_path: Path) -> None:
        import openpyxl

        path = tmp_path / "odl.xlsx"
        self._workbook(path)
        conn = ExcelCsvConnector(config=self._config(path))
        await conn.connect()

        await conn.update_work_order("WO-1", description="changed")

        wb = openpyxl.load_workbook(str(path))
        try:
            assert wb.sheetnames == ["OdL", "Asset"]
            rows = list(wb["OdL"].iter_rows(values_only=True))
            assert rows[1] == ("WO-1", "keep me", "P-201", "created", "changed")
            assert rows[2] == ("WO-2", "and me", "C-3", "created", "second")
            assert list(wb["Asset"].iter_rows(values_only=True))[1] == ("P-201", "Pompa A")
        finally:
            wb.close()

    @pytest.mark.asyncio
    async def test_xlsx_append_follows_the_file_column_order(self, tmp_path: Path) -> None:
        import openpyxl

        path = tmp_path / "odl.xlsx"
        self._workbook(path)
        conn = ExcelCsvConnector(config=self._config(path))
        await conn.connect()

        await conn.create_work_order(
            WorkOrder(
                id="WO-3", type=WorkOrderType.CORRECTIVE, asset_id="P-201", description="new"
            )
        )

        wb = openpyxl.load_workbook(str(path))
        try:
            rows = list(wb["OdL"].iter_rows(values_only=True))
            assert rows[3] == ("WO-3", None, "P-201", "created", "new")
        finally:
            wb.close()
        fresh = ExcelCsvConnector(config=self._config(path))
        await fresh.connect()
        assert [wo.id for wo in await fresh.read_work_orders()] == ["WO-1", "WO-2", "WO-3"]

    @pytest.mark.asyncio
    async def test_csv_append_follows_the_file_column_order(self, tmp_path: Path) -> None:
        path = tmp_path / "odl.csv"
        path.write_text(
            "Descrizione,Note,ID,Codice Asset\nfirst,keep me,WO-1,P-201\n", encoding="utf-8"
        )
        schema = SheetSchema(
            path=str(path),
            sheet="ignored",
            write_mode="append",
            columns=[
                ColumnMapping(column="ID", field="id", required=True),
                ColumnMapping(column="Codice Asset", field="asset_id", required=True),
                ColumnMapping(column="Descrizione", field="description"),
            ],
        )
        conn = ExcelCsvConnector(config=ExcelConnectorConfig(work_orders=schema))
        await conn.connect()

        await conn.create_work_order(
            WorkOrder(id="WO-2", type=WorkOrderType.CORRECTIVE, asset_id="C-3", description="new")
        )

        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines == [
            "Descrizione,Note,ID,Codice Asset",
            "first,keep me,WO-1,P-201",
            "new,,WO-2,C-3",
        ]

    @pytest.mark.asyncio
    async def test_update_sees_rows_added_after_connect(self, tmp_path: Path) -> None:
        conn = await _connected_with_work_orders(tmp_path)
        path = tmp_path / "odl.csv"
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write("WO-EXT,C-3,external,created,\r\n")

        updated = await conn.update_work_order("WO-EXT", description="edited")

        assert updated.description == "edited"
        content = path.read_text(encoding="utf-8-sig")
        assert "WO-EXT,C-3,edited,created" in content
        assert content.count("WO-1") == 1

    @pytest.mark.asyncio
    async def test_update_keeps_failure_mode_and_creation_date(self, tmp_path: Path) -> None:
        path = tmp_path / "odl.csv"
        path.write_text(
            "ID,Codice Asset,Stato,Modo di guasto,Data creazione\n"
            "WO-1,P-201,created,BEAR-WEAR-01,2026-01-15T10:00:00\n",
            encoding="utf-8",
        )
        schema = SheetSchema(
            path=str(path),
            sheet="ignored",
            write_mode="append",
            columns=[
                ColumnMapping(column="ID", field="id", required=True),
                ColumnMapping(column="Codice Asset", field="asset_id", required=True),
                ColumnMapping(column="Stato", field="status"),
                ColumnMapping(column="Modo di guasto", field="failure_mode"),
                ColumnMapping(
                    column="Data creazione", field="created_at", coerce="datetime_parse"
                ),
            ],
        )
        conn = ExcelCsvConnector(config=ExcelConnectorConfig(work_orders=schema))
        await conn.connect()
        (wo,) = await conn.read_work_orders()
        assert wo.failure_mode == "BEAR-WEAR-01"
        assert wo.created_at.replace(tzinfo=None) == datetime(2026, 1, 15, 10, 0)

        await conn.update_work_order("WO-1", status=WorkOrderStatus.ASSIGNED)

        row = path.read_text(encoding="utf-8").splitlines()[1]
        assert row == "WO-1,P-201,assigned,BEAR-WEAR-01,2026-01-15T10:00:00"


class _OverlapDetectingWorkbook:
    """Wraps the connector's ``.xlsx`` file operations and records two at once.

    A sheet read, a row append and a row rewrite each use the file from start
    to end. The first call of the function named by ``hold`` waits for
    ``release`` instead, with ``held`` set while it waits, so a test can act
    while that call is mid-flight on its thread; set ``fail`` before
    releasing it to make that call raise instead of running. Holding
    ``_save_xlsx_atomically`` stops an append or a rewrite after it has loaded
    and changed the workbook, just before the save; holding
    ``_read_xlsx_rows`` stops the next sheet read, by ``connect()``,
    ``refresh()`` or a write, before it opens the file.
    """

    _USES_THE_FILE = ("_read_xlsx_rows", "_append_xlsx_row", "_update_xlsx_row")

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.hold = ""
        self.held = threading.Event()
        self.release = threading.Event()
        self.fail: Exception | None = None
        self.max_users = 0
        self._users = 0
        self._state = threading.Condition()
        for name in (*self._USES_THE_FILE, "_save_xlsx_atomically"):
            monkeypatch.setattr(excel, name, self._wrap(name, getattr(excel, name)))

    def wait_idle(self, timeout: float) -> bool:
        """Wait until no file operation is running, on any thread."""
        with self._state:
            return self._state.wait_for(lambda: self._users == 0, timeout)

    def _wrap(self, name: str, func: Callable[..., Any]) -> Callable[..., Any]:
        uses_the_file = name in self._USES_THE_FILE

        def wrapper(*args: Any) -> Any:
            with self._state:
                if uses_the_file:
                    self._users += 1
                    self.max_users = max(self.max_users, self._users)
                hold = name == self.hold and not self.held.is_set()
                if hold:
                    self.held.set()
            try:
                if hold:
                    self.release.wait(timeout=5)
                    if self.fail is not None:
                        raise self.fail
                return func(*args)
            finally:
                if uses_the_file:
                    with self._state:
                        self._users -= 1
                        self._state.notify_all()

        return wrapper


@pytest.fixture()
def workbook(monkeypatch: pytest.MonkeyPatch) -> Iterator[_OverlapDetectingWorkbook]:
    """The double, released at teardown: a call that a failing test left held
    would otherwise resume later, inside the next test's wrappers."""
    double = _OverlapDetectingWorkbook(monkeypatch)
    yield double
    double.release.set()
    assert double.wait_idle(5)


async def _odl_connector(tmp_path: Path) -> tuple[Path, ExcelCsvConnector]:
    """A workbook holding WO-1 and WO-2, and a connector connected to it."""
    path = tmp_path / "odl.xlsx"
    TestWritesPreserveTheFile._workbook(path)
    conn = ExcelCsvConnector(config=TestWritesPreserveTheFile._config(path))
    await conn.connect()
    return path, conn


def _sheet_rows(path: Path) -> list[tuple[Any, ...]]:
    """The work-order rows of the ``OdL`` sheet, header excluded."""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True)
    try:
        return list(wb["OdL"].iter_rows(min_row=2, values_only=True))
    finally:
        wb.close()


class TestWritesTakeTurns:
    """Work-order writes use the file one at a time, whatever happens to their
    callers: cancelling a caller cannot stop its write's worker thread."""

    @pytest.mark.asyncio
    async def test_a_cancelled_create_holds_the_file_until_its_append_ends(
        self, tmp_path: Path, workbook: _OverlapDetectingWorkbook
    ) -> None:
        """The same-ID retry waits for the abandoned append, then finds the row
        instead of appending it a second time."""
        path, conn = await _odl_connector(tmp_path)
        workbook.hold = "_append_xlsx_row"
        wo = WorkOrder(id="WO-3", type=WorkOrderType.CORRECTIVE, asset_id="C-3", description="new")

        first = asyncio.create_task(conn.create_work_order(wo))
        assert await asyncio.to_thread(workbook.held.wait, 5)  # the append is running
        first.cancel()  # an MCP request cancellation, a workflow step timeout
        with pytest.raises(asyncio.CancelledError):
            await first
        retry = asyncio.create_task(conn.create_work_order(wo))
        await asyncio.sleep(0.05)
        workbook.release.set()
        assert (await retry).description == "new"
        assert await asyncio.to_thread(workbook.wait_idle, 5)

        assert [row[0] for row in _sheet_rows(path)] == ["WO-1", "WO-2", "WO-3"]
        assert workbook.max_users == 1
        assert [w.id for w in await conn.read_work_orders()] == ["WO-1", "WO-2", "WO-3"]

    @pytest.mark.asyncio
    async def test_a_cancelled_update_holds_the_file_until_its_save_ends(
        self, tmp_path: Path, workbook: _OverlapDetectingWorkbook
    ) -> None:
        """The abandoned rewrite has already loaded the workbook, so a row
        appended before it saves would be lost in that save."""
        path, conn = await _odl_connector(tmp_path)
        workbook.hold = "_save_xlsx_atomically"
        new = WorkOrder(
            id="WO-3", type=WorkOrderType.CORRECTIVE, asset_id="C-3", description="new"
        )

        update = asyncio.create_task(conn.update_work_order("WO-1", description="changed"))
        assert await asyncio.to_thread(workbook.held.wait, 5)  # changed, not yet saved
        update.cancel()
        with pytest.raises(asyncio.CancelledError):
            await update
        create = asyncio.create_task(conn.create_work_order(new))
        await asyncio.sleep(0.05)
        workbook.release.set()
        await create
        assert await asyncio.to_thread(workbook.wait_idle, 5)

        expected = [("WO-1", "changed"), ("WO-2", "second"), ("WO-3", "new")]
        assert [(row[0], row[4]) for row in _sheet_rows(path)] == expected
        assert workbook.max_users == 1
        assert [(w.id, w.description) for w in await conn.read_work_orders()] == expected

    @pytest.mark.asyncio
    async def test_concurrent_writes_take_turns(
        self, tmp_path: Path, workbook: _OverlapDetectingWorkbook
    ) -> None:
        path, conn = await _odl_connector(tmp_path)

        await asyncio.gather(
            *(
                conn.create_work_order(
                    WorkOrder(id=f"WO-{n}", type=WorkOrderType.CORRECTIVE, asset_id="C-3")
                )
                for n in (3, 4, 5)
            ),
            conn.update_work_order("WO-1", description="changed"),
        )

        rows = _sheet_rows(path)
        assert [row[0] for row in rows] == ["WO-1", "WO-2", "WO-3", "WO-4", "WO-5"]
        assert rows[0][4] == "changed"
        assert workbook.max_users == 1

    @pytest.mark.asyncio
    async def test_a_write_cancelled_before_its_turn_never_runs(
        self,
        tmp_path: Path,
        workbook: _OverlapDetectingWorkbook,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Nor is it reported as failed, by the connector or by the thread pool."""
        path, conn = await _odl_connector(tmp_path)
        workbook.hold = "_append_xlsx_row"
        log = MagicMock()
        monkeypatch.setattr(excel, "logger", log)
        wo_3, wo_4 = (
            WorkOrder(id=wo_id, type=WorkOrderType.CORRECTIVE, asset_id="C-3")
            for wo_id in ("WO-3", "WO-4")
        )

        first = asyncio.create_task(conn.create_work_order(wo_3))
        assert await asyncio.to_thread(workbook.held.wait, 5)
        waiting = asyncio.create_task(conn.create_work_order(wo_4))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        workbook.release.set()
        await first
        assert await asyncio.to_thread(workbook.wait_idle, 5)

        assert [row[0] for row in _sheet_rows(path)] == ["WO-1", "WO-2", "WO-3"]
        assert [w.id for w in await conn.read_work_orders()] == ["WO-1", "WO-2", "WO-3"]
        log.error.assert_not_called()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    @pytest.mark.asyncio
    async def test_a_write_that_ends_after_its_caller_was_cancelled_is_read_back(
        self,
        tmp_path: Path,
        workbook: _OverlapDetectingWorkbook,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The cached work orders and the log are updated by the write itself,
        not by its caller, so reads match the file without waiting for another
        write, and the write is logged."""
        path, conn = await _odl_connector(tmp_path)
        workbook.hold = "_append_xlsx_row"
        log = MagicMock()
        monkeypatch.setattr(excel, "logger", log)
        wo = WorkOrder(id="WO-3", type=WorkOrderType.CORRECTIVE, asset_id="C-3")

        first = asyncio.create_task(conn.create_work_order(wo))
        assert await asyncio.to_thread(workbook.held.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        workbook.release.set()
        await conn._run_on_file_thread(lambda: None)  # the append has finished

        assert [row[0] for row in _sheet_rows(path)] == ["WO-1", "WO-2", "WO-3"]
        assert [w.id for w in await conn.read_work_orders()] == ["WO-1", "WO-2", "WO-3"]
        assert [c.args[0] for c in log.info.call_args_list] == ["work_order_created"]
        log.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_cancelled_update_whose_save_fails_leaves_the_cache_as_it_was(
        self, tmp_path: Path, workbook: _OverlapDetectingWorkbook
    ) -> None:
        path, conn = await _odl_connector(tmp_path)
        workbook.hold = "_save_xlsx_atomically"

        update = asyncio.create_task(conn.update_work_order("WO-1", description="changed"))
        assert await asyncio.to_thread(workbook.held.wait, 5)
        update.cancel()
        with pytest.raises(asyncio.CancelledError):
            await update
        workbook.fail = PermissionError("workbook open in another program")
        workbook.release.set()
        await conn._run_on_file_thread(lambda: None)  # the rewrite has failed

        expected = [("WO-1", "first"), ("WO-2", "second")]
        assert [(row[0], row[4]) for row in _sheet_rows(path)] == expected
        assert [(w.id, w.description) for w in await conn.read_work_orders()] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("call", "hold", "logged"),
        [
            pytest.param(
                lambda conn: conn.update_work_order("WO-1", description="changed"),
                "_save_xlsx_atomically",
                {
                    "operation": "update_work_order",
                    "work_order_id": "WO-1",
                    "error_type": "ConnectorLockedError",
                },
                id="update",
            ),
            pytest.param(
                lambda conn: conn.create_work_order(
                    WorkOrder(id="WO-3", type=WorkOrderType.CORRECTIVE, asset_id="C-3")
                ),
                "_save_xlsx_atomically",
                {
                    "operation": "create_work_order",
                    "work_order_id": "WO-3",
                    "asset_id": "C-3",
                    "error_type": "ConnectorLockedError",
                },
                id="create",
            ),
            pytest.param(
                lambda conn: conn.connect(),
                "_read_xlsx_rows",
                {"operation": "connect", "error_type": "PermissionError"},
                id="connect",
            ),
        ],
    )
    async def test_a_call_that_fails_after_its_caller_was_cancelled_is_logged(
        self,
        tmp_path: Path,
        workbook: _OverlapDetectingWorkbook,
        monkeypatch: pytest.MonkeyPatch,
        call: Callable[[ExcelCsvConnector], Any],
        hold: str,
        logged: dict[str, str],
    ) -> None:
        """Nobody reads that call's outcome any more, so its failure is logged
        where it happens, with what identifies the call."""
        _path, conn = await _odl_connector(tmp_path)
        workbook.hold = hold
        log = MagicMock()
        monkeypatch.setattr(excel, "logger", log)

        task = asyncio.create_task(call(conn))
        assert await asyncio.to_thread(workbook.held.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        workbook.fail = PermissionError("workbook open in another program")
        workbook.release.set()
        await conn._run_on_file_thread(lambda: None)  # the call has failed

        log.error.assert_called_once_with(
            "failed_after_cancel", connector="ExcelCsvConnector", **logged, error=ANY
        )

    @pytest.mark.asyncio
    async def test_a_failure_its_caller_receives_is_not_logged_again(
        self,
        tmp_path: Path,
        workbook: _OverlapDetectingWorkbook,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _path, conn = await _odl_connector(tmp_path)
        workbook.hold = "_save_xlsx_atomically"
        workbook.fail = PermissionError("workbook open in another program")
        workbook.release.set()
        log = MagicMock()
        monkeypatch.setattr(excel, "logger", log)

        with pytest.raises(ConnectorLockedError):
            await conn.update_work_order("WO-1", description="changed")

        log.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_disconnect_waits_for_a_write_in_flight(
        self, tmp_path: Path, workbook: _OverlapDetectingWorkbook
    ) -> None:
        """Once disconnect() returns, the connector is not writing to the file,
        and the write it waited for still returns its result."""
        path, conn = await _odl_connector(tmp_path)
        workbook.hold = "_save_xlsx_atomically"

        update = asyncio.create_task(conn.update_work_order("WO-1", description="changed"))
        assert await asyncio.to_thread(workbook.held.wait, 5)
        disconnecting = asyncio.create_task(conn.disconnect())
        await asyncio.sleep(0.05)
        assert not disconnecting.done()
        workbook.release.set()
        await disconnecting

        assert workbook.wait_idle(0)
        assert (await update).description == "changed"
        assert _sheet_rows(path)[0][4] == "changed"

    @pytest.mark.asyncio
    async def test_disconnect_stops_waiting_for_a_stuck_write(
        self,
        tmp_path: Path,
        workbook: _OverlapDetectingWorkbook,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _path, conn = await _odl_connector(tmp_path)
        workbook.hold = "_save_xlsx_atomically"
        monkeypatch.setattr(excel, "_DISCONNECT_WAIT_SEC", 0.05)
        log = MagicMock()
        monkeypatch.setattr(excel, "logger", log)

        update = asyncio.create_task(conn.update_work_order("WO-1", description="changed"))
        assert await asyncio.to_thread(workbook.held.wait, 5)
        await conn.disconnect()  # returns while the write is still held

        assert [c.args[0] for c in log.warning.call_args_list] == ["file_access_still_running"]
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.read_work_orders()
        workbook.release.set()
        assert (await update).description == "changed"


# The two ways the connector reloads its sheets. refresh() is synchronous: a
# FileWatcher calls it from its timer thread, as asyncio.to_thread does here.
_RELOADS = [
    pytest.param(lambda conn: asyncio.to_thread(conn.refresh), id="refresh"),
    pytest.param(lambda conn: conn.connect(), id="connect"),
]


class TestReadsTakeTurnsWithWrites:
    """connect() and refresh() read the files in turn with the writes, so a
    read never has a workbook open while a write replaces it (on Windows that
    replace fails, as if another program held the file)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reload", _RELOADS)
    async def test_a_reload_waits_for_a_write_in_flight(
        self,
        tmp_path: Path,
        workbook: _OverlapDetectingWorkbook,
        monkeypatch: pytest.MonkeyPatch,
        reload: Callable[[ExcelCsvConnector], Any],
    ) -> None:
        """The reload reads the file once the write has saved it, so the cache
        keeps the appended row."""
        _path, conn = await _odl_connector(tmp_path)
        workbook.hold = "_save_xlsx_atomically"
        wo = WorkOrder(id="WO-3", type=WorkOrderType.CORRECTIVE, asset_id="C-3")
        queued = threading.Event()
        submit = conn._submit_to_file_thread

        def submit_and_signal(*args: Any) -> Any:
            future = submit(*args)
            queued.set()
            return future

        create = asyncio.create_task(conn.create_work_order(wo))
        assert await asyncio.to_thread(workbook.held.wait, 5)  # appended, not yet saved
        monkeypatch.setattr(conn, "_submit_to_file_thread", submit_and_signal)
        reloading = asyncio.create_task(reload(conn))
        assert await asyncio.to_thread(queued.wait, 5)  # queued behind the write
        assert not reloading.done()
        workbook.release.set()
        await create
        await reloading

        assert workbook.max_users == 1
        assert [w.id for w in await conn.read_work_orders()] == ["WO-1", "WO-2", "WO-3"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reload", _RELOADS)
    async def test_a_write_waits_for_a_reload_in_flight(
        self,
        tmp_path: Path,
        workbook: _OverlapDetectingWorkbook,
        reload: Callable[[ExcelCsvConnector], Any],
    ) -> None:
        path, conn = await _odl_connector(tmp_path)
        workbook.hold = "_read_xlsx_rows"
        wo = WorkOrder(id="WO-3", type=WorkOrderType.CORRECTIVE, asset_id="C-3")

        reloading = asyncio.create_task(reload(conn))
        assert await asyncio.to_thread(workbook.held.wait, 5)  # the reload is reading
        create = asyncio.create_task(conn.create_work_order(wo))
        await asyncio.sleep(0.05)
        assert not create.done()
        assert not reloading.done()  # it returns once the sheets are loaded, not before
        workbook.release.set()
        await reloading
        await create

        assert workbook.max_users == 1
        assert [row[0] for row in _sheet_rows(path)] == ["WO-1", "WO-2", "WO-3"]
        assert [w.id for w in await conn.read_work_orders()] == ["WO-1", "WO-2", "WO-3"]

    @pytest.mark.asyncio
    async def test_connect_reads_off_the_event_loop(
        self, tmp_path: Path, workbook: _OverlapDetectingWorkbook
    ) -> None:
        path = tmp_path / "odl.xlsx"
        TestWritesPreserveTheFile._workbook(path)
        conn = ExcelCsvConnector(config=TestWritesPreserveTheFile._config(path))
        workbook.hold = "_read_xlsx_rows"

        connecting = asyncio.create_task(conn.connect())
        assert await asyncio.to_thread(workbook.held.wait, 5)
        assert not connecting.done()  # the event loop runs while the sheet is read
        workbook.release.set()
        await connecting

        assert [w.id for w in await conn.read_work_orders()] == ["WO-1", "WO-2"]


class TestRefresh:
    @pytest.mark.asyncio
    async def test_refresh_picks_up_new_data(self, tmp_path: Path) -> None:
        import openpyxl

        f = tmp_path / "assets.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws.append(["Codice", "Nome"])
        ws.append(["P-001", "Pompa 1"])
        wb.save(str(f))
        wb.close()

        schema = SheetSchema(
            path=str(f),
            columns=[
                ColumnMapping(column="Codice", field="id", required=True),
                ColumnMapping(column="Nome", field="name", required=True),
            ],
        )
        config = ExcelConnectorConfig(asset_registry=schema)
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assert len(await conn.read_assets()) == 1

        # Add a row externally
        wb = openpyxl.load_workbook(str(f))
        wb.active.append(["P-002", "Pompa 2"])
        wb.save(str(f))
        wb.close()

        conn.refresh()
        assert len(await conn.read_assets()) == 2


class TestFormulaGuardLossless:
    """_guard_formula / _strip_formula_guard must be a true inverse for every
    value class, including ones that already start with an apostrophe + trigger
    (the corruption case that the naive guard turned "'=approved" into
    "=approved" on read)."""

    @pytest.mark.parametrize(
        "value",
        [
            "=SUM(A1:A2)",
            "+1",
            "-1",
            "@cmd",
            "'=approved",  # literal apostrophe + trigger — must NOT be corrupted
            "''=x",  # double apostrophe + trigger
            "'note",  # apostrophe + non-trigger — left untouched
            "'",  # lone apostrophe
            "plain text",
            "",
        ],
    )
    def test_guard_strip_roundtrip(self, value: str) -> None:
        assert _strip_formula_guard(_guard_formula(value)) == value

    def test_trigger_is_neutralized_on_write(self) -> None:
        assert _guard_formula("=SUM(A1)") == "'=SUM(A1)"

    def test_ambiguous_literal_is_escaped_on_write(self) -> None:
        # The on-disk form gains an extra apostrophe so the strip is unambiguous.
        assert _guard_formula("'=approved") == "''=approved"
