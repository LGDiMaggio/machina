"""Tests for GenericSqlConnector failure-mode catalog and asset linkage.

Covers R6 (FailureMode table mapping → catalog) and R8 (semicolon-
delimited failure-code column → Asset.failure_modes).  Mocked pyodbc,
no real database.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from machina.connectors.capabilities import Capability
from machina.connectors.sql.generic import (
    GenericSqlConnector,
    _build_asset,
    _dict_to_failure_mode,
    _split_codes,
)
from machina.connectors.sql.schema import (
    FieldMapping,
    SqlConnectorConfig,
    TableMapping,
)
from machina.exceptions import ConnectorSchemaError

# ------------------------------------------------------------------
# Helpers: configs and mocked connections
# ------------------------------------------------------------------


def _asset_mapping(*, with_failure_codes: bool = False) -> TableMapping:
    fields = {
        "id": FieldMapping(column="ASSET_ID"),
        "name": FieldMapping(column="ASSET_NAME"),
    }
    if with_failure_codes:
        fields["failure_modes"] = FieldMapping(column="FAIL_CODES")
    return TableMapping(query="SELECT * FROM ASSETS", entity="Asset", fields=fields)


def _failure_mode_mapping(*, code_column: str = "FM_CODE") -> TableMapping:
    return TableMapping(
        query="SELECT * FROM FAILURE_MODES",
        entity="FailureMode",
        fields={
            "code": FieldMapping(column=code_column),
            "name": FieldMapping(column="FM_NAME"),
            "category": FieldMapping(column="FM_CAT"),
            "detection_methods": FieldMapping(column="FM_DETECT"),
            "mtbf_hours": FieldMapping(column="FM_MTBF"),
        },
    )


def _config(tables: dict[str, TableMapping]) -> SqlConnectorConfig:
    return SqlConnectorConfig(
        dsn="Driver={ODBC Driver 18};Server=localhost;",
        tables=tables,
    )


_ASSET_COLS = [("ASSET_ID",), ("ASSET_NAME",), ("FAIL_CODES",)]
_FM_COLS = [("FM_CODE",), ("FM_NAME",), ("FM_CAT",), ("FM_DETECT",), ("FM_MTBF",)]


def _make_smart_cursor(
    *,
    asset_rows: list[tuple[Any, ...]] | None = None,
    fm_rows: list[tuple[Any, ...]] | None = None,
) -> MagicMock:
    """Return a cursor mock that sets description/rows per executed query."""
    cursor = MagicMock()
    cursor.fetchone.return_value = (1,)
    cursor.fetchall.return_value = []

    def _execute(query: str, params: Any = None) -> None:
        q = query.upper()
        if "FAILURE_MODES" in q:
            cursor.description = _FM_COLS
            rows = fm_rows or []
        else:
            cursor.description = _ASSET_COLS
            rows = asset_rows or []
        cursor.fetchall.return_value = [] if "WHERE 1=0" in q else rows

    cursor.execute = MagicMock(side_effect=_execute)
    cursor.description = _ASSET_COLS + _FM_COLS
    return cursor


def _make_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


# ------------------------------------------------------------------
# Encoding: semicolon-delimited code strings
# ------------------------------------------------------------------


class TestSplitCodes:
    def test_basic_split(self) -> None:
        assert _split_codes("BEAR-WEAR-01;SEAL-LEAK-01") == [
            "BEAR-WEAR-01",
            "SEAL-LEAK-01",
        ]

    def test_empty_string_returns_empty_list(self) -> None:
        assert _split_codes("") == []

    def test_none_returns_empty_list(self) -> None:
        assert _split_codes(None) == []

    def test_whitespace_around_entries_trimmed(self) -> None:
        assert _split_codes("  BEAR-WEAR-01 ; SEAL-LEAK-01  ") == [
            "BEAR-WEAR-01",
            "SEAL-LEAK-01",
        ]

    def test_trailing_delimiter_tolerated(self) -> None:
        assert _split_codes("BEAR-WEAR-01;SEAL-LEAK-01;") == [
            "BEAR-WEAR-01",
            "SEAL-LEAK-01",
        ]

    def test_only_delimiters_and_whitespace_returns_empty(self) -> None:
        assert _split_codes(" ; ;; ") == []

    def test_existing_list_passed_through_cleaned(self) -> None:
        assert _split_codes([" A ", "", "B"]) == ["A", "B"]


# ------------------------------------------------------------------
# Pure mapper tests
# ------------------------------------------------------------------


class TestDictToFailureMode:
    def test_basic(self) -> None:
        fm = _dict_to_failure_mode(
            {
                "code": "BEAR-WEAR-01",
                "name": "Bearing wear",
                "category": "mechanical",
                "detection_methods": "vibration_analysis;thermography",
                "mtbf_hours": 8760,
            }
        )
        assert fm.code == "BEAR-WEAR-01"
        assert fm.name == "Bearing wear"
        assert fm.category == "mechanical"
        assert fm.detection_methods == ["vibration_analysis", "thermography"]
        assert fm.mtbf_hours == 8760.0

    def test_optional_fields_default(self) -> None:
        fm = _dict_to_failure_mode({"code": "SEAL-LEAK-01", "name": "Seal leakage"})
        assert fm.mechanism == ""
        assert fm.detection_methods == []
        assert fm.mtbf_hours is None
        assert fm.iso_14224_code is None


class TestBuildAsset:
    def test_failure_codes_resolved(self) -> None:
        asset = _build_asset(
            {"id": "P-001", "name": "Pompa 1", "failure_modes": "BEAR-WEAR-01;SEAL-LEAK-01"}
        )
        assert asset.failure_modes == ["BEAR-WEAR-01", "SEAL-LEAK-01"]

    def test_no_failure_codes_field(self) -> None:
        asset = _build_asset({"id": "P-001", "name": "Pompa 1"})
        assert asset.failure_modes == []

    def test_empty_codes_string(self) -> None:
        asset = _build_asset({"id": "P-001", "name": "Pompa 1", "failure_modes": ""})
        assert asset.failure_modes == []


# ------------------------------------------------------------------
# Capability declaration
# ------------------------------------------------------------------


class TestCapabilityDeclaration:
    def test_declared_when_mapping_configured(self) -> None:
        config = _config({"assets": _asset_mapping(), "failure_modes": _failure_mode_mapping()})
        connector = GenericSqlConnector(config=config)
        assert Capability.READ_FAILURE_MODES in connector.capabilities

    def test_absent_when_no_mapping(self) -> None:
        config = _config({"assets": _asset_mapping()})
        connector = GenericSqlConnector(config=config)
        assert Capability.READ_FAILURE_MODES not in connector.capabilities


# ------------------------------------------------------------------
# read_failure_modes (R6)
# ------------------------------------------------------------------


class TestReadFailureModes:
    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_configured_mapping_yields_catalog(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor(
            fm_rows=[
                ("BEAR-WEAR-01", "Bearing wear", "mechanical", "vibration_analysis", 8760),
                ("SEAL-LEAK-01", "Seal leakage", "mechanical", None, None),
            ]
        )
        mock_connect.return_value = _make_conn(cursor)
        config = _config({"assets": _asset_mapping(), "failure_modes": _failure_mode_mapping()})
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        catalog = await connector.read_failure_modes()
        assert len(catalog) == 2
        assert catalog[0].code == "BEAR-WEAR-01"
        assert catalog[0].detection_methods == ["vibration_analysis"]
        assert catalog[1].code == "SEAL-LEAK-01"
        assert catalog[1].detection_methods == []

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_no_mapping_returns_empty(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor()
        mock_connect.return_value = _make_conn(cursor)
        config = _config({"assets": _asset_mapping()})
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        assert await connector.read_failure_modes() == []


# ------------------------------------------------------------------
# Asset ↔ failure-code linkage (R8)
# ------------------------------------------------------------------


class TestAssetFailureCodeLinkage:
    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_asset_row_codes_resolve_to_failure_modes(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor(
            asset_rows=[
                ("P-001", "Pompa 1", "BEAR-WEAR-01;SEAL-LEAK-01"),
                ("P-002", "Pompa 2", " BEAR-WEAR-01 ; "),
                ("P-003", "Pompa 3", None),
            ]
        )
        mock_connect.return_value = _make_conn(cursor)
        config = _config({"assets": _asset_mapping(with_failure_codes=True)})
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        assets = await connector.read_assets()
        assert assets[0].failure_modes == ["BEAR-WEAR-01", "SEAL-LEAK-01"]
        assert assets[1].failure_modes == ["BEAR-WEAR-01"]
        assert assets[2].failure_modes == []


# ------------------------------------------------------------------
# Error path: schema validation at connect
# ------------------------------------------------------------------


class TestSchemaValidation:
    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_unknown_column_fails_at_connect(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor()
        mock_connect.return_value = _make_conn(cursor)
        config = _config({"failure_modes": _failure_mode_mapping(code_column="NO_SUCH_COL")})
        connector = GenericSqlConnector(config=config)
        with pytest.raises(ConnectorSchemaError, match="NO_SUCH_COL"):
            await connector.connect()
