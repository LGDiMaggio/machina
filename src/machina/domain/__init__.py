"""Domain model entities for industrial maintenance."""

from machina.domain.alarm import Alarm, Severity
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.calendar import CalendarEvent, EventType, PlannedDowntime, ShiftPattern
from machina.domain.failure_mode import FailureMode
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.plant import Plant
from machina.domain.spare_part import SparePart
from machina.domain.work_order import (
    FailureImpact,
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)

__all__ = [
    "Alarm",
    "Asset",
    "AssetType",
    "CalendarEvent",
    "Criticality",
    "EventType",
    "FailureImpact",
    "FailureMode",
    "Interval",
    "MaintenancePlan",
    "PlannedDowntime",
    "Plant",
    "Priority",
    "Severity",
    "ShiftPattern",
    "SparePart",
    "WorkOrder",
    "WorkOrderStatus",
    "WorkOrderType",
]
