"""Machina — The open-source Python framework for AI agents in industrial maintenance."""

from machina.agent.runtime import Agent
from machina.domain.alarm import Alarm, Severity
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.failure_mode import FailureMode
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.plant import Plant
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderStatus, WorkOrderType
from machina.workflows import Step, Workflow, WorkflowEngine

__version__ = "0.2.0"

__all__ = [
    "Agent",
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
    "Step",
    "WorkOrder",
    "WorkOrderStatus",
    "WorkOrderType",
    "Workflow",
    "WorkflowEngine",
    "__version__",
]
