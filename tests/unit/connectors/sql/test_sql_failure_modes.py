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
from machina.connectors.sql.generic import GenericSqlConnector
from machina.connectors.sql.schema import (
    FieldMapping,
    SqlConnectorConfig,
    TableMapping,
)
from machina.exceptions import ConnectorError, ConnectorSchemaError

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
# Capability declaration
# ------------------------------------------------------------------
# (Encoding and pure-mapper tests live in
# tests/unit/connectors/test_entity_builders.py — the shared helpers'
# home module.)


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

    @pytest.mark.asyncio
    async def test_unconnected_read_raises_connector_error(self) -> None:
        """An un-connected provider raises ConnectorError, never a raw
        AttributeError — the runtime harvest keys its skip-and-degrade
        behavior on the ConnectorError contract."""
        config = _config({"failure_modes": _failure_mode_mapping()})
        connector = GenericSqlConnector(config=config)
        with pytest.raises(ConnectorError, match="Not connected"):
            await connector.read_failure_modes()

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_null_code_row_raises_connector_schema_error(
        self, mock_connect: MagicMock
    ) -> None:
        """A NULL/invalid code row surfaces as a loud ConnectorSchemaError
        (a ConnectorError the harvest can degrade on), not a pydantic
        ValidationError that aborts agent start, and never a literal
        'None' catalog entry."""
        cursor = _make_smart_cursor(
            fm_rows=[(None, None, "mechanical", None, None)],
        )
        mock_connect.return_value = _make_conn(cursor)
        config = _config({"failure_modes": _failure_mode_mapping()})
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        with pytest.raises(ConnectorSchemaError, match="invalid row"):
            await connector.read_failure_modes()


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
