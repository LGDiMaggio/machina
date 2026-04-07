"""Alarm entity — a real-time sensor event with severity classification."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Severity(StrEnum):
    """Alarm severity level."""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Alarm(BaseModel):
    """A sensor alarm or event from an industrial data source.

    Represents a threshold exceedance or anomalous condition detected
    by the IoT / SCADA layer.

    Example:
        ```python
        from machina.domain.alarm import Alarm, Severity

        alarm = Alarm(
            id="ALM-001",
            asset_id="P-201",
            severity=Severity.WARNING,
            parameter="vibration",
            value=12.5,
            threshold=10.0,
            unit="mm/s",
        )
        ```
    """

    id: str = Field(..., description="Unique alarm identifier")
    asset_id: str = Field(..., description="Related asset identifier")
    severity: Severity = Field(..., description="Alarm severity")
    parameter: str = Field(..., description="Measured parameter name")
    value: float = Field(..., description="Measured value that triggered the alarm")
    threshold: float = Field(..., description="Threshold that was exceeded")
    unit: str = Field(default="", description="Engineering unit (e.g. 'mm/s', '°C')")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="When the alarm occurred"
    )
    source: str = Field(default="", description="Data source URI (e.g. OPC-UA node ID)")
    acknowledged: bool = Field(default=False, description="Whether alarm was acknowledged")

    model_config = {"frozen": False, "str_strip_whitespace": True}

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id cannot be empty")
        return v.strip()

    @property
    def is_above_threshold(self) -> bool:
        """Whether the measured value exceeds the threshold."""
        return self.value > self.threshold
