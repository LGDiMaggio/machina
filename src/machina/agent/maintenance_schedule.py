"""The ``get_maintenance_schedule`` agent tool.

Lists preventive-maintenance plans from the first connector that declares
:attr:`~machina.connectors.capabilities.Capability.READ_MAINTENANCE_PLANS` —
the same first-provider rule the other CMMS read tools follow — with each
plan's recurrence, tasks, effort and required skills.

Due dates are deliberately **not** computed. No connector reports when a
plan was last executed; without that, a calendar projection
(:meth:`~machina.domain.services.maintenance_scheduler.MaintenanceScheduler.scan_due_plans`)
can only assume the last run was exactly one interval ago — which reports
every active plan as due today. The tool says so instead of inventing dates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from machina.agent.prompts import safe_text
from machina.connectors.capabilities import Capability
from machina.exceptions import ConnectorError

if TYPE_CHECKING:
    from machina.connectors.base import ConnectorRegistry
    from machina.domain.maintenance_plan import MaintenancePlan

logger = structlog.get_logger(__name__)

DUE_DATES_NOTE = (
    "Due dates are not computed: the CMMS reports each plan's recurrence but "
    "not when it was last executed."
)


def _plan_summary(plan: MaintenancePlan) -> dict[str, Any]:
    """Serialize one plan for the LLM (recurrence, work content, effort)."""
    return {
        "plan_id": plan.id,
        "plan_name": plan.name,
        "asset_id": plan.asset_id,
        "active": plan.active,
        "interval_days": plan.interval.total_days or None,
        "interval_operating_hours": plan.interval.hours or None,
        "tasks": list(plan.tasks),
        "estimated_duration_hours": plan.estimated_duration_hours,
        "required_skills": list(plan.required_skills),
    }


async def get_maintenance_schedule(
    registry: ConnectorRegistry,
    *,
    asset_id: str = "",
) -> dict[str, Any]:
    """Return the preventive-maintenance plans for an asset or the whole plant.

    Args:
        registry: The agent's connector registry.
        asset_id: Keep only the plans for this asset (all plans when empty).

    Returns:
        ``{"plans": [...], "due_dates": <note>}`` plus a ``note`` when no plan
        matches, or ``{"error": ...}`` when no connector provides plans or the
        read fails.
    """
    providers = registry.find_by_capability(Capability.READ_MAINTENANCE_PLANS)
    if not providers:
        return {"error": "No connector provides maintenance plans"}
    connector_name, connector = providers[0]
    try:
        plans: list[MaintenancePlan] = await connector.read_maintenance_plans()  # type: ignore[attr-defined]
    except ConnectorError as exc:
        logger.warning(
            "maintenance_plans_read_failed",
            connector=connector_name,
            asset_id=asset_id,
            operation="get_maintenance_schedule",
            error=str(exc),
        )
        return {"error": safe_text(str(exc))}

    if asset_id:
        plans = [plan for plan in plans if plan.asset_id == asset_id]
    result: dict[str, Any] = {
        "plans": [_plan_summary(plan) for plan in plans],
        "due_dates": DUE_DATES_NOTE,
    }
    if not plans:
        result["note"] = (
            f"No maintenance plans found for asset {asset_id!r}."
            if asset_id
            else "The CMMS returned no maintenance plans."
        )
    return result
