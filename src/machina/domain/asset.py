"""Asset entity — physical equipment in a maintenance hierarchy.

Assets form a hierarchy: Plant → Area → System → Equipment → Component.
Each asset has a criticality classification and can be associated with
known failure modes.
"""

from __future__ import annotations

from datetime import date  # noqa: TC003
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AssetType(StrEnum):
    """Equipment classification based on ISO 14224 categories."""

    ROTATING_EQUIPMENT = "rotating_equipment"
    STATIC_EQUIPMENT = "static_equipment"
    INSTRUMENT = "instrument"
    ELECTRICAL = "electrical"
    PIPING = "piping"
    STRUCTURAL = "structural"
    HVAC = "hvac"
    SAFETY = "safety"


class Criticality(StrEnum):
    """Asset criticality classification for maintenance prioritisation.

    A = Critical (production-stopping), B = Important, C = Standard.
    """

    A = "A"
    B = "B"
    C = "C"


class Asset(BaseModel):
    """A physical piece of equipment in the maintenance hierarchy.

    Assets represent any maintainable item — from an entire plant down to
    an individual bearing.  They carry metadata for identification, location,
    and maintenance strategy.
    """

    id: str = Field(..., description="Unique asset identifier (e.g. 'P-201')")
    name: str = Field(..., description="Human-readable asset name")
    type: AssetType = Field(..., description="Equipment classification")
    location: str = Field(default="", description="Physical location path")
    manufacturer: str = Field(default="", description="Equipment manufacturer")
    model: str = Field(default="", description="Manufacturer model number")
    serial_number: str = Field(default="", description="Serial number")
    install_date: date | None = Field(default=None, description="Installation date")
    criticality: Criticality = Field(
        default=Criticality.C, description="Criticality classification"
    )
    parent: str | None = Field(default=None, description="Parent asset ID")
    children: list[str] = Field(default_factory=list, description="Child asset IDs")
    failure_modes: list[str] = Field(
        default_factory=list,
        description="Associated failure mode codes",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary key-value metadata",
    )

    model_config = {"frozen": False, "str_strip_whitespace": True}

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id cannot be empty")
        return v.strip()
