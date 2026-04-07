"""MaintenancePlan entity — scheduled maintenance strategy for an asset."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Interval(BaseModel):
    """A maintenance interval specification.

    At least one of the duration fields should be set.
    """

    days: int = Field(default=0, ge=0)
    weeks: int = Field(default=0, ge=0)
    months: int = Field(default=0, ge=0)
    hours: int = Field(default=0, ge=0, description="Operating hours interval")

    @property
    def total_days(self) -> int:
        """Approximate interval in calendar days (months ≈ 30 days)."""
        return self.days + self.weeks * 7 + self.months * 30


class MaintenancePlan(BaseModel):
    """A preventive maintenance plan defining recurring tasks for an asset.

    Links an asset to a set of inspection or service tasks performed
    at a fixed calendar or operating-hours interval.
    """

    id: str = Field(..., description="Plan identifier (e.g. 'MP-P201-QUARTERLY')")
    asset_id: str = Field(..., description="Target asset identifier")
    name: str = Field(..., description="Plan name")
    interval: Interval = Field(..., description="Recurrence interval")
    tasks: list[str] = Field(
        default_factory=list,
        description="Ordered list of task descriptions",
    )
    estimated_duration_hours: float | None = Field(
        default=None, ge=0, description="Estimated execution time"
    )
    required_skills: list[str] = Field(
        default_factory=list,
        description="Skills needed to execute the plan",
    )
    active: bool = Field(default=True, description="Whether the plan is active")

    model_config = {"frozen": False, "str_strip_whitespace": True}
