"""Pydantic models for the Generic SQL connector YAML schema.

The YAML schema tells GenericSqlConnector how to map database tables
and columns to Machina domain entity fields.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class FieldMapping(BaseModel):
    """Mapping from one database column to a domain entity field."""

    column: str = Field(..., description="Database column name")
    coerce: str | None = Field(
        default=None,
        description="Named coercer (e.g. 'strip_ebcdic', 'db2_date', 'decimal')",
    )
    enum_map: dict[str, str] | None = Field(
        default=None,
        description="Database value → domain enum value mapping",
    )
    default: Any = Field(default=None, description="Default when column is NULL")


class TableMapping(BaseModel):
    """Mapping from a database table/query to a domain entity type."""

    query: str = Field(..., description="SQL SELECT query for reading")
    entity: Literal["Asset", "WorkOrder"] = Field(
        ..., description="Target Machina domain entity type"
    )
    fields: dict[str, FieldMapping] = Field(
        ..., min_length=1, description="entity_field_name → FieldMapping"
    )
    insert_table: str | None = Field(
        default=None,
        description="Table name for INSERT operations (write path)",
    )
    insert_columns: dict[str, str] | None = Field(
        default=None,
        description="entity_field_name → column_name for INSERT",
    )


class SqlRetryConfig(BaseModel):
    """Retry settings for transient SQL errors."""

    max_retries: int = Field(default=3, ge=0, le=10)
    base_backoff: float = Field(default=0.5, ge=0.1, le=5.0)
    max_backoff: float = Field(default=8.0, ge=1.0, le=60.0)


class SqlConnectorConfig(BaseModel):
    """Top-level configuration for GenericSqlConnector."""

    dsn: str = Field(..., description="ODBC/JDBC connection string")
    driver_type: Literal["odbc", "jdbc"] = Field(default="odbc", description="Driver backend")
    jdbc_driver_class: str | None = Field(
        default=None,
        description="JDBC driver class name (required when driver_type='jdbc')",
    )
    jdbc_driver_path: str | None = Field(
        default=None,
        description="Path to JDBC .jar file",
    )
    capabilities: Literal["read_only", "read_write"] = Field(
        default="read_only",
        description="Exposed capability set",
    )
    tables: dict[str, TableMapping] = Field(
        ..., min_length=1, description="Named table mappings (e.g. 'assets', 'work_orders')"
    )
    ebcdic_codepage: str = Field(
        default="cp037",
        description="EBCDIC codepage for strip_ebcdic coercer",
    )
    retry: SqlRetryConfig = Field(default_factory=SqlRetryConfig)

    @model_validator(mode="after")
    def _jdbc_requires_driver_class(self) -> SqlConnectorConfig:
        if self.driver_type == "jdbc" and not self.jdbc_driver_class:
            msg = "jdbc_driver_class is required when driver_type is 'jdbc'"
            raise ValueError(msg)
        return self
