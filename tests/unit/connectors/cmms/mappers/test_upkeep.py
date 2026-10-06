"""Unit tests for the UpKeep mapper — pure ``dict`` → Entity conversions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from machina.connectors.cmms.mappers.upkeep import (
    parse_asset,
    parse_datetime,
    parse_maintenance_plans,
    parse_spare_part,
    parse_work_order,
    reverse_priority,
    reverse_status,
)
from machina.domain.asset import AssetType, Criticality
from machina.domain.maintenance_plan import Interval
from machina.domain.work_order import (
    Priority,
    WorkOrderStatus,
    WorkOrderType,
)


class TestParseAssetPublicAPI:
    def test_happy_path(self) -> None:
        asset = parse_asset({"id": "1", "name": "Pump"})
        assert asset.id == "1"
        assert asset.name == "Pump"
        assert asset.criticality == Criticality.C  # UpKeep has no criticality field

    def test_category_rotating_equipment(self) -> None:
        asset = parse_asset({"id": "2", "category": "Rotating Equipment"})
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_unknown_category_falls_back_to_rotating(self) -> None:
        asset = parse_asset({"id": "3", "category": "Mystery"})
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_empty_dict_raises_validation_error(self) -> None:
        """Empty input has no id → Asset pydantic validator rejects."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="id cannot be empty"):
            parse_asset({})


class TestParseWorkOrderPublicAPI:
    def test_priority_0_is_low(self) -> None:
        """UpKeep uses 0-indexed priority (0 = lowest)."""
        wo = parse_work_order({"id": "1", "priority": 0})
        assert wo.priority == Priority.LOW

    def test_priority_3_is_emergency(self) -> None:
        wo = parse_work_order({"id": "2", "priority": 3})
        assert wo.priority == Priority.EMERGENCY

    def test_category_preventive_maps_to_preventive_type(self) -> None:
        wo = parse_work_order({"id": "3", "category": "preventive"})
        assert wo.type == WorkOrderType.PREVENTIVE

    def test_default_category_maps_to_corrective(self) -> None:
        wo = parse_work_order({"id": "4"})
        assert wo.type == WorkOrderType.CORRECTIVE

    def test_status_on_hold_maps_to_assigned(self) -> None:
        wo = parse_work_order({"id": "5", "status": "on hold"})
        assert wo.status == WorkOrderStatus.ASSIGNED


class TestParseSparePartSku:
    """SKU preference order: partNumber > barcode > id."""

    def test_prefers_part_number(self) -> None:
        sp = parse_spare_part({"id": "internal", "partNumber": "PN-1", "barcode": "BC-1"})
        assert sp.sku == "PN-1"

    def test_falls_back_to_barcode(self) -> None:
        sp = parse_spare_part({"id": "internal", "barcode": "BC-1"})
        assert sp.sku == "BC-1"

    def test_falls_back_to_id_when_neither_provided(self) -> None:
        sp = parse_spare_part({"id": "internal"})
        assert sp.sku == "internal"


# Both documented schedules run from 2023-12-10 to 2024-10-09.
DURING_SCHEDULES = datetime(2024, 1, 1, tzinfo=UTC)
AFTER_SCHEDULES = datetime(2024, 10, 10, tzinfo=UTC)


