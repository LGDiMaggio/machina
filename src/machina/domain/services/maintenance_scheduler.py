"""MaintenanceScheduler — calculate upcoming maintenance windows.

Determines when the next preventive maintenance is due for assets
based on their maintenance plans and last execution dates.
"""

from __future__ import annotations

from datetime import date, timedelta

from machina.domain.maintenance_plan import MaintenancePlan  # noqa: TC001


class MaintenanceScheduler:
    """Service for computing maintenance due dates.

    This is a calendar-based baseline; future versions will account
    for operating hours and condition-based triggers.
    """

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
        """
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
        """
        today = today or date.today()
        return today > self.next_due_date(plan, last_executed)
