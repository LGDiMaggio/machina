"""MaintenanceScheduler — calculate upcoming maintenance windows.

Determines when the next preventive maintenance is due for assets
based on their maintenance plans and last execution dates.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from machina.domain.maintenance_plan import MaintenancePlan  # noqa: TC001
from machina.exceptions import DomainValidationError

NO_CALENDAR_DUE_DATE_REASON = (
    "No calendar interval (operating-hours, meter-triggered or unknown "
    "recurrence): a calendar due date cannot be computed for this plan."
)


def _has_calendar_interval(plan: MaintenancePlan) -> bool:
    """Whether the plan recurs on the calendar, so a due date can be computed."""
    return plan.interval.total_days > 0


class MaintenanceScheduler:
    """Service for computing maintenance due dates.

    This is a calendar-based baseline; future versions will account
    for operating hours and condition-based triggers. A plan without a
    calendar interval (only operating hours, a meter trigger, or no
    recurrence at all) has no calendar due date: it is never reported
    as due, and :meth:`plans_without_calendar_due_date` lists it.

    Args:
        plans: Optional list of maintenance plans to manage.
        last_executed: Optional mapping of plan ID to last execution date.
    """

    def __init__(
        self,
        *,
        plans: list[MaintenancePlan] | None = None,
        last_executed: dict[str, date] | None = None,
    ) -> None:
        self._plans = plans or []
        self._last_executed = last_executed or {}

    def next_due_date(
        self,
        plan: MaintenancePlan,
        last_executed: date,
    ) -> date:
        """Calculate the next due date for a maintenance plan.

        Args:
            plan: The maintenance plan.
            last_executed: Date the plan was last executed.

        Returns:
            The next calendar date the maintenance is due.

        Raises:
            DomainValidationError: The plan has no calendar interval, so
                no calendar due date exists for it.
        """
        if not _has_calendar_interval(plan):
            raise DomainValidationError(f"Plan {plan.id!r}: {NO_CALENDAR_DUE_DATE_REASON}")
        return last_executed + timedelta(days=plan.interval.total_days)

    def is_overdue(
        self,
        plan: MaintenancePlan,
        last_executed: date,
        today: date | None = None,
    ) -> bool:
        """Check whether a maintenance plan is overdue.

        Args:
            plan: The maintenance plan.
            last_executed: Date the plan was last executed.
            today: Reference date (defaults to today).

        Returns:
            True if the plan is past its due date.

        Raises:
            DomainValidationError: The plan has no calendar interval.
        """
        today = today or date.today()
        return today > self.next_due_date(plan, last_executed)

    def find_window(
        self,
        *,
        asset_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Find the next available maintenance window for an asset.

        Returns a suggested window based on the asset's maintenance
        plans and last execution dates.  In future versions this will
        integrate with calendar connectors to check production schedules.
        Inactive plans and plans without a calendar interval are not
        considered.

        Args:
            asset_id: Target asset identifier.

        Returns:
            A dict with ``start``, ``end``, ``plan_id`` and ``asset_id``.
        """
        today = date.today()
        best: dict[str, Any] | None = None

        for plan in self._plans:
            if asset_id and plan.asset_id != asset_id:
                continue
            if not plan.active or not _has_calendar_interval(plan):
                continue
            last = self._last_executed.get(
                plan.id, today - timedelta(days=plan.interval.total_days)
            )
            due = self.next_due_date(plan, last)
            window_start = max(due, today)
            duration_days = max(1, int((plan.estimated_duration_hours or 4) / 8))
            window_end = window_start + timedelta(days=duration_days)

            if best is None or window_start < best["start"]:
                best = {
                    "start": window_start,
                    "end": window_end,
                    "plan_id": plan.id,
                    "plan_name": plan.name,
                    "asset_id": plan.asset_id,
                }

        if best is None:
            return {
                "start": today + timedelta(days=1),
                "end": today + timedelta(days=2),
                "plan_id": "",
                "plan_name": "Ad-hoc maintenance",
                "asset_id": asset_id,
            }
        return best

    def scan_due_plans(
        self,
        *,
        horizon_days: int | str = 14,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return maintenance plans due within the given horizon.

        Plans without a calendar interval are never reported as due:
        :meth:`plans_without_calendar_due_date` lists them instead.

        Args:
            horizon_days: Number of days to look ahead (accepts str
                for workflow template compatibility).

        Returns:
            A list of dicts with plan details and due dates.
        """
        horizon = int(horizon_days)
        today = date.today()
        cutoff = today + timedelta(days=horizon)
        results: list[dict[str, Any]] = []

        for plan in self._plans:
            if not plan.active or not _has_calendar_interval(plan):
                continue
            last = self._last_executed.get(
                plan.id, today - timedelta(days=plan.interval.total_days)
            )
            due = self.next_due_date(plan, last)
            if due <= cutoff:
                results.append(
                    {
                        "plan_id": plan.id,
                        "plan_name": plan.name,
                        "asset_id": plan.asset_id,
                        "due_date": due,
                        "overdue": due < today,
                        "tasks": plan.tasks,
                    }
                )

        results.sort(key=lambda r: r["due_date"])
        return results

    def plans_without_calendar_due_date(
        self,
        *,
        asset_id: str = "",
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """List the plans that calendar scheduling cannot place.

        These plans recur on operating hours, on a meter reading, or on
        no stated recurrence, so :meth:`scan_due_plans` and
        :meth:`find_window` leave them out rather than invent a due
        date. Each plan is listed with its own ``active`` flag.

        Args:
            asset_id: Keep only the plans for this asset (all when empty).

        Returns:
            A list of dicts with ``plan_id``, ``plan_name``, ``asset_id``,
            ``active``, ``interval_operating_hours`` (``None`` when not
            set) and ``reason``.
        """
        return [
            {
                "plan_id": plan.id,
                "plan_name": plan.name,
                "asset_id": plan.asset_id,
                "active": plan.active,
                "interval_operating_hours": plan.interval.hours or None,
                "reason": NO_CALENDAR_DUE_DATE_REASON,
            }
            for plan in self._plans
            if not _has_calendar_interval(plan) and (not asset_id or plan.asset_id == asset_id)
        ]
