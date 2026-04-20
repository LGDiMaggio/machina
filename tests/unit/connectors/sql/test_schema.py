"""Tests for sql/schema.py — YAML table-to-entity mapping models."""

import pytest

from machina.connectors.sql.schema import (
    FieldMapping,
    SqlConnectorConfig,
    SqlRetryConfig,
    TableMapping,
)


class TestFieldMapping:
    def test_defaults(self) -> None:
        m = FieldMapping(column="COD_MAC")
        assert m.coerce is None
        assert m.enum_map is None
        assert m.default is None

    def test_with_coerce_and_enum(self) -> None:
        m = FieldMapping(
            column="CAT_MAC",
            coerce="strip_ebcdic",
            enum_map={"POM": "rotating_equipment"},
        )
        assert m.coerce == "strip_ebcdic"
        assert m.enum_map == {"POM": "rotating_equipment"}


class TestTableMapping:
    def test_valid(self) -> None:
        t = TableMapping(
            query="SELECT * FROM MAINT.ANAG_MAC",
            entity="Asset",
            fields={"id": FieldMapping(column="COD_MAC")},
        )
        assert t.entity == "Asset"

    def test_empty_fields_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            TableMapping(query="SELECT 1", entity="Asset", fields={})

    def test_insert_config(self) -> None:
        t = TableMapping(
            query="SELECT * FROM WO",
            entity="WorkOrder",
            fields={"id": FieldMapping(column="WO_ID")},
            insert_table="WO",
            insert_columns={"id": "WO_ID", "asset_id": "ASSET_ID"},
        )
        assert t.insert_table == "WO"


class TestSqlRetryConfig:
    def test_defaults(self) -> None:
        r = SqlRetryConfig()
        assert r.max_retries == 3
        assert r.base_backoff == 0.5

    def test_bounds(self) -> None:
        with pytest.raises(ValueError):
            SqlRetryConfig(max_retries=-1)


class TestSqlConnectorConfig:
    def test_valid_odbc(self) -> None:
        cfg = SqlConnectorConfig(
            dsn="Driver={ODBC Driver 18};Server=localhost;",
            tables={
                "assets": TableMapping(
                    query="SELECT * FROM ASSETS",
                    entity="Asset",
                    fields={"id": FieldMapping(column="ASSET_ID")},
                )
            },
        )
        assert cfg.driver_type == "odbc"
        assert cfg.capabilities == "read_only"

    def test_jdbc_requires_driver_class(self) -> None:
        with pytest.raises(ValueError, match="jdbc_driver_class"):
            SqlConnectorConfig(
                dsn="jdbc:db2://host:50000/db",
                driver_type="jdbc",
                tables={
                    "assets": TableMapping(
                        query="SELECT 1",
                        entity="Asset",
                        fields={"id": FieldMapping(column="ID")},
                    )
                },
            )

    def test_jdbc_valid(self) -> None:
        cfg = SqlConnectorConfig(
            dsn="jdbc:db2://host:50000/db",
            driver_type="jdbc",
            jdbc_driver_class="com.ibm.db2.jcc.DB2Driver",
            tables={
                "assets": TableMapping(
                    query="SELECT 1",
                    entity="Asset",
                    fields={"id": FieldMapping(column="ID")},
                )
            },
        )
        assert cfg.jdbc_driver_class == "com.ibm.db2.jcc.DB2Driver"

    def test_empty_tables_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            SqlConnectorConfig(dsn="test", tables={})

    def test_read_write_capabilities(self) -> None:
        cfg = SqlConnectorConfig(
            dsn="test",
            capabilities="read_write",
            tables={
                "wo": TableMapping(
                    query="SELECT 1",
                    entity="WorkOrder",
                    fields={"id": FieldMapping(column="ID")},
                )
            },
        )
        assert cfg.capabilities == "read_write"
