"""Tests for GenericSqlConnector — mocked pyodbc, no real database."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from machina.connectors.sql.generic import (
    GenericSqlConnector,
    _coerce_value,
    _dict_to_asset,
    _dict_to_work_order,
    _is_transient,
    _row_to_dict,
)
from machina.connectors.sql.schema import (
    FieldMapping,
    SqlConnectorConfig,
    TableMapping,
)
from machina.domain.asset import AssetType, Criticality
from machina.domain.work_order import WorkOrder, WorkOrderType
from machina.exceptions import (
    ConnectorConfigError,
    ConnectorSchemaError,
    ConnectorTransientError,
)

# ------------------------------------------------------------------
# Helper: build a config with mocked connection
# ------------------------------------------------------------------


def _basic_config(
    *,
    capabilities: str = "read_only",
    with_insert: bool = False,
) -> SqlConnectorConfig:
    fields = {
        "id": FieldMapping(column="ASSET_ID"),
        "name": FieldMapping(column="ASSET_NAME"),
        "type": FieldMapping(
            column="ASSET_CAT",
            enum_map={"POM": "rotating_equipment", "VAL": "instrument"},
        ),
        "criticality": FieldMapping(
            column="CRIT",
            enum_map={"A": "A", "B": "B", "C": "C"},
        ),
    }
    wo_fields: dict[str, FieldMapping] = {
        "id": FieldMapping(column="WO_ID"),
        "asset_id": FieldMapping(column="ASSET_ID"),
        "description": FieldMapping(column="WO_DESC"),
    }
    tables: dict[str, TableMapping] = {
        "assets": TableMapping(
            query="SELECT * FROM ASSETS",
            entity="Asset",
            fields=fields,
        ),
        "work_orders": TableMapping(
            query="SELECT * FROM WORK_ORDERS",
            entity="WorkOrder",
            fields=wo_fields,
            insert_table="WORK_ORDERS" if with_insert else None,
            insert_columns={"id": "WO_ID", "asset_id": "ASSET_ID", "description": "WO_DESC"}
            if with_insert
            else None,
        ),
    }
    return SqlConnectorConfig(
        dsn="Driver={ODBC Driver 18};Server=localhost;",
        capabilities=capabilities,
        tables=tables,
    )


_ASSET_COLS = [("ASSET_ID",), ("ASSET_NAME",), ("ASSET_CAT",), ("CRIT",)]
_WO_COLS = [("WO_ID",), ("ASSET_ID",), ("WO_DESC",)]
_ALL_COLS = _ASSET_COLS + _WO_COLS


def _make_smart_cursor(
    *,
    read_rows: list[tuple[Any, ...]] | None = None,
) -> MagicMock:
    """Return a cursor mock that sets description based on the executed query."""
    cursor = MagicMock()
    cursor.fetchone.return_value = (1,)
    cursor.fetchall.return_value = read_rows or []

    def _execute(query: str, params: Any = None) -> None:
        q = query.upper()
        if "ASSETS" in q:
            cursor.description = _ASSET_COLS
        elif "WORK_ORDERS" in q:
            cursor.description = _WO_COLS
        else:
            cursor.description = _ALL_COLS

        if read_rows and "WHERE 1=0" not in q:
            cursor.fetchall.return_value = read_rows
        else:
            cursor.fetchall.return_value = []

    cursor.execute = MagicMock(side_effect=_execute)
    cursor.description = _ALL_COLS
    return cursor


def _make_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


# ------------------------------------------------------------------
# Pure function tests
# ------------------------------------------------------------------


class TestCoerceValue:
    def test_none_returns_default(self) -> None:
        m = FieldMapping(column="X", default="fallback")
        assert _coerce_value(None, m) == "fallback"

    def test_named_coercer(self) -> None:
        m = FieldMapping(column="X", coerce="db2_date")
        result = _coerce_value("1240416", m)
        assert result == date(2024, 4, 16)

    def test_enum_map(self) -> None:
        m = FieldMapping(column="X", enum_map={"POM": "rotating_equipment"})
        assert _coerce_value("POM", m) == "rotating_equipment"

    def test_coerce_then_enum(self) -> None:
        m = FieldMapping(column="X", coerce="strip", enum_map={"POM": "rotating_equipment"})
        assert _coerce_value("  POM  ", m) == "rotating_equipment"

    def test_unknown_coercer_raises(self) -> None:
        m = FieldMapping(column="X", coerce="nonexistent")
        with pytest.raises(ConnectorConfigError, match="Unknown coercer"):
            _coerce_value("val", m)


class TestRowToDict:
    def test_basic(self) -> None:
        mapping = TableMapping(
            query="SELECT 1",
            entity="Asset",
            fields={
                "id": FieldMapping(column="COD"),
                "name": FieldMapping(column="NOM"),
            },
        )
        row = ("P-001", "Pompa 1")
        columns = ["COD", "NOM"]
        result = _row_to_dict(row, columns, mapping)
        assert result == {"id": "P-001", "name": "Pompa 1"}


class TestDictToAsset:
    def test_basic(self) -> None:
        d = {"id": "P-001", "name": "Pompa", "type": "rotating_equipment", "criticality": "A"}
        asset = _dict_to_asset(d)
        assert asset.id == "P-001"
        assert asset.type == AssetType.ROTATING_EQUIPMENT
        assert asset.criticality == Criticality.A

    def test_defaults(self) -> None:
        d = {"id": "X", "name": "Y"}
        asset = _dict_to_asset(d)
        assert asset.type == AssetType.ROTATING_EQUIPMENT
        assert asset.criticality == Criticality.C


class TestDictToWorkOrder:
    def test_basic(self) -> None:
        d = {"id": "WO-001", "asset_id": "P-001", "description": "Fix pump"}
        wo = _dict_to_work_order(d)
        assert wo.id == "WO-001"
        assert wo.asset_id == "P-001"


class TestIsTransient:
    def test_deadlock_1205(self) -> None:
        assert _is_transient(Exception("Error 1205: deadlock victim"))

    def test_db2_timeout(self) -> None:
        assert _is_transient(Exception("SQLCODE=-911"))

    def test_non_transient(self) -> None:
        assert not _is_transient(Exception("Syntax error in SQL"))


# ------------------------------------------------------------------
# Connector tests (mocked connection)
# ------------------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_connect_validates_schemas(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor()
        mock_connect.return_value = _make_conn(cursor)
        config = _basic_config()
        conn = GenericSqlConnector(config=config)
        await conn.connect()
        assert cursor.execute.called

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_missing_column_raises(self, mock_connect: MagicMock) -> None:
        cursor = MagicMock()
        cursor.description = [("WRONG_COL",)]
        cursor.execute = MagicMock()
        cursor.fetchone.return_value = (1,)
        mock_connect.return_value = _make_conn(cursor)
        config = _basic_config()
        conn = GenericSqlConnector(config=config)
        with pytest.raises(ConnectorSchemaError, match="ASSET_ID"):
            await conn.connect()


class TestReadAssets:
    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_read_assets(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor(
            read_rows=[
                ("P-001", "Pompa 1", "POM", "A"),
                ("V-001", "Valvola 1", "VAL", "B"),
            ]
        )
        mock_connect.return_value = _make_conn(cursor)
        config = _basic_config()
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        assets = await connector.read_assets()
        assert len(assets) == 2
        assert assets[0].id == "P-001"
        assert assets[0].type == AssetType.ROTATING_EQUIPMENT
        assert assets[1].type == AssetType.INSTRUMENT


class TestReadWriteCapabilities:
    def test_read_only_capabilities(self) -> None:
        config = _basic_config(capabilities="read_only")
        connector = GenericSqlConnector(config=config)
        from machina.connectors.capabilities import Capability

        assert Capability.READ_ASSETS in connector.capabilities
        assert Capability.CREATE_WORK_ORDER not in connector.capabilities

    def test_read_write_capabilities(self) -> None:
        config = _basic_config(capabilities="read_write")
        connector = GenericSqlConnector(config=config)
        from machina.connectors.capabilities import Capability

        assert Capability.CREATE_WORK_ORDER in connector.capabilities


class TestCreateWorkOrder:
    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_insert(self, mock_connect: MagicMock) -> None:
        validate_cursor = _make_smart_cursor()
        insert_cursor = MagicMock()
        insert_cursor.fetchone.return_value = (1,)
        mock_conn_obj = MagicMock()
        call_count = 0

        def cursor_factory() -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return validate_cursor
            return insert_cursor

        mock_conn_obj.cursor = cursor_factory
        mock_connect.return_value = mock_conn_obj
        config = _basic_config(capabilities="read_write", with_insert=True)
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        wo = WorkOrder(
            id="WO-001",
            type=WorkOrderType.CORRECTIVE,
            asset_id="P-001",
            description="Fix pump seal",
        )
        result = await connector.create_work_order(wo)
        assert result.id == "WO-001"
        insert_cursor.execute.assert_called_once()
        mock_conn_obj.commit.assert_called()

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_read_only_rejects_write(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor()
        mock_connect.return_value = _make_conn(cursor)
        config = _basic_config(capabilities="read_only")
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        wo = WorkOrder(id="WO-001", type=WorkOrderType.CORRECTIVE, asset_id="P-001")
        with pytest.raises(ConnectorConfigError, match="Write operations not enabled"):
            await connector.create_work_order(wo)


class TestRetry:
    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_transient_error_retries(self, mock_connect: MagicMock) -> None:
        validate_cursor = _make_smart_cursor()
        mock_conn_obj = MagicMock()
        call_count = 0

        def cursor_factory() -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return validate_cursor
            c = MagicMock()
            if call_count == 2:
                c.execute.side_effect = Exception("Error 1205: deadlock victim")
            else:
                c.description = _ASSET_COLS
                c.fetchall.return_value = [("P-001", "Pompa", "POM", "A")]
            return c

        mock_conn_obj.cursor = cursor_factory
        mock_connect.return_value = mock_conn_obj
        config = _basic_config()
        config.retry.base_backoff = 0.01
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        assets = await connector.read_assets()
        assert len(assets) == 1

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_transient_exhausted_raises(self, mock_connect: MagicMock) -> None:
        validate_cursor = _make_smart_cursor()
        mock_conn_obj = MagicMock()
        call_count = 0

        def cursor_factory() -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return validate_cursor
            c = MagicMock()
            c.execute.side_effect = Exception("Error 1205: deadlock victim")
            return c

        mock_conn_obj.cursor = cursor_factory
        mock_connect.return_value = mock_conn_obj
        config = _basic_config()
        config.retry.max_retries = 1
        config.retry.base_backoff = 0.01
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        with pytest.raises(ConnectorTransientError, match="1205"):
            await connector.read_assets()


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_not_connected(self) -> None:
        config = _basic_config()
        connector = GenericSqlConnector(config=config)
        health = await connector.health_check()
        assert health.status.value == "unhealthy"

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_healthy(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor()
        mock_connect.return_value = _make_conn(cursor)
        config = _basic_config()
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        health = await connector.health_check()
        assert health.status.value == "healthy"


class TestDisconnect:
    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_disconnect(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor()
        conn_mock = _make_conn(cursor)
        mock_connect.return_value = conn_mock
        config = _basic_config()
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        await connector.disconnect()
        conn_mock.close.assert_called_once()
