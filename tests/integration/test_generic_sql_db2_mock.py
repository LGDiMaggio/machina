"""Integration test for GenericSqlConnector — mocked DB2 via sqlite3.

Uses sqlite3 as a compatibility shim to exercise the full connector
pipeline (connect → validate → read → write → read) without requiring
a real DB2 or AS/400 instance.  The dialect coercion layer (EBCDIC,
DB2 century-dates) is tested via unit tests in test_dialect.py.

Coverage limit: this does NOT test real pyodbc or JDBC driver behavior.
Real AS/400 coverage requires an IBM i emulator (licensed) — see the
optional @pytest.mark.ibm_i tests below, skipped by default.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import patch

import pytest

from machina.connectors.sql.generic import GenericSqlConnector
from machina.connectors.sql.schema import (
    FieldMapping,
    SqlConnectorConfig,
    TableMapping,
)


def _create_test_db(db_path: str) -> sqlite3.Connection:
    """Create a test sqlite3 database with asset and work order tables."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        "CREATE TABLE ASSETS ("
        "  ASSET_ID TEXT PRIMARY KEY,"
        "  ASSET_NAME TEXT,"
        "  ASSET_CAT TEXT,"
        "  CRIT TEXT,"
        "  INSTALL_DATE TEXT"
        ")"
    )
    cursor.execute(
        "CREATE TABLE WORK_ORDERS ("
        "  WO_ID TEXT PRIMARY KEY,"
        "  ASSET_ID TEXT,"
        "  WO_DESC TEXT,"
        "  WO_TYPE TEXT,"
        "  PRIORITY TEXT"
        ")"
    )
    cursor.executemany(
        "INSERT INTO ASSETS VALUES (?, ?, ?, ?, ?)",
        [
            ("P-001", "Pompa centrifuga", "POM", "A", "2019-03-15"),
            ("C-001", "Compressore aria", "POM", "B", "2018-01-01"),
            ("V-001", "Valvola sicurezza", "VAL", "A", "2016-12-01"),
        ],
    )
    conn.commit()
    return conn


def _make_config(db_path: str, *, read_write: bool = False) -> SqlConnectorConfig:
    return SqlConnectorConfig(
        dsn=f"sqlite:///{db_path}",
        capabilities="read_write" if read_write else "read_only",
        tables={
            "assets": TableMapping(
                query="SELECT ASSET_ID, ASSET_NAME, ASSET_CAT, CRIT, INSTALL_DATE FROM ASSETS",
                entity="Asset",
                fields={
                    "id": FieldMapping(column="ASSET_ID"),
                    "name": FieldMapping(column="ASSET_NAME"),
                    "type": FieldMapping(
                        column="ASSET_CAT",
                        enum_map={"POM": "rotating_equipment", "VAL": "safety"},
                    ),
                    "criticality": FieldMapping(
                        column="CRIT",
                        enum_map={"A": "A", "B": "B", "C": "C"},
                    ),
                    "install_date": FieldMapping(column="INSTALL_DATE", coerce="iso_date"),
                },
            ),
            "work_orders": TableMapping(
                query="SELECT WO_ID, ASSET_ID, WO_DESC FROM WORK_ORDERS",
                entity="WorkOrder",
                fields={
                    "id": FieldMapping(column="WO_ID"),
                    "asset_id": FieldMapping(column="ASSET_ID"),
                    "description": FieldMapping(column="WO_DESC"),
                },
                insert_table="WORK_ORDERS" if read_write else None,
                insert_columns={
                    "id": "WO_ID",
                    "asset_id": "ASSET_ID",
                    "description": "WO_DESC",
                }
                if read_write
                else None,
            ),
        },
    )


class TestDb2MockRoundTrip:
    @pytest.mark.asyncio
    async def test_read_assets(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "test.db")
        db_conn = _create_test_db(db_path)

        config = _make_config(db_path)
        connector = GenericSqlConnector(config=config)

        with patch("machina.connectors.sql.generic.connect_odbc", return_value=db_conn):
            await connector.connect()
            assets = await connector.read_assets()

        assert len(assets) == 3
        assert assets[0].id == "P-001"
        assert assets[0].name == "Pompa centrifuga"
        assert assets[0].type.value == "rotating_equipment"
        assert assets[0].criticality.value == "A"

    @pytest.mark.asyncio
    async def test_read_work_orders_empty(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "test.db")
        db_conn = _create_test_db(db_path)

        config = _make_config(db_path)
        connector = GenericSqlConnector(config=config)

        with patch("machina.connectors.sql.generic.connect_odbc", return_value=db_conn):
            await connector.connect()
            wos = await connector.read_work_orders()

        assert wos == []

    @pytest.mark.asyncio
    async def test_write_then_read(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "test.db")
        db_conn = _create_test_db(db_path)

        config = _make_config(db_path, read_write=True)
        connector = GenericSqlConnector(config=config)

        with patch("machina.connectors.sql.generic.connect_odbc", return_value=db_conn):
            await connector.connect()

            from machina.domain.work_order import WorkOrder, WorkOrderType

            wo = WorkOrder(
                id="WO-2026-001",
                type=WorkOrderType.CORRECTIVE,
                asset_id="P-001",
                description="Sostituzione guarnizione pompa",
            )
            await connector.create_work_order(wo)

            wos = await connector.read_work_orders()
            assert len(wos) == 1
            assert wos[0].id == "WO-2026-001"
            assert wos[0].asset_id == "P-001"
            assert wos[0].description == "Sostituzione guarnizione pompa"

    @pytest.mark.asyncio
    async def test_health_check(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "test.db")
        db_conn = _create_test_db(db_path)

        config = _make_config(db_path)
        connector = GenericSqlConnector(config=config)

        with patch("machina.connectors.sql.generic.connect_odbc", return_value=db_conn):
            await connector.connect()
            health = await connector.health_check()

        assert health.status.value == "healthy"


@pytest.mark.ibm_i
class TestIbmIReal:
    """Manual-only tests for real AS/400 connections.

    Skipped by default. Run with: pytest -m ibm_i --ibm-i-dsn="..."
    Requires a licensed IBM i Access ODBC driver and a reachable AS/400.
    """

    @pytest.mark.asyncio
    async def test_read_assets_real(self) -> None:
        pytest.skip("IBM i integration test — requires real AS/400 connection")
