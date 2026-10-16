"""Tests for GenericSqlConnector — mocked pyodbc, no real database."""

from __future__ import annotations

import asyncio
import threading
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from machina.connectors.base import ConnectorStatus
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
    ConnectorTimeoutError,
    ConnectorTransientError,
)

# ------------------------------------------------------------------
# Helper: build a config with mocked connection
# ------------------------------------------------------------------


def _basic_config(
    *,
    capabilities: str = "read_only",
    with_insert: bool = False,
    **settings: Any,
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
        **settings,
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


class _FakeOdbcError(Exception):
    """Stand-in for a pyodbc error: the SQLSTATE comes first, then the message."""


class _FakeJavaSqlError(Exception):
    """Stand-in for a java.sql.SQLException reached through JPype.

    ``str()`` gives its ``toString()``: the class name, then the message.
    """

    def __init__(self, text: str, sqlstate: str | None) -> None:
        super().__init__(text)
        self._sqlstate = sqlstate

    def getSQLState(self) -> str | None:  # noqa: N802 - the Java method
        return self._sqlstate


class _FakeJdbcError(Exception):
    """Stand-in for jaydebeapi.DatabaseError, which wraps the Java exception."""

    def __init__(self, text: str, sqlstate: str | None) -> None:
        super().__init__(_FakeJavaSqlError(text, sqlstate))


# What pyodbc raises when the driver stops a statement at its query timeout.
_SQLSERVER_TIMEOUT = (
    "HYT00",
    "[HYT00] [Microsoft][ODBC Driver 18 for SQL Server]Query timeout expired (0) (SQLExecDirectW)",
)


def _make_stuck_conn(
    started: threading.Event, release: threading.Event, *, stuck_on: str
) -> MagicMock:
    """A connection whose ``stuck_on`` statement blocks until ``release`` is set.

    It stands for a statement waiting on a lock in a driver that does not stop
    it at the query timeout.
    """
    cursor = _make_smart_cursor(read_rows=[("WO-1", "P-001", "Seal")])
    answer = cursor.execute.side_effect

    def _execute(query: str, params: Any = None) -> None:
        if query.upper().startswith(stuck_on) and "WHERE 1=0" not in query.upper():
            started.set()
            release.wait(timeout=10)
        answer(query, params)

    cursor.execute = MagicMock(side_effect=_execute)
    return _make_conn(cursor)


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


class TestIsTimeout:
    """A statement stopped by its timeout, in the forms the drivers report it."""

    @pytest.mark.parametrize(
        "exc",
        [
            # pyodbc: (SQLSTATE, message)
            _FakeOdbcError(*_SQLSERVER_TIMEOUT),
            _FakeOdbcError(
                "HYT01",
                "[HYT01] [Microsoft][ODBC Driver 18 for SQL Server]Connection timeout expired "
                "(0) (SQLEndTran)",
            ),
            _FakeOdbcError(  # Db2 LUW CLI
                "HY008",
                "[HY008] [IBM][CLI Driver][DB2/NT64] SQL0952N  Processing was cancelled due to "
                "an interrupt.  SQLSTATE=57014\r\n (-952) (SQLExecDirectW)",
            ),
            _FakeOdbcError(  # IBM i Access: refused on the optimizer's estimate
                "HY000",
                "[HY000] [IBM][System i Access ODBC Driver][DB2 for i5/OS]SQL0666 - SQL query "
                "exceeds specified time limit or storage limit. (-666) (SQLExecDirectW)",
            ),
            _FakeOdbcError(  # PostgreSQL statement_timeout
                "57014",
                "[57014] ERROR: canceling statement due to statement timeout;\n"
                "Error while executing the query (1) (SQLExecDirectW)",
            ),
            # jaydebeapi: the Java exception, with its SQLSTATE
            _FakeJdbcError(  # mssql-jdbc 7.0+
                "java.sql.SQLTimeoutException: The query has timed out.", "HY008"
            ),
            _FakeJdbcError(  # pgjdbc: no timeout in the text, only the SQLSTATE
                "org.postgresql.util.PSQLException: ERROR: canceling statement due to user "
                "request",
                "57014",
            ),
            _FakeJdbcError(  # Db2 JCC
                "com.ibm.db2.jcc.am.SqlTimeoutException: DB2 SQL Error: SQLCODE=-952, "
                "SQLSTATE=57014, SQLERRMC=null, DRIVER=4.33.31",
                "57014",
            ),
            _FakeJdbcError(  # jt400, query timeout mechanism=cancel
                "java.sql.SQLTimeoutException: [SQL0952] Processing of the SQL statement ended.",
                "57014",
            ),
            _FakeJdbcError(  # jt400, default mechanism: refused on the estimate
                "java.sql.SQLException: [SQL0666] SQL query exceeds specified time limit or "
                "storage limit.",
                "57005",
            ),
            _FakeJdbcError(  # MySQL Connector/J 8+: no SQLSTATE
                "com.mysql.cj.jdbc.exceptions.MySQLTimeoutException: Statement cancelled due "
                "to timeout or client request",
                None,
            ),
        ],
    )
    def test_timeouts(self, exc: Exception) -> None:
        from machina.connectors.sql.generic import _is_timeout

        assert _is_timeout(exc)

    @pytest.mark.parametrize(
        "exc",
        [
            Exception("Error 1205: deadlock victim"),
            _FakeOdbcError(
                "40001",
                "[40001] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Transaction "
                "(Process ID 52) was deadlocked on lock resources with another process and has "
                "been chosen as the deadlock victim. Rerun the transaction. (1205) "
                "(SQLExecDirectW)",
            ),
            _FakeOdbcError("42S02", "[42S02] Invalid object name 'WO'. (208) (SQLExecDirectW)"),
            _FakeOdbcError(  # a timeout SQLSTATE quoted in the data is not one
                "22001",
                "[22001] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]String or binary "
                "data would be truncated in table 'MAINT.dbo.WORK_ORDERS', column 'WO_DESC'. "
                "Truncated value: 'Pump 57014 seal'. (2628) (SQLExecDirectW)",
            ),
            _FakeJdbcError(
                "org.postgresql.util.PSQLException: ERROR: duplicate key value violates unique "
                'constraint "work_orders_pkey"',
                "23505",
            ),
        ],
    )
    def test_other_errors(self, exc: Exception) -> None:
        from machina.connectors.sql.generic import _is_timeout

        assert not _is_timeout(exc)


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


class TestYamlSettingsAndCallContract:
    """Built from flat YAML settings; honours the agent/MCP call shapes."""

    def test_flat_settings_build_the_connector(self) -> None:
        from machina.connectors.capabilities import Capability

        settings = _basic_config(capabilities="read_write").model_dump()
        connector = GenericSqlConnector(**settings)
        assert Capability.CREATE_WORK_ORDER in connector.capabilities

    def test_config_and_flat_settings_together_are_refused(self) -> None:
        config = _basic_config()
        with pytest.raises(ConnectorConfigError, match="either"):
            GenericSqlConnector(config=config, dsn=config.dsn)

    def test_invalid_settings_never_echo_the_dsn(self) -> None:
        secret_dsn = "Driver={ODBC Driver 18};Server=db;UID=svc;PWD=hunter2-secret;"
        with pytest.raises(ConnectorConfigError, match="tables") as excinfo:
            GenericSqlConnector(dsn=secret_dsn, capabilities="read_write")
        assert "hunter2" not in str(excinfo.value)

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_get_asset(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor(
            read_rows=[("P-001", "Pompa 1", "POM", "A"), ("V-001", "Valvola 1", "VAL", "B")]
        )
        mock_connect.return_value = _make_conn(cursor)
        connector = GenericSqlConnector(config=_basic_config())
        await connector.connect()
        asset = await connector.get_asset("V-001")
        assert asset is not None
        assert asset.name == "Valvola 1"
        assert await connector.get_asset("NOPE") is None

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_read_work_orders_filters(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor(
            read_rows=[
                ("WO-1", "P-001", "Seal"),
                ("WO-2", "V-001", "Valve"),
                ("WO-3", "P-001", "X"),
            ]
        )
        mock_connect.return_value = _make_conn(cursor)
        connector = GenericSqlConnector(config=_basic_config())
        await connector.connect()
        on_pump = await connector.read_work_orders(asset_id="P-001")
        assert [wo.id for wo in on_pump] == ["WO-1", "WO-3"]
        assert len(await connector.read_work_orders(status="created")) == 3
        assert await connector.read_work_orders(status="closed") == []
        # None means "no status filter", as the empty string does — not "None".
        assert len(await connector.read_work_orders(status=None)) == 3  # type: ignore[arg-type]

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_health_check_waits_for_the_shared_connection(
        self, mock_connect: MagicMock
    ) -> None:
        """The health probe uses the same DB-API connection as reads and writes."""
        mock_connect.return_value = _make_conn(_make_smart_cursor(read_rows=[]))
        connector = GenericSqlConnector(config=_basic_config())
        await connector.connect()
        async with connector._db_lock:
            probe = asyncio.create_task(connector.health_check())
            await asyncio.sleep(0.05)
            assert not probe.done()
        await probe

    def test_read_write_declares_create_but_not_the_unimplemented_update(self) -> None:
        from machina.connectors.capabilities import Capability

        connector = GenericSqlConnector(config=_basic_config(capabilities="read_write"))
        assert Capability.CREATE_WORK_ORDER in connector.capabilities
        assert Capability.UPDATE_WORK_ORDER not in connector.capabilities

    def test_config_given_as_a_dict_is_validated(self) -> None:
        connector = GenericSqlConnector(config=_basic_config().model_dump())
        assert connector.capabilities

    def test_unknown_setting_is_refused(self) -> None:
        settings = _basic_config().model_dump()
        settings["capabilites"] = "read_write"  # typo
        with pytest.raises(ConnectorConfigError, match="capabilites"):
            GenericSqlConnector(**settings)

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_create_is_idempotent_on_existing_id(self, mock_connect: MagicMock) -> None:
        cursor = _make_smart_cursor(read_rows=[("WO-001", "P-001", "Existing")])
        conn_obj = _make_conn(cursor)
        mock_connect.return_value = conn_obj
        connector = GenericSqlConnector(
            config=_basic_config(capabilities="read_write", with_insert=True)
        )
        await connector.connect()
        cursor.execute.reset_mock()

        result = await connector.create_work_order(
            WorkOrder(
                id="WO-001", type=WorkOrderType.CORRECTIVE, asset_id="P-001", description="Dup"
            )
        )

        assert result.description == "Existing"  # the stored record, not the retry
        executed = [str(call.args[0]).upper() for call in cursor.execute.call_args_list]
        assert not any(q.startswith("INSERT") for q in executed)
        conn_obj.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_keyword_update_raises_connector_error_not_type_error(self) -> None:
        from machina.domain.work_order import WorkOrderStatus
        from machina.exceptions import ConnectorError

        connector = GenericSqlConnector(config=_basic_config(capabilities="read_write"))
        with pytest.raises(ConnectorError, match="not yet implemented"):
            await connector.update_work_order("WO-1", status=WorkOrderStatus.CLOSED)


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
        # The idempotency check reads first; exactly one INSERT follows.
        inserts = [
            call
            for call in insert_cursor.execute.call_args_list
            if str(call.args[0]).upper().startswith("INSERT")
        ]
        assert len(inserts) == 1
        mock_conn_obj.commit.assert_called()

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_concurrent_same_id_creates_insert_once(self, mock_connect: MagicMock) -> None:
        """The ID probe and the INSERT are serialized, so a race cannot duplicate."""
        import asyncio

        stored: list[tuple[Any, ...]] = []
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)

        def _execute(query: str, params: Any = None) -> None:
            if query.upper().startswith("INSERT"):
                stored.append(tuple(params))
                return
            cursor.description = _WO_COLS if "WORK_ORDERS" in query.upper() else _ALL_COLS
            cursor.fetchall.return_value = [] if "WHERE 1=0" in query.upper() else list(stored)

        cursor.execute = MagicMock(side_effect=_execute)
        mock_connect.return_value = _make_conn(cursor)
        connector = GenericSqlConnector(
            config=_basic_config(capabilities="read_write", with_insert=True)
        )
        await connector.connect()
        wo = WorkOrder(id="WO-9", type=WorkOrderType.CORRECTIVE, asset_id="P-001", description="x")

        await asyncio.gather(connector.create_work_order(wo), connector.create_work_order(wo))

        assert len(stored) == 1

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_unparseable_existing_row_does_not_block_create(
        self, mock_connect: MagicMock
    ) -> None:
        """The idempotency probe matches IDs on raw rows; it does not validate them."""
        config = _basic_config(capabilities="read_write", with_insert=True)
        wo_mapping = config.tables["work_orders"]
        wo_mapping.fields["status"] = FieldMapping(column="WO_STATUS")
        columns = [*_WO_COLS, ("WO_STATUS",)]
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)

        def _execute(query: str, params: Any = None) -> None:
            cursor.description = columns if "WORK_ORDERS" in query.upper() else _ALL_COLS
            # A legacy row whose status no WorkOrderStatus value matches.
            legacy = [("WO-OLD", "P-001", "legacy", "APERTO")]
            cursor.fetchall.return_value = [] if "WHERE 1=0" in query.upper() else legacy

        cursor.execute = MagicMock(side_effect=_execute)
        conn_obj = _make_conn(cursor)
        mock_connect.return_value = conn_obj
        connector = GenericSqlConnector(config=config)
        await connector.connect()

        result = await connector.create_work_order(
            WorkOrder(id="WO-NEW", type=WorkOrderType.CORRECTIVE, asset_id="P-001")
        )

        assert result.id == "WO-NEW"
        executed = [str(call.args[0]).upper() for call in cursor.execute.call_args_list]
        assert any(q.startswith("INSERT") for q in executed)
        conn_obj.commit.assert_called()

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_non_transient_write_error_is_a_connector_error(
        self, mock_connect: MagicMock
    ) -> None:
        from machina.exceptions import ConnectorError

        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)

        def _execute(query: str, params: Any = None) -> None:
            if query.upper().startswith("INSERT"):
                raise ValueError("value too long for column WO_DESC")
            cursor.description = _WO_COLS if "WORK_ORDERS" in query.upper() else _ALL_COLS
            cursor.fetchall.return_value = []

        cursor.execute = MagicMock(side_effect=_execute)
        mock_connect.return_value = _make_conn(cursor)
        connector = GenericSqlConnector(
            config=_basic_config(capabilities="read_write", with_insert=True)
        )
        await connector.connect()

        with pytest.raises(ConnectorError, match="SQL write failed"):
            await connector.create_work_order(
                WorkOrder(id="WO-1", type=WorkOrderType.CORRECTIVE, asset_id="P-001")
            )

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


