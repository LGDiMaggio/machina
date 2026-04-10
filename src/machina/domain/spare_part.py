"""SparePart entity — a replaceable component with inventory tracking."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class SparePart(BaseModel):
    """A spare part with inventory and reorder tracking.

    Links to compatible assets for automatic part lookup during
    work order creation.
    """

    sku: str = Field(..., description="Stock Keeping Unit identifier")
    name: str = Field(..., description="Part description")
    manufacturer: str = Field(default="", description="Part manufacturer")
    compatible_assets: list[str] = Field(
        default_factory=list,
        description="Asset IDs this part is compatible with",
    )
    stock_quantity: int = Field(default=0, ge=0, description="Current stock level")
    reorder_point: int = Field(default=0, ge=0, description="Stock level that triggers reorder")
    lead_time_days: int = Field(default=0, ge=0, description="Supplier lead time in days")
    unit_cost: float = Field(default=0.0, ge=0, description="Cost per unit")
    warehouse_location: str = Field(default="", description="Physical storage location")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Connector-specific fields preserved verbatim from the source CMMS",
    )

    model_config = {"frozen": False, "str_strip_whitespace": True}

    @field_validator("sku")
    @classmethod
    def _validate_sku(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("sku cannot be empty")
        return v.strip()

    @property
    def needs_reorder(self) -> bool:
        """Whether current stock is at or below the reorder point."""
        return self.stock_quantity <= self.reorder_point
