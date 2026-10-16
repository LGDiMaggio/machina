"""Integration test for GenericSqlConnector with real SQL Server.

Requires Docker and pyodbc with ODBC Driver 17/18 for SQL Server.
Skipped automatically when Docker or the driver is not available.
"""

from __future__ import annotations

import pytest

# Skip entire module if testcontainers or pyodbc not available
pytest.importorskip("testcontainers", reason="testcontainers not installed")
pytest.importorskip("pyodbc", reason="pyodbc not installed")

pytestmark = pytest.mark.sqlserver


@pytest.fixture(scope="module")
def mssql_dsn():
    """Start a SQL Server container and return a DSN."""
    try:
        from testcontainers.mssql import SqlServerContainer  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("testcontainers[mssql] not installed")

    try:
        with SqlServerContainer("mcr.microsoft.com/mssql/server:2022-latest") as mssql:
            import pyodbc  # type: ignore[import-untyped]

            dsn = (
                f"Driver={{ODBC Driver 18 for SQL Server}};"
                f"Server=localhost,{mssql.get_exposed_port(1433)};"
                f"UID=sa;PWD={mssql.SQLSERVER_PASSWORD};"
                f"TrustServerCertificate=yes;"
            )

            # Create test schema
            conn = pyodbc.connect(dsn, autocommit=True)
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE test_assets ("
                "  asset_id VARCHAR(50) PRIMARY KEY,"
                "  asset_name NVARCHAR(200),"
                "  asset_cat VARCHAR(10),"
                "  criticality CHAR(1)"
                ")"
            )
            cursor.execute(
                "CREATE TABLE test_work_orders ("
                "  wo_id VARCHAR(50) PRIMARY KEY,"
                "  asset_id VARCHAR(50),"
                "  description NVARCHAR(500)"
                ")"
            )
            cursor.execute(
                "INSERT INTO test_assets VALUES "
                "('P-001', 'Pompa centrifuga', 'POM', 'A'),"
                "('C-001', 'Compressore aria', 'POM', 'B')"
            )
            conn.commit()
            cursor.close()
            conn.close()

            yield dsn
    except Exception as exc:
        pytest.skip(f"Could not start SQL Server container: {exc}")


class TestSqlServerRoundTrip:
    @pytest.mark.asyncio
    async def test_read_assets(self, mssql_dsn: str) -> None:
        from machina.connectors.sql.generic import GenericSqlConnector
        from machina.connectors.sql.schema import (
            FieldMapping,
            SqlConnectorConfig,
            TableMapping,
        )

        config = SqlConnectorConfig(
            dsn=mssql_dsn,
            tables={
                "assets": TableMapping(
                    query="SELECT asset_id, asset_name, asset_cat, criticality FROM test_assets",
                    entity="Asset",
                    fields={
                        "id": FieldMapping(column="asset_id"),
                        "name": FieldMapping(column="asset_name"),
                        "type": FieldMapping(
                            column="asset_cat",
                            enum_map={"POM": "rotating_equipment"},
                        ),
                        "criticality": FieldMapping(
                            column="criticality",
                            enum_map={"A": "A", "B": "B"},
                        ),
                    },
                ),
            },
        )
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        try:
            assets = await connector.read_assets()
            assert len(assets) == 2
            assert assets[0].id == "P-001"
        finally:
            await connector.disconnect()

    @pytest.mark.asyncio
    async def test_write_then_read(self, mssql_dsn: str) -> None:
        from machina.connectors.sql.generic import GenericSqlConnector
        from machina.connectors.sql.schema import (
            FieldMapping,
            SqlConnectorConfig,
            TableMapping,
        )
        from machina.domain.work_order import WorkOrder, WorkOrderType

        config = SqlConnectorConfig(
            dsn=mssql_dsn,
            capabilities="read_write",
            tables={
                "work_orders": TableMapping(
                    query="SELECT wo_id, asset_id, description FROM test_work_orders",
                    entity="WorkOrder",
                    fields={
                        "id": FieldMapping(column="wo_id"),
                        "asset_id": FieldMapping(column="asset_id"),
                        "description": FieldMapping(column="description"),
                    },
                    insert_table="test_work_orders",
                    insert_columns={
                        "id": "wo_id",
                        "asset_id": "asset_id",
                        "description": "description",
                    },
                ),
            },
        )
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        try:
            wo = WorkOrder(
                id="WO-TEST-001",
                type=WorkOrderType.CORRECTIVE,
                asset_id="P-001",
                description="Integration test work order",
            )
            await connector.create_work_order(wo)
            wos = await connector.read_work_orders()
            assert len(wos) == 1
            assert wos[0].id == "WO-TEST-001"
            assert wos[0].description == "Integration test work order"
        finally:
            await connector.disconnect()


class TestSqlServerStatementTimeout:
    @pytest.mark.asyncio
    async def test_read_waiting_on_a_row_lock_times_out(self, mssql_dsn: str) -> None:
        """SQL Server waits for locks indefinitely by default; query_timeout bounds it."""
        import asyncio

        import pyodbc  # type: ignore[import-untyped]

        from machina.connectors.sql.generic import GenericSqlConnector
        from machina.connectors.sql.schema import (
            FieldMapping,
            SqlConnectorConfig,
            TableMapping,
        )
        from machina.exceptions import ConnectorTimeoutError

        config = SqlConnectorConfig(
            dsn=mssql_dsn,
            query_timeout=1,
            tables={
                "assets": TableMapping(
                    query="SELECT asset_id, asset_name FROM test_assets",
                    entity="Asset",
                    fields={
                        "id": FieldMapping(column="asset_id"),
                        "name": FieldMapping(column="asset_name"),
                    },
                ),
            },
        )
        connector = GenericSqlConnector(config=config)
        await connector.connect()
        blocker = None
        try:
            # Another application holds a row lock: an UPDATE it has not committed.
            blocker = pyodbc.connect(mssql_dsn, autocommit=False)
            blocker.cursor().execute(
                "UPDATE test_assets SET asset_name = 'locked' WHERE asset_id = 'P-001'"
            )
            with pytest.raises(ConnectorTimeoutError, match="HYT00"):
                # wait_for: without the timeout the read would wait forever.
                await asyncio.wait_for(connector.read_assets(), timeout=30)
            # The timed-out read no longer holds the connection.
            assert (await connector.health_check()).status.value == "healthy"
        finally:
            if blocker is not None:
                blocker.rollback()
                blocker.close()
            await connector.disconnect()