class TestQueryTimeout:
    """connect() hands query_timeout to the driver, which applies it per statement."""

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_odbc_connection_gets_it(self, mock_connect: MagicMock) -> None:
        mock_connect.return_value = _make_conn(_make_smart_cursor())
        await GenericSqlConnector(config=_basic_config(query_timeout=30)).connect()
        assert mock_connect.call_args.kwargs["query_timeout"] == 30

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_unset_leaves_the_driver_setting(self, mock_connect: MagicMock) -> None:
        mock_connect.return_value = _make_conn(_make_smart_cursor())
        await GenericSqlConnector(config=_basic_config()).connect()
        assert mock_connect.call_args.kwargs["query_timeout"] is None

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_jdbc")
    async def test_jdbc_connection_gets_it(self, mock_connect: MagicMock) -> None:
        mock_connect.return_value = _make_conn(_make_smart_cursor())
        config = _basic_config(
            driver_type="jdbc",
            jdbc_driver_class="com.ibm.as400.access.AS400JDBCDriver",
            query_timeout=45,
        )
        await GenericSqlConnector(config=config).connect()
        assert mock_connect.call_args.kwargs["query_timeout"] == 45


class TestStatementTimeout:
    """A statement stopped by its timeout raises ConnectorTimeoutError, unretried."""

    @staticmethod
    async def _timing_out_reader(mock_connect: MagicMock) -> tuple[GenericSqlConnector, MagicMock]:
        cursor = _make_smart_cursor()
        answer = cursor.execute.side_effect

        def _execute(query: str, params: Any = None) -> None:
            if "WHERE 1=0" not in query.upper():
                raise _FakeOdbcError(*_SQLSERVER_TIMEOUT)
            answer(query, params)

        cursor.execute = MagicMock(side_effect=_execute)
        mock_connect.return_value = _make_conn(cursor)
        config = _basic_config(query_timeout=5)
        config.retry.base_backoff = 0.01
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        cursor.execute.reset_mock()
        return connector, cursor

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic._is_transient", return_value=True)
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_timed_out_read_is_not_retried(
        self, mock_connect: MagicMock, _transient: MagicMock
    ) -> None:
        """Not even when the error also reads as transient."""
        connector, cursor = await self._timing_out_reader(mock_connect)

        with pytest.raises(ConnectorTimeoutError, match="Query timeout expired"):
            await connector.read_assets()

        # A retry would likely wait on the same lock, holding the connection again.
        assert cursor.execute.call_count == 1

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_timeout_is_logged(self, mock_connect: MagicMock) -> None:
        """Callers such as the MCP tools return the error without logging it."""
        connector, _cursor = await self._timing_out_reader(mock_connect)

        with capture_logs() as logs, pytest.raises(ConnectorTimeoutError):
            await connector.read_assets()

        [event] = [e for e in logs if e["event"] == "sql_statement_timeout"]
        assert event["log_level"] == "warning"
        assert event["operation"] == "read"
        assert event["query_timeout"] == 5

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic._is_transient", return_value=True)
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_timed_out_insert_is_rolled_back_and_not_retried(
        self, mock_connect: MagicMock, _transient: MagicMock
    ) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)

        def _execute(query: str, params: Any = None) -> None:
            if query.upper().startswith("INSERT"):
                raise _FakeOdbcError(*_SQLSERVER_TIMEOUT)
            cursor.description = _WO_COLS if "WORK_ORDERS" in query.upper() else _ALL_COLS
            cursor.fetchall.return_value = []

        cursor.execute = MagicMock(side_effect=_execute)
        conn_obj = _make_conn(cursor)
        mock_connect.return_value = conn_obj
        config = _basic_config(capabilities="read_write", with_insert=True)
        config.retry.base_backoff = 0.01
        connector = GenericSqlConnector(config=config)
        await connector.connect()

        with pytest.raises(ConnectorTimeoutError, match="SQL write timed out"):
            await connector.create_work_order(
                WorkOrder(id="WO-1", type=WorkOrderType.CORRECTIVE, asset_id="P-001")
            )

        executed = [str(call.args[0]).upper() for call in cursor.execute.call_args_list]
        assert len([q for q in executed if q.startswith("INSERT")]) == 1
        conn_obj.rollback.assert_called_once()
        conn_obj.commit.assert_not_called()

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_create_retried_after_a_commit_timeout_inserts_once(
        self, mock_connect: MagicMock
    ) -> None:
        """The timeout can come after the row reached the database (here, in commit).

        The connector does not re-send the INSERT itself; the caller's retry goes
        through create_work_order's ID check and finds the row.
        """
        stored: list[tuple[Any, ...]] = []
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)

        def _execute(query: str, params: Any = None) -> None:
            if query.upper().startswith("INSERT"):
                stored.append(tuple(params))
                return
            cursor.description = _WO_COLS if "WORK_ORDERS" in query.upper() else _ALL_COLS
            cursor.fetchall.return_value = [] if "WHERE 1=0" in query.upper() else list(stored)

        cursor.execute = MagicMock(side_effect=_execute)
        conn_obj = _make_conn(cursor)
        conn_obj.commit.side_effect = _FakeOdbcError(
            "HYT01",
            "[HYT01] [Microsoft][ODBC Driver 18 for SQL Server]Connection timeout expired "
            "(0) (SQLEndTran)",
        )
        mock_connect.return_value = conn_obj
        config = _basic_config(capabilities="read_write", with_insert=True)
        config.retry.base_backoff = 0.01
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        wo = WorkOrder(id="WO-7", type=WorkOrderType.CORRECTIVE, asset_id="P-001")

        with pytest.raises(ConnectorTimeoutError):
            await connector.create_work_order(wo)
        result = await connector.create_work_order(wo)

        assert result.id == "WO-7"
        assert len(stored) == 1


