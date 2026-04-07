"""Machina — The open-source Python framework for AI agents in industrial maintenance."""

from machina.domain.alarm import Alarm, Severity
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.failure_mode import FailureMode
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.plant import Plant
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderStatus, WorkOrderType

__version__ = "0.0.1"

__all__ = [
    "Alarm",
    "Asset",
    "AssetType",
    "Criticality",
    "FailureMode",
    "Interval",
    "MaintenancePlan",
    "Plant",
    "Priority",
    "Severity",
    "SparePart",
    "WorkOrder",
    "WorkOrderStatus",
    "WorkOrderType",
    "__version__",
]
