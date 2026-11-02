"""Tests for GenericSqlConnector — mocked pyodbc, no real database."""

from __future__ import annotations

import asyncio
import threading
import time
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


class _OverlapDetectingConnection:
    """DB-API connection double that records use from two threads at once.

    The connection is in use from ``cursor()`` until that cursor is closed.
    ``execute`` lingers, so calls that are not serialized really do overlap;
    a query containing ``hold`` waits for ``release`` instead, so a test can
    act while that query is mid-flight on its worker thread.
    """

    def __init__(self, *, hold: str = "") -> None:
        self.hold = hold
        self.held = threading.Event()
        self.release = threading.Event()
        self.max_users = 0
        self.work_orders: list[tuple[Any, ...]] = []
        self._users = 0
        self._guard = threading.Lock()

    def cursor(self) -> _OverlapDetectingCursor:
        with self._guard:
            self._users += 1
            self.max_users = max(self.max_users, self._users)
        return _OverlapDetectingCursor(self)

    def cursor_closed(self) -> None:
        with self._guard:
            self._users -= 1

    def commit(self) -> None:
        pass


class _OverlapDetectingCursor:
    def __init__(self, conn: _OverlapDetectingConnection) -> None:
        self._conn = conn
        self.description: list[tuple[str]] = []
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, query: str, params: Any = None) -> None:
        q = query.upper()
        if self._conn.hold and self._conn.hold in q:
            self._conn.held.set()
            self._conn.release.wait(timeout=5)
        else:
            time.sleep(0.02)
        if q.startswith("INSERT"):
            self._conn.work_orders.append(tuple(params))
            return
        if "WORK_ORDERS" in q:
            self.description = _WO_COLS
            self._rows = list(self._conn.work_orders)
        else:
            self.description = _ASSET_COLS
            self._rows = [("P-001", "Pompa 1", "POM", "A")]
        if "WHERE 1=0" in q:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        self._conn.cursor_closed()


class TestSharedConnection:
    """One DB-API connection serves every caller, but never two threads at once.

    pyodbc and jaydebeapi declare ``threadsafety = 1``: threads may share the
    module, not a connection.
    """

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_concurrent_reads_take_turns(self, mock_connect: MagicMock) -> None:
        conn = _OverlapDetectingConnection()
        mock_connect.return_value = conn
        connector = GenericSqlConnector(config=_basic_config())
        await connector.connect()

        results = await asyncio.gather(*(connector.read_assets() for _ in range(4)))

        assert conn.max_users == 1
        assert [len(assets) for assets in results] == [1, 1, 1, 1]

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_a_read_waits_for_an_insert_in_flight(self, mock_connect: MagicMock) -> None:
        conn = _OverlapDetectingConnection(hold="INSERT")
        mock_connect.return_value = conn
        connector = GenericSqlConnector(
            config=_basic_config(capabilities="read_write", with_insert=True)
        )
        await connector.connect()
        wo = WorkOrder(id="WO-7", type=WorkOrderType.CORRECTIVE, asset_id="P-001")

        create = asyncio.create_task(connector.create_work_order(wo))
        assert await asyncio.to_thread(conn.held.wait, 5)  # the INSERT is running
        read = asyncio.create_task(connector.read_assets())
        await asyncio.sleep(0.05)
        conn.release.set()
        await create

        assert len(await read) == 1
        assert conn.max_users == 1
        assert len(conn.work_orders) == 1

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_a_health_check_waits_for_a_read_in_flight(
        self, mock_connect: MagicMock
    ) -> None:
        conn = _OverlapDetectingConnection()
        mock_connect.return_value = conn
        connector = GenericSqlConnector(config=_basic_config())
        await connector.connect()
        conn.hold = "FROM ASSETS"  # set after connect, which validates that query too

        read = asyncio.create_task(connector.read_assets())
        assert await asyncio.to_thread(conn.held.wait, 5)
        probe = asyncio.create_task(connector.health_check())
        await asyncio.sleep(0.05)
        assert not probe.done()
        conn.release.set()

        assert (await probe).status.value == "healthy"
        assert len(await read) == 1
        assert conn.max_users == 1

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_a_cancelled_create_holds_the_connection_until_its_insert_ends(
        self, mock_connect: MagicMock
    ) -> None:
        """Cancelling the caller cannot stop the INSERT's thread, so the retry
        waits for it and then finds the row instead of inserting it twice."""
        conn = _OverlapDetectingConnection(hold="INSERT")
        mock_connect.return_value = conn
        connector = GenericSqlConnector(
            config=_basic_config(capabilities="read_write", with_insert=True)
        )
        await connector.connect()
        wo = WorkOrder(id="WO-7", type=WorkOrderType.CORRECTIVE, asset_id="P-001")

        first = asyncio.create_task(connector.create_work_order(wo))
        assert await asyncio.to_thread(conn.held.wait, 5)
        first.cancel()  # an MCP request cancellation, a workflow step timeout
        with pytest.raises(asyncio.CancelledError):
            await first
        retry = asyncio.create_task(connector.create_work_order(wo))
        await asyncio.sleep(0.05)
        conn.release.set()
        await retry

        assert conn.max_users == 1
        assert len(conn.work_orders) == 1

    @pytest.mark.asyncio
    @patch("machina.connectors.sql.generic.connect_odbc")
    async def test_a_read_waits_for_schema_validation(self, mock_connect: MagicMock) -> None:
        conn = _OverlapDetectingConnection(hold="WHERE 1=0")
        mock_connect.return_value = conn
        connector = GenericSqlConnector(config=_basic_config())

        connecting = asyncio.create_task(connector.connect())
        assert await asyncio.to_thread(conn.held.wait, 5)  # validating the mappings
        read = asyncio.create_task(connector.read_assets())
        await asyncio.sleep(0.05)
        conn.release.set()
        await connecting

        assert len(await read) == 1
        assert conn.max_users == 1


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
