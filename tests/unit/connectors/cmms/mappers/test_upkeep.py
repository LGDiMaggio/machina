"""Unit tests for the UpKeep mapper — pure ``dict`` → Entity conversions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from machina.connectors.cmms.mappers.upkeep import (
    REVERSE_UPKEEP_STATUS,
    parse_asset,
    parse_asset_part_ids,
    parse_datetime,
    parse_maintenance_plans,
    parse_part_record,
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

# The work-order status values UpKeep documents for the ``status`` filter of
# GET /api/v2/work-orders and the PATCH /api/v2/work-orders/<ID> body:
# https://developers.onupkeep.com/#get-all-work-orders
UPKEEP_STATUSES = {"open", "onHold", "inProgress", "complete"}


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

    @pytest.mark.parametrize(
        ("upkeep_status", "expected"),
        [
            ("open", WorkOrderStatus.CREATED),
            ("onHold", WorkOrderStatus.ASSIGNED),
            ("inProgress", WorkOrderStatus.IN_PROGRESS),
            ("complete", WorkOrderStatus.COMPLETED),
        ],
    )
    def test_documented_status_values(self, upkeep_status: str, expected: WorkOrderStatus) -> None:
        wo = parse_work_order({"id": "5", "status": upkeep_status})
        assert wo.status == expected

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("in progress", WorkOrderStatus.IN_PROGRESS),
            ("On Hold", WorkOrderStatus.ASSIGNED),
        ],
    )
    def test_spaced_status_labels_still_map(self, label: str, expected: WorkOrderStatus) -> None:
        """Not API values, but the spaced forms this mapper once expected still map."""
        wo = parse_work_order({"id": "6", "status": label})
        assert wo.status == expected


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


def _docs_asset_body(**result: Any) -> dict[str, Any]:
    """Response of "Get a Specific Asset", GET /api/v2/assets/<ID>.

    Shaped after the documented response
    (https://developers.onupkeep.com/#get-a-specific-asset), with synthetic
    values and a subset of its keys. ``parts`` holds the IDs of the parts the
    asset lists; the parent and sub-asset IDs next to it are not parts.
    Keyword arguments replace fields of ``result``.
    """
    record: dict[str, Any] = {
        "id": "8iqb8hStJt",
        "name": "Cooling water pump CWP-2",
        "parentAsset": "QRFWg1fz1b",
        "subAssets": ["McaNx1Zl4u"],
        "parts": ["Wuy1TIHIfL"],
    }
    record.update(result)
    return {"success": True, "result": record}


def _docs_part_body(**result: Any) -> dict[str, Any]:
    """Response of "Get a Specific Part", GET /api/v2/parts/<ID>.

    Shaped after the documented response
    (https://developers.onupkeep.com/#get-a-specific-part), with synthetic
    values and a subset of its keys: those the mapper reads, and the
    ``partId``, ``partName``, ``serial``, ``minimumQuantity`` and ``location``
    it must not read in their place. Keyword arguments replace fields of
    ``result``.
    """
    record: dict[str, Any] = {
        "id": "Wuy1TIHIfL",
        "name": "GS-45 seal cartridge",
        "cost": "37.80",
        "quantity": "6",
        "minimumQuantity": "2",
        "partId": "def8ec501103f8f3cab63802",
        "partName": "Seal cartridge",
        "serial": "SC45-20931",
        "area": "Store 2, bin 14",
        "location": "TvXIVf42fB",
    }
    record.update(result)
    return {"success": True, "result": record}


_DOCS_ASSET_ID = "8iqb8hStJt"


class TestParseAssetPartIds:
    """The asset's ``parts`` list holds the IDs of the parts UpKeep associates with it."""

    def test_documented_asset(self) -> None:
        assert parse_asset_part_ids(_docs_asset_body(), _DOCS_ASSET_ID) == ["Wuy1TIHIfL"]

    def test_record_outside_result(self) -> None:
        """A body without ``result`` is read as the record, as the connector's reads do."""
        record = _docs_asset_body()["result"]
        assert parse_asset_part_ids(record, _DOCS_ASSET_ID) == ["Wuy1TIHIfL"]

    @pytest.mark.parametrize("parts", ["missing", None, []], ids=["missing", "null", "empty"])
    def test_no_parts_listed_is_an_empty_association(self, parts: object) -> None:
        body = _docs_asset_body(parts=parts)
        if parts == "missing":
            del body["result"]["parts"]
        assert parse_asset_part_ids(body, _DOCS_ASSET_ID) == []

    def test_duplicates_keep_their_first_position(self) -> None:
        body = _docs_asset_body(parts=["b", "a", "b", "c", "a"])
        assert parse_asset_part_ids(body, _DOCS_ASSET_ID) == ["b", "a", "c"]

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({}, id="empty-object"),
            pytest.param({"success": True}, id="no-result"),
            pytest.param(_docs_asset_body(id="Zq8RtW3mKe"), id="another-asset"),
            pytest.param(_docs_asset_body(id=None), id="id-null"),
        ],
    )
    def test_a_body_that_is_not_the_assets_record_raises_value_error(self, body: object) -> None:
        """Only the requested asset's own record can say it lists no parts."""
        with pytest.raises(ValueError):
            parse_asset_part_ids(body, _DOCS_ASSET_ID)

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(["Wuy1TIHIfL"], id="body-array"),
            pytest.param("Wuy1TIHIfL", id="body-string"),
            pytest.param(None, id="body-null"),
            pytest.param({"success": True, "result": None}, id="result-null"),
            pytest.param({"success": True, "result": ["Wuy1TIHIfL"]}, id="result-array"),
            pytest.param({"success": True, "result": "8iqb8hStJt"}, id="result-string"),
            pytest.param(_docs_asset_body(parts="Wuy1TIHIfL"), id="parts-string"),
            pytest.param(_docs_asset_body(parts={"id": "Wuy1TIHIfL"}), id="parts-object"),
            pytest.param(_docs_asset_body(parts=[12345]), id="entry-number"),
            pytest.param(_docs_asset_body(parts=[None]), id="entry-null"),
            pytest.param(_docs_asset_body(parts=[True]), id="entry-boolean"),
            pytest.param(
                _docs_asset_body(parts=[{"id": "Wuy1TIHIfL", "name": "GS-45 seal cartridge"}]),
                id="entry-object",
            ),
        ],
    )
    def test_malformed_body_raises_type_error(self, body: object) -> None:
        with pytest.raises(TypeError):
            parse_asset_part_ids(body, _DOCS_ASSET_ID)

    def test_empty_entry_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            parse_asset_part_ids(_docs_asset_body(parts=["Wuy1TIHIfL", ""]), _DOCS_ASSET_ID)


