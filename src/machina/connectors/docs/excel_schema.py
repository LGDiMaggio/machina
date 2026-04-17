"""Pydantic models for Excel/CSV YAML schema validation.

The YAML schema tells the ExcelCsvConnector how to map spreadsheet
columns to Machina domain entity fields, including type coercion.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ColumnMapping(BaseModel):
    """Mapping from one spreadsheet column to a domain entity field."""

    column: str = Field(..., description="Spreadsheet column header name")
    field: str = Field(..., description="Target domain entity field name")
    type: Literal["str", "int", "float", "date", "datetime", "bool"] = Field(
        default="str", description="Expected Python type after coercion"
    )
    required: bool = Field(default=False, description="Row is invalid if this cell is empty")
    default: Any = Field(default=None, description="Default value when cell is empty")
    coerce: str | None = Field(
        default=None,
        description="Named coercer function (e.g. 'float_it', 'date_parse', 'italian_date')",
    )


class SheetSchema(BaseModel):
    """Schema for one worksheet or CSV file."""

    path: str = Field(..., description="File path (local, UNC, or SMB URL)")
    sheet: str = Field(default="Sheet1", description="Worksheet name (ignored for CSV)")
    columns: list[ColumnMapping] = Field(..., min_length=1)
    write_mode: Literal["append", "overwrite"] | None = Field(
        default=None,
        description="Write mode — None means read-only",
    )

    @model_validator(mode="after")
    def _check_unique_fields(self) -> SheetSchema:
        fields = [c.field for c in self.columns]
        dupes = [f for f in fields if fields.count(f) > 1]
        if dupes:
            msg = f"Duplicate field mappings: {sorted(set(dupes))}"
            raise ValueError(msg)
        return self


class WatcherConfig(BaseModel):
    """Configuration for the file-system watcher."""

    enabled: bool = Field(default=True)
    debounce_ms: int = Field(default=500, ge=100, le=10000)
    poll_fallback_sec: int = Field(default=30, ge=5, le=300)


class ExcelConnectorConfig(BaseModel):
    """Top-level configuration for the ExcelCsvConnector."""

    asset_registry: SheetSchema | None = Field(default=None)
    work_orders: SheetSchema | None = Field(default=None)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)

    @model_validator(mode="after")
    def _at_least_one_schema(self) -> ExcelConnectorConfig:
        if self.asset_registry is None and self.work_orders is None:
            msg = "At least one of 'asset_registry' or 'work_orders' must be configured"
            raise ValueError(msg)
        return self