def _docs_pm_template() -> dict[str, Any]:
    """Response of "Get a specific PM" (#get-a-specific-pm), GET /api/v2/pm/:id.

    Taken from https://developers.onupkeep.com/ — one calendar schedule and
    one meter schedule on the same asset.
    """
    return {
        "category": "Inspection",
        "createFirstWO": True,
        "createdAt": "2023-11-29T12:26:52.682Z",
        "createdBy": "2kGw1JigKE",
        "estimatedTime": 71,
        "files": ["fED0SSDyqF"],
        "formTemplate": "fUC7Anvm2y",
        "id": "65672e0c90176209662a4fc1",
        "images": [],
        "mainDescription": "Oil change",
        "name": "Oil change",
        "note": "Oil change",
        "partInventories": [{"part": "nrnMB2M6O5", "quantity": 1}],
        "priority": 0,
        "requiresSignature": True,
        "role": "PUlegnw3ml",
        "schedules": [
            {
                "asset": "BcmoxOY4mE",
                "assignee": "Fu8PEA9P9G",
                "bySetPosition": [],
                "cadenceFreq": "DAILY",
                "cadenceInterval": 1,
                "cadenceType": "manual",
                "endDate": "2024-10-09T08:42:24.000Z",
                "excludedates": [],
                "id": "65672e0c90176209662a4fc2",
                "inactiveIntervals": [
                    {"start": {"month": 1, "day": 1}, "end": {"month": 1, "day": 16}}
                ],
                "includeDates": [],
                "isBasedOnCompletion": False,
                "location": "c78MVjkbvh",
                "monthdays": [],
                "nextDueDate": "2023-12-11T08:42:24.000Z",
                "nextTriggerDate": "2023-12-10T08:42:24.000Z",
                "pmTemplate": "65672e0c90176209662a4fc1",
                "repeatFrequency": "DAILY",
                "repeatInterval": 1,
                "role": "PUlegnw3ml",
                "scheduleType": "EVERY_N_DAYS",
                "startDate": "2023-12-10T08:42:24.000Z",
                "supportUsers": ["IDb8mbMV9l"],
                "team": "SFejxVqERT",
                "timeZone": "Asia/Kolkata",
                "weekdays": [],
            },
            {
                "asset": "BcmoxOY4mE",
                "assignee": "Fu8PEA9P9G",
                "bySetPosition": [],
                "endDate": "2024-10-09T16:23:35.000Z",
                "excludedates": [],
                "id": "65672e0c90176209662a4fc4",
                "inactiveIntervals": [
                    {"start": {"month": 5, "day": 1}, "end": {"month": 8, "day": 1}}
                ],
                "includeDates": [],
                "location": "c78MVjkbvh",
                "meter": "vXMGBoLMJv",
                "meterConditionValue": 1000,
                "meterDueFrequency": "weeks",
                "meterDueInterval": 1,
                "monthdays": [],
                "nextMeterReading": 1075,
                "pmTemplate": "65672e0c90176209662a4fc1",
                "role": "PUlegnw3ml",
                "startDate": "2023-12-10T16:23:35.000Z",
                "supportUsers": ["IDb8mbMV9l"],
                "team": "SFejxVqERT",
                "timeZone": "Asia/Kolkata",
                "weekdays": [],
            },
        ],
        "tasks": [],
    }