class TestParsePartRecord:
    """The ID and record of a single-part read, checked for an ``id``."""

    def test_documented_part(self) -> None:
        part_id, record = parse_part_record(_docs_part_body())
        assert part_id == "Wuy1TIHIfL"
        assert record["name"] == "GS-45 seal cartridge"

    def test_record_outside_result(self) -> None:
        part_id, record = parse_part_record(_docs_part_body()["result"])
        assert part_id == "Wuy1TIHIfL"
        assert record["name"] == "GS-45 seal cartridge"

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(["Wuy1TIHIfL"], id="body-array"),
            pytest.param(None, id="body-null"),
            pytest.param({"success": True, "result": None}, id="result-null"),
            pytest.param({"success": True, "result": ["Wuy1TIHIfL"]}, id="result-array"),
            pytest.param(_docs_part_body(id=12345), id="id-number"),
        ],
    )
    def test_malformed_body_raises_type_error(self, body: object) -> None:
        with pytest.raises(TypeError):
            parse_part_record(body)

    @pytest.mark.parametrize("part_id", ["missing", None, ""], ids=["missing", "null", "empty"])
    def test_record_without_id_raises_value_error(self, part_id: object) -> None:
        body = _docs_part_body(id=part_id)
        if part_id == "missing":
            del body["result"]["id"]
        with pytest.raises(ValueError):
            parse_part_record(body)


