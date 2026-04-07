"""FailureMode entity — a specific way an asset can fail.

Failure modes follow the ISO 14224 taxonomy and carry detection
methods, typical indicators, and recommended corrective actions.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class FailureMode(BaseModel):
    """A known failure mode for a class of equipment.

    Encodes domain knowledge about *how* equipment fails, *how* to
    detect the failure, and *what* to do about it.
    """

    code: str = Field(..., description="Unique failure mode code (e.g. 'BEAR-WEAR-01')")
    name: str = Field(..., description="Human-readable failure mode name")
    mechanism: str = Field(
        default="", description="Failure mechanism (e.g. 'fatigue', 'corrosion')"
    )
    category: str = Field(default="", description="Category (e.g. 'mechanical', 'electrical')")
    detection_methods: list[str] = Field(
        default_factory=list,
        description="Methods to detect this failure (e.g. 'vibration_analysis')",
    )
    typical_indicators: list[str] = Field(
        default_factory=list,
        description="Observable symptoms when this failure is developing",
    )
    recommended_actions: list[str] = Field(
        default_factory=list,
        description="Corrective actions to resolve this failure",
    )
    mtbf_hours: float | None = Field(
        default=None,
        ge=0,
        description="Mean Time Between Failures in hours",
    )

    model_config = {"frozen": False, "str_strip_whitespace": True}

    @field_validator("code")
    @classmethod
    def _validate_code(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("code cannot be empty")
        return v.strip()
