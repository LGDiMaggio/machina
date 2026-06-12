"""Tests for ExcelCsvConnector failure-modes sheet and asset failure-code linkage.

Covers R7 (optional failure-modes sheet behind a conditionally declared
READ_FAILURE_MODES capability) and R8 (asset rows carrying a
semicolon-delimited failure-code cell mapped to Asset.failure_modes).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from machina.connectors.capabilities import Capability
from machina.connectors.docs.excel import ExcelCsvConnector, _split_semicolon_list
from machina.connectors.docs.excel_schema import (
    ColumnMapping,
    ExcelConnectorConfig,
    SheetSchema,
)
from machina.exceptions import ConnectorConfigError, ConnectorSchemaError

# ------------------------------------------------------------------
# Fixture helpers — temp CSV files (no openpyxl needed)
# ------------------------------------------------------------------


def _fm_schema(path: str) -> SheetSchema:
    return SheetSchema(
        path=path,
        columns=[
            ColumnMapping(column="Codice", field="code", required=True),
            ColumnMapping(column="Nome", field="name", required=True),
            ColumnMapping(column="Meccanismo", field="mechanism"),
            ColumnMapping(column="Categoria", field="category"),
            ColumnMapping(column="Rilevamento", field="detection_methods"),
            ColumnMapping(column="MTBF (ore)", field="mtbf_hours", coerce="float_it"),
            ColumnMapping(column="ISO 14224", field="iso_14224_code"),
        ],
    )


def _fm_csv(tmp_path: Path) -> Path:
    f = tmp_path / "failure_modes.csv"
    f.write_text(
        "Codice,Nome,Meccanismo,Categoria,Rilevamento,MTBF (ore),ISO 14224\n"
        "BEAR-WEAR-01,Usura cuscinetto,fatigue,mechanical,"
        "vibration_analysis; oil_analysis,12000,VIB\n"
        "SEAL-LEAK-01,Perdita tenuta,wear,mechanical,visual_inspection,8000,ELP\n",
        encoding="utf-8",
    )
    return f


def _asset_schema(path: str) -> SheetSchema:
    return SheetSchema(
        path=path,
        columns=[
            ColumnMapping(column="Codice", field="id", required=True),
            ColumnMapping(column="Nome", field="name", required=True),
            ColumnMapping(column="Codici Guasto", field="failure_modes"),
        ],
    )


def _asset_csv(tmp_path: Path, rows: list[str]) -> Path:
    f = tmp_path / "assets.csv"
    f.write_text(
        "Codice,Nome,Codici Guasto\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return f


# ------------------------------------------------------------------
# R7 — capability declaration
# ------------------------------------------------------------------


class TestCapabilityDeclaration:
    def test_declared_when_failure_modes_sheet_configured(self, tmp_path: Path) -> None:
        config = ExcelConnectorConfig(failure_modes=_fm_schema(str(_fm_csv(tmp_path))))
        conn = ExcelCsvConnector(config=config)
        assert Capability.READ_FAILURE_MODES in conn.capabilities

    def test_base_capabilities_still_present(self, tmp_path: Path) -> None:
        config = ExcelConnectorConfig(failure_modes=_fm_schema(str(_fm_csv(tmp_path))))
        conn = ExcelCsvConnector(config=config)
        assert Capability.READ_ASSETS in conn.capabilities
        assert Capability.CREATE_WORK_ORDER in conn.capabilities

    def test_absent_when_not_configured(self, tmp_path: Path) -> None:
        asset_file = _asset_csv(tmp_path, ["P-001,Pompa 1,"])
        config = ExcelConnectorConfig(asset_registry=_asset_schema(str(asset_file)))
        conn = ExcelCsvConnector(config=config)
        assert Capability.READ_FAILURE_MODES not in conn.capabilities

    def test_failure_modes_only_config_is_valid(self, tmp_path: Path) -> None:
        """A config with only a failure_modes sheet passes schema validation."""
        config = ExcelConnectorConfig(failure_modes=_fm_schema(str(_fm_csv(tmp_path))))
        assert config.asset_registry is None
        assert config.failure_modes is not None


# ------------------------------------------------------------------
# R7 — reading the failure-modes sheet
# ------------------------------------------------------------------


class TestReadFailureModes:
    @pytest.mark.asyncio
    async def test_read_failure_modes(self, tmp_path: Path) -> None:
        config = ExcelConnectorConfig(failure_modes=_fm_schema(str(_fm_csv(tmp_path))))
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        modes = await conn.read_failure_modes()
        assert len(modes) == 2
        by_code = {m.code: m for m in modes}
        bear = by_code["BEAR-WEAR-01"]
        assert bear.name == "Usura cuscinetto"
        assert bear.mechanism == "fatigue"
        assert bear.category == "mechanical"
        assert bear.mtbf_hours == 12000.0
        assert bear.iso_14224_code == "VIB"
        # Semicolon-delimited list cell, whitespace trimmed
        assert bear.detection_methods == ["vibration_analysis", "oil_analysis"]
        assert by_code["SEAL-LEAK-01"].detection_methods == ["visual_inspection"]

    @pytest.mark.asyncio
    async def test_no_sheet_configured_returns_empty(self, tmp_path: Path) -> None:
        asset_file = _asset_csv(tmp_path, ["P-001,Pompa 1,"])
        config = ExcelConnectorConfig(asset_registry=_asset_schema(str(asset_file)))
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assert await conn.read_failure_modes() == []

    @pytest.mark.asyncio
    async def test_disconnect_clears_cache(self, tmp_path: Path) -> None:
        config = ExcelConnectorConfig(failure_modes=_fm_schema(str(_fm_csv(tmp_path))))
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assert len(await conn.read_failure_modes()) == 2
        await conn.disconnect()
        assert await conn.read_failure_modes() == []


# ------------------------------------------------------------------
# R8 — asset ↔ failure-code linkage
# ------------------------------------------------------------------


class TestAssetFailureCodeLinkage:
    @pytest.mark.asyncio
    async def test_semicolon_delimited_codes(self, tmp_path: Path) -> None:
        asset_file = _asset_csv(
            tmp_path,
            [
                "P-001,Pompa 1,BEAR-WEAR-01;SEAL-LEAK-01",
                "C-001,Compressore 1,IMP-EROS-01",
            ],
        )
        config = ExcelConnectorConfig(asset_registry=_asset_schema(str(asset_file)))
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        by_id = {a.id: a for a in await conn.read_assets()}
        assert by_id["P-001"].failure_modes == ["BEAR-WEAR-01", "SEAL-LEAK-01"]
        assert by_id["C-001"].failure_modes == ["IMP-EROS-01"]

    @pytest.mark.asyncio
    async def test_empty_cell_yields_empty_list(self, tmp_path: Path) -> None:
        asset_file = _asset_csv(tmp_path, ["P-001,Pompa 1,"])
        config = ExcelConnectorConfig(asset_registry=_asset_schema(str(asset_file)))
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assets = await conn.read_assets()
        assert assets[0].failure_modes == []

    @pytest.mark.asyncio
    async def test_whitespace_trimmed(self, tmp_path: Path) -> None:
        asset_file = _asset_csv(tmp_path, ['"P-001","Pompa 1"," BEAR-WEAR-01 ; SEAL-LEAK-01 "'])
        config = ExcelConnectorConfig(asset_registry=_asset_schema(str(asset_file)))
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assets = await conn.read_assets()
        assert assets[0].failure_modes == ["BEAR-WEAR-01", "SEAL-LEAK-01"]

    @pytest.mark.asyncio
    async def test_trailing_delimiter_tolerated(self, tmp_path: Path) -> None:
        asset_file = _asset_csv(tmp_path, ["P-001,Pompa 1,BEAR-WEAR-01;"])
        config = ExcelConnectorConfig(asset_registry=_asset_schema(str(asset_file)))
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assets = await conn.read_assets()
        assert assets[0].failure_modes == ["BEAR-WEAR-01"]


# ------------------------------------------------------------------
# Encoding helper unit tests
# ------------------------------------------------------------------


class TestSplitSemicolonList:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, []),
            ("", []),
            ("   ", []),
            ("A", ["A"]),
            ("A;B", ["A", "B"]),
            (" A ; B ", ["A", "B"]),
            ("A;", ["A"]),
            (";A", ["A"]),
            ("A;;B;", ["A", "B"]),
            (["A", " B "], ["A", "B"]),
        ],
    )
    def test_split(self, value: Any, expected: list[str]) -> None:
        assert _split_semicolon_list(value) == expected


# ------------------------------------------------------------------
# Error paths — validation at connect()
# ------------------------------------------------------------------


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_missing_failure_modes_file_raises(self, tmp_path: Path) -> None:
        config = ExcelConnectorConfig(
            failure_modes=_fm_schema(str(tmp_path / "nonexistent.csv")),
        )
        conn = ExcelCsvConnector(config=config)
        with pytest.raises(ConnectorConfigError, match="Failure modes file not found"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_bad_header_raises(self, tmp_path: Path) -> None:
        """A file missing a required column fails validation with a clear message."""
        f = tmp_path / "failure_modes.csv"
        f.write_text("WrongColumn,Nome\nX,Y\n", encoding="utf-8")
        config = ExcelConnectorConfig(failure_modes=_fm_schema(str(f)))
        conn = ExcelCsvConnector(config=config)
        with pytest.raises(ConnectorSchemaError, match="Required columns missing"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_health_check_reports_missing_failure_modes_file(self, tmp_path: Path) -> None:
        config = ExcelConnectorConfig(
            failure_modes=_fm_schema(str(tmp_path / "gone.csv")),
        )
        conn = ExcelCsvConnector(config=config)
        health = await conn.health_check()
        assert health.status.value == "unhealthy"
        assert "failure_modes" in (health.message or "")