# Both schedules run from 2025-03-03 to 2026-03-02.
DURING_SCHEDULES = datetime(2025, 6, 1, tzinfo=UTC)
AFTER_SCHEDULES = datetime(2026, 3, 3, tzinfo=UTC)


def _docs_pm_template() -> dict[str, Any]:
    """Response of "Get a specific PM" (#get-a-specific-pm), GET /api/v2/pm/:id.

    Shaped after https://developers.onupkeep.com/#get-a-specific-pm, with
    synthetic values and a subset of its keys: a pump's daily round, with one
    calendar schedule and one meter schedule on the same asset. A schedule's
    ``location`` is not its asset.
    """
    return {
        "estimatedTime": 2,
        "id": "6045fdf022af4032c65aa3f6",
        "name": "CWP-2 daily round",
        "schedules": [
            {
                "asset": "8iqb8hStJt",
                "cadenceFreq": "DAILY",
                "cadenceInterval": 1,
                "endDate": "2026-03-02T06:00:00.000Z",
                "id": "4054992e77b334fa30d1e8f0",
                "location": "HZsOn28jvL",
                "pmTemplate": "6045fdf022af4032c65aa3f6",
                "repeatFrequency": "DAILY",
                "repeatInterval": 1,
                "scheduleType": "EVERY_N_DAYS",
                "startDate": "2025-03-03T06:00:00.000Z",
            },
            {
                "asset": "8iqb8hStJt",
                "endDate": "2026-03-02T06:30:00.000Z",
                "id": "fe3e7424e3d4d6a54df9f00b",
                "location": "HZsOn28jvL",
                "meter": "2g5tVrrs0R",
                "meterConditionValue": 250,
                "meterDueFrequency": "weeks",
                "meterDueInterval": 2,
                "pmTemplate": "6045fdf022af4032c65aa3f6",
                "startDate": "2025-03-03T06:30:00.000Z",
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
            "4054992e77b334fa30d1e8f0",
            "fe3e7424e3d4d6a54df9f00b",
        ]
        assert {p.name for p in plans} == {"CWP-2 daily round"}
        assert {p.asset_id for p in plans} == {"8iqb8hStJt"}

    def test_calendar_schedule_interval_duration_and_active(self) -> None:
        calendar = parse_maintenance_plans(_docs_pm_template(), now=DURING_SCHEDULES)[0]
        assert calendar.interval == Interval(days=1)
        assert calendar.estimated_duration_hours == 2  # "Duration ... in hours"
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
        assert parse_maintenance_plans(template)[0].id == "4054992e77b334fa30d1e8f0"

    def test_task_names_become_tasks(self) -> None:
        """Tasks are form items with ``name`` and ``type`` keys (no populated docs example)."""
        template = _docs_pm_template()
        template["tasks"] = [
            {"name": "Check the seal for leaks", "type": "TASK"},
            {"name": "Bearing temperature", "type": "NUMBER"},
            {"name": "Run hours", "type": "METER", "meter": "2g5tVrrs0R"},
        ]
        plan = parse_maintenance_plans(template)[0]
        assert plan.tasks == ["Check the seal for leaks", "Bearing temperature", "Run hours"]

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
        template["deletedAt"] = "2025-09-15T10:12:44.318Z"
        template["deletedBy"] = "zHbT5ePyUm"
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
        """UpKeep has no distinct CANCELLED state — maps to 'onHold'."""
        assert reverse_status(WorkOrderStatus.CANCELLED) == "onHold"

    def test_reverse_status_uses_camel_case_values(self) -> None:
        assert reverse_status(WorkOrderStatus.IN_PROGRESS) == "inProgress"
        assert reverse_status(WorkOrderStatus.ASSIGNED) == "onHold"

    def test_every_status_has_a_documented_upkeep_value(self) -> None:
        """The status filter and the PATCH body only ever send values UpKeep accepts."""
        assert set(REVERSE_UPKEEP_STATUS) == set(WorkOrderStatus)
        assert set(REVERSE_UPKEEP_STATUS.values()) <= UPKEEP_STATUSES