class TestBusyConnection:
    """health_check and disconnect wait only so long for a call using the connection."""

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_health_check_reports_a_busy_connection(self, mock_connect: MagicMock) -> None:
        started, release = threading.Event(), threading.Event()
        mock_connect.return_value = _make_stuck_conn(started, release, stuck_on="SELECT * FROM")
        connector = GenericSqlConnector(config=_basic_config(query_timeout=1))
        await connector.connect()
        stuck = asyncio.create_task(connector.read_work_orders())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            health = await asyncio.wait_for(connector.health_check(), timeout=5)
        finally:
            release.set()
            await stuck

        assert health.status == ConnectorStatus.UNHEALTHY
        assert "busy" in health.message

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_without_query_timeout_the_wait_is_still_bounded(
        self, mock_connect: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("machina.connectors.sql.generic._FALLBACK_CONNECTION_WAIT", 0.5)
        started, release = threading.Event(), threading.Event()
        mock_connect.return_value = _make_stuck_conn(started, release, stuck_on="SELECT * FROM")
        connector = GenericSqlConnector(config=_basic_config())
        await connector.connect()
        stuck = asyncio.create_task(connector.read_work_orders())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            health = await asyncio.wait_for(connector.health_check(), timeout=5)
        finally:
            release.set()
            await stuck

        assert "busy" in health.message

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_disconnect_leaves_a_busy_connection_to_the_call_using_it(
        self, mock_connect: MagicMock
    ) -> None:
        """Closing it under the running statement is unsafe; the call closes it."""
        started, release = threading.Event(), threading.Event()
        conn_obj = _make_stuck_conn(started, release, stuck_on="SELECT * FROM")
        mock_connect.return_value = conn_obj
        connector = GenericSqlConnector(config=_basic_config(query_timeout=1))
        await connector.connect()
        stuck = asyncio.create_task(connector.read_work_orders())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            with capture_logs() as logs:
                await asyncio.wait_for(connector.disconnect(), timeout=5)
            conn_obj.close.assert_not_called()
            assert (await connector.health_check()).message == "Not connected"
        finally:
            release.set()
            await stuck

        conn_obj.close.assert_called_once()
        [event] = [e for e in logs if e["event"] == "sql_disconnect_busy"]
        assert event["log_level"] == "warning"
        assert event["operation"] == "disconnect"

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_insert_running_at_disconnect_still_commits(
        self, mock_connect: MagicMock
    ) -> None:
        started, release = threading.Event(), threading.Event()
        conn_obj = _make_stuck_conn(started, release, stuck_on="INSERT")
        mock_connect.return_value = conn_obj
        connector = GenericSqlConnector(
            config=_basic_config(capabilities="read_write", with_insert=True, query_timeout=1)
        )
        await connector.connect()
        create = asyncio.create_task(
            connector.create_work_order(
                WorkOrder(id="WO-5", type=WorkOrderType.CORRECTIVE, asset_id="P-001")
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await asyncio.wait_for(connector.disconnect(), timeout=5)
        finally:
            release.set()
            created = await create

        assert created.id == "WO-5"
        conn_obj.commit.assert_called_once()
        conn_obj.close.assert_called_once()
