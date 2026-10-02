"""Tests for the get_maintenance_schedule agent tool."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from machina.agent.maintenance_schedule import DUE_DATES_NOTE, get_maintenance_schedule
from machina.connectors.base import ConnectorRegistry
from machina.connectors.capabilities import Capability
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.exceptions import ConnectorError


def _plans() -> list[MaintenancePlan]:
    return [
        MaintenancePlan(
            id="MP-P201-Q",
            asset_id="P-201",
            name="Quarterly bearing inspection",
            interval=Interval(months=3),
            tasks=["Check vibration", "Inspect seal"],
            estimated_duration_hours=2.0,
            required_skills=["mechanical"],
        ),
        MaintenancePlan(
            id="MP-P201-H",
            asset_id="P-201",
            name="Run-hours overhaul",
            interval=Interval(hours=8000),
            active=False,
        ),
        MaintenancePlan(
            id="MP-P202-M",
            asset_id="P-202",
            name="Monthly lubrication",
            interval=Interval(weeks=4),
        ),
    ]


class _PlansConnector:
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READ_MAINTENANCE_PLANS})

    def __init__(self, plans: list[MaintenancePlan] | None = None, *, fail: bool = False) -> None:
        self._plans = plans if plans is not None else _plans()
        self._fail = fail
        self.reads = 0

    async def read_maintenance_plans(self) -> list[MaintenancePlan]:
        self.reads += 1
        if self._fail:
            raise ConnectorError(r"CMMS unreachable at C:\Users\ops\secret\cmms.log")
        return list(self._plans)


def _registry(*connectors: Any) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    for i, conn in enumerate(connectors):
        registry.register(f"cmms_{i}", conn)
    return registry


@pytest.mark.asyncio
async def test_lists_every_plan_with_recurrence_and_effort() -> None:
    result = await get_maintenance_schedule(_registry(_PlansConnector()))

    assert [p["plan_id"] for p in result["plans"]] == ["MP-P201-Q", "MP-P201-H", "MP-P202-M"]
    quarterly = result["plans"][0]
    assert quarterly["interval_days"] == 90
    assert quarterly["interval_operating_hours"] is None
    assert quarterly["tasks"] == ["Check vibration", "Inspect seal"]
    assert quarterly["estimated_duration_hours"] == 2.0
    assert quarterly["required_skills"] == ["mechanical"]
    assert result["plans"][1]["interval_operating_hours"] == 8000
    assert result["plans"][1]["active"] is False
    assert result["due_dates"] == DUE_DATES_NOTE
    assert "note" not in result


@pytest.mark.asyncio
async def test_filters_by_asset() -> None:
    result = await get_maintenance_schedule(_registry(_PlansConnector()), asset_id="P-201")
    assert [p["plan_id"] for p in result["plans"]] == ["MP-P201-Q", "MP-P201-H"]


@pytest.mark.asyncio
async def test_never_reports_due_dates() -> None:
    result = await get_maintenance_schedule(_registry(_PlansConnector()))
    for plan in result["plans"]:
        assert "due_date" not in plan
        assert "overdue" not in plan


@pytest.mark.asyncio
async def test_unknown_asset_gets_an_explanatory_note() -> None:
    result = await get_maintenance_schedule(_registry(_PlansConnector()), asset_id="X-999")
    assert result["plans"] == []
    assert "X-999" in result["note"]


@pytest.mark.asyncio
async def test_provider_without_plans_gets_an_explanatory_note() -> None:
    result = await get_maintenance_schedule(_registry(_PlansConnector(plans=[])))
    assert result["plans"] == []
    assert "no maintenance plans" in result["note"]


@pytest.mark.asyncio
async def test_no_provider_is_a_tool_error() -> None:
    result = await get_maintenance_schedule(ConnectorRegistry())
    assert result == {"error": "No connector provides maintenance plans"}


@pytest.mark.asyncio
async def test_connector_failure_degrades_to_a_scrubbed_tool_error() -> None:
    result = await get_maintenance_schedule(_registry(_PlansConnector(fail=True)))
    assert "CMMS unreachable" in result["error"]
    assert "secret" not in result["error"]  # absolute paths never reach the LLM


@pytest.mark.asyncio
async def test_reads_only_the_first_provider() -> None:
    first, second = _PlansConnector(), _PlansConnector(plans=[])
    await get_maintenance_schedule(_registry(first, second))
    assert (first.reads, second.reads) == (1, 0)
