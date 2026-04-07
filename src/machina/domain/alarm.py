"""Alarm entity — a real-time sensor event with severity classification."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """Alarm severity level."""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Alarm(BaseModel):
    """A sensor alarm or event from an industrial data source.

    Represents a threshold exceedance or anomalous condition detected
    by the IoT / SCADA layer.
    """

    id: str = Field(..., description="Unique alarm identifier")
    asset_id: str = Field(..., description="Related asset identifier")
    severity: Severity = Field(..., description="Alarm severity")
    parameter: str = Field(..., description="Measured parameter name")
    value: float = Field(..., description="Measured value that triggered the alarm")
    threshold: float = Field(..., description="Threshold that was exceeded")
    unit: str = Field(default="", description="Engineering unit (e.g. 'mm/s', '°C')")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="When the alarm occurred"
    )
    source: str = Field(default="", description="Data source URI (e.g. OPC-UA node ID)")
    acknowledged: bool = Field(default=False, description="Whether alarm was acknowledged")

    model_config = {"frozen": False, "str_strip_whitespace": True}

    @property
    def is_above_threshold(self) -> bool:
        """Whether the measured value exceeds the threshold."""
        return self.value > self.threshold