class TestParseMaintenancePlans:
    """A PM template (GET /api/v2/pm) yields one plan per schedule.

    The asset and the recurrence live on each schedule; the name, tasks and
    estimated time on the template.
    """

    def test_one_plan_per_schedule(self) -> None:
        plans = parse_maintenance_plans(_docs_pm_template())
        assert [p.id for p in plans] == [
            "65672e0c90176209662a4fc2",
            "65672e0c90176209662a4fc4",
        ]
        assert {p.name for p in plans} == {"Oil change"}
        assert {p.asset_id for p in plans} == {"BcmoxOY4mE"}

    def test_calendar_schedule_interval_duration_and_active(self) -> None:
        calendar = parse_maintenance_plans(_docs_pm_template(), now=DURING_SCHEDULES)[0]
        assert calendar.interval == Interval(days=1)
        assert calendar.estimated_duration_hours == 71  # "Duration ... in hours"
        assert calendar.active is True

    def test_meter_schedule_has_no_calendar_interval(self) -> None:
        """The meter's unit is not in the PM payload, so no interval is guessed."""
        meter = parse_maintenance_plans(_docs_pm_template())[1]
        assert meter.interval == Interval()

    @pytest.mark.parametrize(
        ("frequency", "every", "expected"),
        [
            ("DAILY", 3, Interval(days=3)),
            ("WEEKLY", 2, Interval(weeks=2)),
            ("MONTHLY", 7, Interval(months=7)),
            ("YEARLY", 1, Interval(months=12)),
        ],
    )
    def test_repeat_frequency_and_interval(
        self, frequency: str, every: int, expected: Interval
    ) -> None:
        template = _docs_pm_template()
        schedule = template["schedules"][0]
        schedule["repeatFrequency"] = frequency
        schedule["repeatInterval"] = every
        assert parse_maintenance_plans(template)[0].interval == expected

    def test_underscore_id_keys(self) -> None:
        """Create/list responses key the template and schedules by ``_id``."""
        template = _docs_pm_template()
        template["_id"] = template.pop("id")
        schedule = template["schedules"][0]
        schedule["_id"] = schedule.pop("id")
        assert parse_maintenance_plans(template)[0].id == "65672e0c90176209662a4fc2"

    def test_task_names_become_tasks(self) -> None:
        """Tasks are form items with ``name`` and ``type`` keys (no populated docs example)."""
        template = _docs_pm_template()
        template["tasks"] = [
            {"name": "Drain old oil", "type": "TASK"},
            {"name": "Oil level after refill", "type": "NUMBER"},
            {"name": "Hour meter", "type": "METER", "meter": "vXMGBoLMJv"},
        ]
        plan = parse_maintenance_plans(template)[0]
        assert plan.tasks == ["Drain old oil", "Oil level after refill", "Hour meter"]

    def test_schedule_without_asset(self) -> None:
        template = _docs_pm_template()
        del template["schedules"][0]["asset"]
        assert parse_maintenance_plans(template)[0].asset_id == ""

    def test_schedule_past_its_end_date_is_inactive(self) -> None:
        """``endDate`` is the schedule's "End date for trigger"."""
        plans = parse_maintenance_plans(_docs_pm_template(), now=AFTER_SCHEDULES)
        assert [p.active for p in plans] == [False, False]

    def test_schedule_without_end_date_stays_active(self) -> None:
        template = _docs_pm_template()
        del template["schedules"][0]["endDate"]
        assert parse_maintenance_plans(template, now=AFTER_SCHEDULES)[0].active is True

    def test_schedule_marked_ended_is_inactive(self) -> None:
        """``scheduleHasEnded`` appears on GET /api/v2/pm/schedules payloads."""
        template = _docs_pm_template()
        template["schedules"][0]["scheduleHasEnded"] = True
        assert parse_maintenance_plans(template, now=DURING_SCHEDULES)[0].active is False

    def test_deleted_template_yields_no_plans(self) -> None:
        """The "Get all PMs" example shows a soft-deleted template (``deletedAt``)."""
        template = _docs_pm_template()
        template["deletedAt"] = "2023-11-30T19:43:30.229Z"
        template["deletedBy"] = "N5lbFIv8LW"
        assert parse_maintenance_plans(template) == []

    def test_template_without_schedules_yields_no_plans(self) -> None:
        template = _docs_pm_template()
        template["schedules"] = []
        assert parse_maintenance_plans(template) == []


class TestParseDatetime:
    def test_iso_with_z(self) -> None:
        dt = parse_datetime("2024-06-01T09:30:00Z")
        assert dt == datetime(2024, 6, 1, 9, 30, 0, tzinfo=UTC)


class TestReverseMaps:
    def test_reverse_priority_low_is_0(self) -> None:
        """Inverse of 0-indexed priority: LOW → 0."""
        assert reverse_priority(Priority.LOW) == 0

    def test_reverse_priority_emergency_is_3(self) -> None:
        assert reverse_priority(Priority.EMERGENCY) == 3

    def test_reverse_status_closed_maps_to_complete(self) -> None:
        """UpKeep has no distinct CLOSED state — both COMPLETED and CLOSED → 'complete'."""
        assert reverse_status(WorkOrderStatus.COMPLETED) == "complete"
        assert reverse_status(WorkOrderStatus.CLOSED) == "complete"

    def test_reverse_status_cancelled_maps_to_on_hold(self) -> None:
        """UpKeep has no distinct CANCELLED state — maps to 'on hold'."""
        assert reverse_status(WorkOrderStatus.CANCELLED) == "on hold"
