"""Tests for excel_schema.py — YAML schema validation models."""

import pytest

from machina.connectors.docs.excel_schema import (
    ColumnMapping,
    ExcelConnectorConfig,
    SheetSchema,
    WatcherConfig,
)


class TestColumnMapping:
    def test_defaults(self) -> None:
        m = ColumnMapping(column="Codice", field="id")
        assert m.type == "str"
        assert m.required is False
        assert m.default is None
        assert m.coerce is None

    def test_all_fields(self) -> None:
        m = ColumnMapping(
            column="Prezzo",
            field="price",
            type="float",
            required=True,
            coerce="float_it",
        )
        assert m.type == "float"
        assert m.required is True
        assert m.coerce == "float_it"


class TestSheetSchema:
    def test_valid_schema(self) -> None:
        s = SheetSchema(
            path="/data/assets.xlsx",
            sheet="Macchine",
            columns=[
                ColumnMapping(column="Codice", field="id", required=True),
                ColumnMapping(column="Nome", field="name"),
            ],
        )
        assert s.path == "/data/assets.xlsx"
        assert s.sheet == "Macchine"
        assert len(s.columns) == 2

    def test_empty_columns_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            SheetSchema(path="/data/x.xlsx", columns=[])

    def test_duplicate_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="Duplicate field"):
            SheetSchema(
                path="/data/x.xlsx",
                columns=[
                    ColumnMapping(column="A", field="id"),
                    ColumnMapping(column="B", field="id"),
                ],
            )

    def test_default_sheet_name(self) -> None:
        s = SheetSchema(
            path="/data/x.xlsx",
            columns=[ColumnMapping(column="A", field="id")],
        )
        assert s.sheet == "Sheet1"


class TestWatcherConfig:
    def test_defaults(self) -> None:
        w = WatcherConfig()
        assert w.enabled is True
        assert w.debounce_ms == 500
        assert w.poll_fallback_sec == 30

    def test_bounds(self) -> None:
        with pytest.raises(ValueError):
            WatcherConfig(debounce_ms=50)
        with pytest.raises(ValueError):
            WatcherConfig(poll_fallback_sec=2)


class TestExcelConnectorConfig:
    def test_at_least_one_required(self) -> None:
        with pytest.raises(ValueError, match="At least one"):
            ExcelConnectorConfig()

    def test_asset_only(self) -> None:
        cfg = ExcelConnectorConfig(
            asset_registry=SheetSchema(
                path="/data/assets.xlsx",
                columns=[ColumnMapping(column="Codice", field="id", required=True)],
            )
        )
        assert cfg.asset_registry is not None
        assert cfg.work_orders is None

    def test_both(self) -> None:
        cfg = ExcelConnectorConfig(
            asset_registry=SheetSchema(
                path="/data/assets.xlsx",
                columns=[ColumnMapping(column="Codice", field="id", required=True)],
            ),
            work_orders=SheetSchema(
                path="/data/odl.xlsx",
                columns=[ColumnMapping(column="ID", field="id", required=True)],
                write_mode="append",
            ),
        )
        assert cfg.work_orders is not None
        assert cfg.work_orders.write_mode == "append"
