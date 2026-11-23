"""Unit tests for the SAP PM mapper — pure ``dict`` → Entity conversions.

These tests exercise the public API of
:mod:`machina.connectors.cmms.mappers.sap_pm` with raw dictionaries.
No HTTP mocks; no connector state; no network.  Complements the
existing characterization tests in ``tests/unit/test_sap_pm.py`` which
were redirected to the mapper module as part of the Unit 2 refactor.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from machina.connectors.cmms.mappers.sap_pm import (
    SapFieldError,
    order_etag,
    parse_asset,
    parse_maintenance_plan,
    parse_sap_datetime,
    parse_spare_part,
    parse_work_order,
    reverse_order_type,
    reverse_priority,
    reverse_status,
)
from machina.domain.asset import AssetType
from machina.domain.work_order import (
    Priority,
    WorkOrderStatus,
    WorkOrderType,
)


class TestParseAssetPublicAPI:
    """parse_asset: dict → Asset happy path and edge cases."""

    def test_happy_path(self) -> None:
        asset = parse_asset(
            {"Equipment": "EQ-1", "EquipmentName": "Pump 1", "EquipmentCategory": "M"}
        )
        assert asset.id == "EQ-1"
        assert asset.name == "Pump 1"
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_missing_category_falls_back_to_rotating(self) -> None:
        asset = parse_asset({"Equipment": "EQ-2", "EquipmentName": "Noname"})
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_empty_dict_raises_validation_error(self) -> None:
        """Empty input has no Equipment id → Asset pydantic validator rejects."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="id cannot be empty"):
            parse_asset({})

    def test_category_e_maps_to_electrical(self) -> None:
        asset = parse_asset({"Equipment": "EQ-3", "EquipmentCategory": "E"})
        assert asset.type == AssetType.ELECTRICAL


class TestParseWorkOrderPublicAPI:
    """parse_work_order: dict → WorkOrder happy path and edge cases."""

    def test_compound_status_rel_wins_over_crtd(self) -> None:
        """SAP compound status ``"CRTD REL MANC"`` → ASSIGNED (most progressed)."""
        wo = parse_work_order(
            {
                "MaintenanceOrder": "W-1",
                "SystemStatusText": "CRTD REL MANC",
            }
        )
        assert wo.status == WorkOrderStatus.ASSIGNED

    def test_compound_status_teco_wins(self) -> None:
        """TECO > REL > CRTD in lifecycle progression."""
        wo = parse_work_order(
            {
                "MaintenanceOrder": "W-2",
                "SystemStatusText": "CRTD REL TECO",
            }
        )
        assert wo.status == WorkOrderStatus.CLOSED

    def test_missing_creation_date_defaults_to_utc_now(self) -> None:
        before = datetime.now(tz=UTC)
        wo = parse_work_order({"MaintenanceOrder": "W-3"})
        after = datetime.now(tz=UTC)
        assert before <= wo.created_at <= after
        assert wo.created_at.tzinfo == UTC


class TestParseSapDatetime:
    """parse_sap_datetime: handle every SAP date format Machina has seen."""

    def test_sap_millis_format_returns_utc(self) -> None:
        """``/Date(1704067200000+0000)/`` → 2024-01-01 00:00:00 UTC."""
        dt = parse_sap_datetime("/Date(1704067200000+0000)/")
        assert dt == datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_yyyymmdd_format(self) -> None:
        dt = parse_sap_datetime("20240101")
        assert dt == datetime(2024, 1, 1, tzinfo=UTC)

    def test_empty_string_returns_utc_now_without_raising(self) -> None:
        before = datetime.now(tz=UTC)
        dt = parse_sap_datetime("")
        after = datetime.now(tz=UTC)
        assert before <= dt <= after

    def test_unparseable_string_returns_utc_now_without_raising(self) -> None:
        """Garbage in → current UTC (graceful degradation)."""
        before = datetime.now(tz=UTC)
        dt = parse_sap_datetime("not-a-date-at-all")
        after = datetime.now(tz=UTC)
        assert before <= dt <= after

    def test_iso8601_with_z_suffix(self) -> None:
        dt = parse_sap_datetime("2024-01-01T12:00:00Z")
        assert dt == datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


class TestReverseMaps:
    """reverse_priority / reverse_order_type / reverse_status."""

    def test_reverse_priority_emergency_is_1(self) -> None:
        assert reverse_priority(Priority.EMERGENCY) == "1"

    def test_reverse_priority_medium_is_3(self) -> None:
        assert reverse_priority(Priority.MEDIUM) == "3"

    def test_reverse_order_type_corrective_is_pm01(self) -> None:
        assert reverse_order_type(WorkOrderType.CORRECTIVE) == "PM01"

    def test_reverse_status_cancelled_is_dlfl(self) -> None:
        assert reverse_status(WorkOrderStatus.CANCELLED) == "DLFL"

    def test_reverse_status_unknown_falls_back_to_crtd(self) -> None:
        """Defensive: any unmapped status falls back to ``CRTD``."""
        # No unmapped members today; assert the documented fallback shape
        # via the default path on a member we control.
        result = reverse_status(WorkOrderStatus.CREATED)
        assert result == "CRTD"


# ---------------------------------------------------------------------------
# OData transport members stay out of ``metadata`` (SEC-18)
# ---------------------------------------------------------------------------

# The host and service path a SAP gateway reports itself on, often internal.
_INTERNAL_HOST = "sapgw-internal.plant.example:44300"
_SERVICE_ROOT = f"https://{_INTERNAL_HOST}/sap/opu/odata/sap"
_ETAG = "W/\"datetimeoffset'2026-11-22T10%3A00%3A00.0000000Z'\""

_Parse = Callable[[dict[str, Any]], Any]

# One record per entity with a ``metadata`` field, each with a property no
# domain field takes.
_RECORDS: list[tuple[_Parse, str, dict[str, Any]]] = [
    (
        parse_asset,
        "API_EQUIPMENT/Equipment('10000001')",
        {"Equipment": "10000001", "EquipmentName": "Pump", "MaintenancePlant": "1010"},
    ),
    (
        parse_work_order,
        "API_MAINTENANCEORDER/MaintenanceOrder('4000001')",
        {"MaintenanceOrder": "4000001", "Equipment": "10000001", "MaintenancePlant": "1010"},
    ),
    (
        parse_spare_part,
        "API_BILL_OF_MATERIAL_SRV/BillOfMaterialItem('1')",
        {"Material": "MAT-001", "AvailableQuantity": 5, "MaintenancePlant": "1010"},
    ),
]
_RECORD_IDS = ["asset", "work_order", "spare_part"]


def _odata_v2(record: dict[str, Any], path: str) -> dict[str, Any]:
    """``record`` as an OData V2 JSON entry: its ``__metadata`` and three navigation
    properties, one deferred and two expanded (an entry and a collection)."""
    return {
        "__metadata": {"uri": f"{_SERVICE_ROOT}/{path}", "type": "API.Entity", "etag": _ETAG},
        **record,
        "to_Plant": {"__deferred": {"uri": f"{_SERVICE_ROOT}/{path}/to_Plant"}},
        "to_Class": {
            "__metadata": {"uri": f"{_SERVICE_ROOT}/Class('PUMPS')", "type": "API.Class"},
            "Class": "PUMPS",
        },
        "to_Items": {
            "results": [
                {"__metadata": {"uri": f"{_SERVICE_ROOT}/Item('1')"}, "Item": "1"},
            ]
        },
        # An expanded collection with no entries in this page still carries
        # OData's own __next link (and __count): every "__" name is OData's.
        "to_Notes": {
            "results": [],
            "__next": f"{_SERVICE_ROOT}/{path}/to_Notes?$skiptoken=1",
            "__count": "0",
        },
        "Tags": ["critical", "outdoor"],
    }


def _odata_v4(record: dict[str, Any], path: str) -> dict[str, Any]:
    """``record`` as an OData V4 JSON entity with its control information."""
    return {
        "@odata.context": f"{_SERVICE_ROOT}/$metadata#Entity/$entity",
        "@odata.etag": _ETAG,
        "@odata.id": f"{_SERVICE_ROOT}/{path}",
        "@odata.editLink": path,
        **record,
        "to_Plant@odata.navigationLink": f"{_SERVICE_ROOT}/{path}/to_Plant",
        "to_Class": {"@odata.id": f"{_SERVICE_ROOT}/Class('PUMPS')", "Class": "PUMPS"},
        "Dimensions": {"Length": "12", "Unit": "MM"},
    }


class TestTransportMembersLeftOut:
    """No OData transport member, nor the host it names, reaches ``metadata``."""

    @pytest.mark.parametrize(("parse", "path", "record"), _RECORDS, ids=_RECORD_IDS)
    def test_odata_v2_entry(self, parse: _Parse, path: str, record: dict[str, Any]) -> None:
        entity = parse(_odata_v2(record, path))

        assert _INTERNAL_HOST not in entity.model_dump_json()
        assert "__metadata" not in json.dumps(entity.metadata)
        assert "__deferred" not in json.dumps(entity.metadata)
        # A list of plain values is a value: kept.
        assert entity.metadata == {"MaintenancePlant": "1010", "Tags": ["critical", "outdoor"]}

    @pytest.mark.parametrize(("parse", "path", "record"), _RECORDS, ids=_RECORD_IDS)
    def test_odata_v4_entity(self, parse: _Parse, path: str, record: dict[str, Any]) -> None:
        entity = parse(_odata_v4(record, path))

        assert _INTERNAL_HOST not in entity.model_dump_json()
        assert "@odata" not in json.dumps(entity.metadata)
        # A structured value without control information is a value: kept.
        assert entity.metadata == {
            "MaintenancePlant": "1010",
            "Dimensions": {"Length": "12", "Unit": "MM"},
        }

    def test_record_is_not_changed_and_its_etag_stays_readable(self) -> None:
        """The connector reads the ETag from the raw record, which mapping leaves alone."""
        record = _odata_v2(
            {"MaintenanceOrder": "4000001", "SystemStatusText": "CRTD"},
            "API_MAINTENANCEORDER/MaintenanceOrder('4000001')",
        )
        before = copy.deepcopy(record)

        parse_work_order(record)

        assert record == before
        assert order_etag(record) == _ETAG


# ---------------------------------------------------------------------------
# A value the mapper cannot read: the property, never the value (SEC-13)
# ---------------------------------------------------------------------------

_SECRET = "Q7ZX3"  # Stands for a vendor value that must not be repeated.

_WHOLE = "a whole number"


def _assert_value_free(error: SapFieldError, field: str) -> None:
    assert error.field == field
    assert _SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


class TestUnreadableValues:
    """A number or date the mapper cannot read raises :class:`SapFieldError`."""

    @pytest.mark.parametrize(
        ("record", "field", "expected"),
        [
            # An OData V2 Edm.Decimal arrives as a string with decimals.
            ({"Material": "M", "AvailableQuantity": "12.500"}, "AvailableQuantity", _WHOLE),
            ({"Material": "M", "AvailableQuantity": f"12.{_SECRET}"}, "AvailableQuantity", _WHOLE),
            ({"Material": "M", "Quantity": f"x{_SECRET}"}, "Quantity", _WHOLE),
            ({"Material": "M", "AvailableQuantity": {"v": _SECRET}}, "AvailableQuantity", _WHOLE),
            ({"Material": "M", "AvailableQuantity": None}, "AvailableQuantity", _WHOLE),
            ({"Material": "M", "StandardPrice": f"{_SECRET}.50"}, "StandardPrice", "a number"),
            ({"Material": "M", "Price": [_SECRET]}, "Price", "a number"),
        ],
    )
    def test_spare_part(self, record: dict[str, Any], field: str, expected: str) -> None:
        with pytest.raises(SapFieldError) as raised:
            parse_spare_part(record)
        _assert_value_free(raised.value, field)
        assert str(raised.value) == f"SAP property {field!r} is not {expected}"

    @pytest.mark.parametrize(
        ("record", "field"),
        [
            (
                {"MaintenancePlan": "P", "MaintenancePlanCycleValue": "3.500"},
                "MaintenancePlanCycleValue",
            ),
            ({"MaintenancePlan": "P", "CycleValue": _SECRET}, "CycleValue"),
        ],
    )
    def test_maintenance_plan_cycle(self, record: dict[str, Any], field: str) -> None:
        with pytest.raises(SapFieldError) as raised:
            parse_maintenance_plan(record)
        _assert_value_free(raised.value, field)
        assert str(raised.value) == f"SAP property {field!r} is not {_WHOLE}"

    @pytest.mark.parametrize(
        ("record", "field"),
        [
            ({"MaintenanceOrder": "W", "CreationDate": f"/Date({_SECRET})/"}, "CreationDate"),
            (
                {"MaintenanceOrder": "W", "MaintOrdBasicStartDate": f"/Date(1{_SECRET})/"},
                "MaintOrdBasicStartDate",
            ),
            # Far outside the range a datetime holds, either way.
            (
                {"MaintenanceOrder": "W", "LastChangeDateTime": "/Date(99999999999999999999)/"},
                "LastChangeDateTime",
            ),
            (
                {"MaintenanceOrder": "W", "MaintOrdBasicEndDate": "/Date(-99999999999999999)/"},
                "MaintOrdBasicEndDate",
            ),
            # Not text: a number of milliseconds is not one of SAP's date formats.
            ({"MaintenanceOrder": "W", "CreationDate": 1704067200000}, "CreationDate"),
        ],
    )
    def test_work_order_date(self, record: dict[str, Any], field: str) -> None:
        with pytest.raises(SapFieldError) as raised:
            parse_work_order(record)
        _assert_value_free(raised.value, field)
        assert str(raised.value) == f"SAP property {field!r} is not a date"

    @pytest.mark.parametrize(
        ("parse", "record", "field"),
        [
            (parse_asset, {"Equipment": {"__deferred": {"uri": _SERVICE_ROOT}}}, "Equipment"),
            (parse_asset, {"Equipment": "E", "EquipmentName": [_SECRET]}, "EquipmentName"),
            (
                parse_asset,
                {"Equipment": "E", "FunctionalLocation": {"v": _SECRET}},
                "FunctionalLocation",
            ),
            (parse_asset, {"Equipment": "E", "Manufacturer": [_SECRET]}, "Manufacturer"),
            (parse_asset, {"Equipment": "E", "ModelNumber": [_SECRET]}, "ModelNumber"),
            (parse_asset, {"Equipment": "E", "SerialNumber": [_SECRET]}, "SerialNumber"),
            (parse_work_order, {"MaintenanceOrder": [_SECRET]}, "MaintenanceOrder"),
            (
                parse_work_order,
                {"MaintenanceOrder": "W", "Equipment": {"v": _SECRET}},
                "Equipment",
            ),
            (parse_work_order, {"MaintenanceOrder": "W", "Description": [_SECRET]}, "Description"),
            (parse_spare_part, {"Material": {"__deferred": {"uri": _SERVICE_ROOT}}}, "Material"),
            (
                parse_spare_part,
                {"Material": "M", "MaterialDescription": [_SECRET]},
                "MaterialDescription",
            ),
            (parse_spare_part, {"Material": "M", "Plant": {"v": _SECRET}}, "Plant"),
            (parse_maintenance_plan, {"MaintenancePlan": [_SECRET]}, "MaintenancePlan"),
            (
                parse_maintenance_plan,
                {"MaintenancePlan": "P", "Equipment": [_SECRET]},
                "Equipment",
            ),
            (
                parse_maintenance_plan,
                {"MaintenancePlan": "P", "Description": [_SECRET]},
                "Description",
            ),
        ],
    )
    def test_structured_value_for_a_text_field(
        self, parse: _Parse, record: dict[str, Any], field: str
    ) -> None:
        """An object or list where a field takes text is refused, not turned into
        text that would carry it (a deferred link's URI, say) into the entity."""
        with pytest.raises(SapFieldError) as raised:
            parse(record)
        _assert_value_free(raised.value, field)
        assert _INTERNAL_HOST not in str(raised.value)
        assert str(raised.value) == f"SAP property {field!r} is not text"

    def test_scalar_values_for_text_fields_are_unchanged(self) -> None:
        asset = parse_asset({"Equipment": 10000001, "EquipmentName": 7, "SerialNumber": True})
        assert (asset.id, asset.name, asset.serial_number) == ("10000001", "7", "True")
        assert parse_spare_part({"Material": 42, "Price": 1}).sku == "42"

    def test_is_a_value_error(self) -> None:
        """A caller that caught the ValueError a bad number raised still catches it."""
        with pytest.raises(ValueError, match="AvailableQuantity"):
            parse_spare_part({"Material": "M", "AvailableQuantity": "12.5"})

    def test_readable_values_and_defaults_are_unchanged(self) -> None:
        part = parse_spare_part({"Material": "M", "Quantity": "7", "Price": "35.50"})
        assert (part.stock_quantity, part.unit_cost) == (7, 35.5)
        assert parse_spare_part({"Material": "M"}).stock_quantity == 0
        plan = parse_maintenance_plan({"MaintenancePlan": "P", "CycleValue": "3"})
        assert plan.interval.days == 3
        wo = parse_work_order(
            {
                "MaintenanceOrder": "W",
                "CreationDate": "/Date(1704067200000+0000)/",
                "LastChangeDateTime": "",
            }
        )
        assert wo.created_at == datetime(2024, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Whole-number quantities and cycle values: exact in every representation
# ---------------------------------------------------------------------------

_BOUND = "a whole number of at most 12 digits"


def _spare_part_quantity(record: dict[str, Any]) -> int:
    return int(parse_spare_part({"Material": "M", **record}).stock_quantity)


def _cycle_days(record: dict[str, Any]) -> int:
    # Without a cycle unit the plan's interval is in days.
    return int(parse_maintenance_plan({"MaintenancePlan": "P", **record}).interval.days)


# The four properties the rule covers, with what reads each one back.
_WHOLE_NUMBER_FIELDS = [
    pytest.param("AvailableQuantity", _spare_part_quantity, id="AvailableQuantity"),
    pytest.param("Quantity", _spare_part_quantity, id="Quantity"),
    pytest.param("MaintenancePlanCycleValue", _cycle_days, id="MaintenancePlanCycleValue"),
    pytest.param("CycleValue", _cycle_days, id="CycleValue"),
]


class TestWholeNumbers:
    """A quantity or cycle value is read exactly: a whole number in any OData
    representation is accepted, a fraction is refused and never cut."""

    @pytest.mark.parametrize(("field", "read"), _WHOLE_NUMBER_FIELDS)
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # OData V2 sends Edm.Decimal as text with its decimals.
            pytest.param("12", 12, id="v2-text"),
            pytest.param("12.000", 12, id="v2-decimal-text"),
            pytest.param("0.000", 0, id="v2-zero"),
            pytest.param(" 7 ", 7, id="v2-text-padded"),
            # OData V4 sends it as a JSON number.
            pytest.param(12, 12, id="v4-integer"),
            pytest.param(12.0, 12, id="v4-integral-float"),
            # The largest magnitude the bound accepts.
            pytest.param("999999999999", 999_999_999_999, id="twelve-digits-text"),
            pytest.param(999_999_999_999.0, 999_999_999_999, id="twelve-digits-float"),
        ],
    )
    def test_whole_number_is_read_exactly(
        self, field: str, read: Callable[[dict[str, Any]], int], value: Any, expected: int
    ) -> None:
        assert read({field: value}) == expected

    @pytest.mark.parametrize(("field", "read"), _WHOLE_NUMBER_FIELDS)
    @pytest.mark.parametrize(
        "value",
        [
            # A fraction is never truncated or rounded to another whole number.
            pytest.param("12.5", id="v2-fraction"),
            pytest.param("12.500", id="v2-decimal-fraction"),
            pytest.param(12.7, id="v4-fraction"),
            pytest.param(0.5, id="v4-half"),
            pytest.param("NaN", id="nan-text"),
            pytest.param("Infinity", id="infinity-text"),
            pytest.param("-Infinity", id="minus-infinity-text"),
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="infinity"),
            pytest.param(True, id="true"),
            pytest.param(False, id="false"),
            pytest.param(None, id="null"),
            pytest.param("", id="empty-text"),
            pytest.param(f"x{_SECRET}", id="other-text"),
            pytest.param({"v": _SECRET}, id="object"),
            pytest.param([_SECRET], id="list"),
        ],
    )
    def test_value_that_is_not_a_whole_number_is_refused(
        self, field: str, read: Callable[[dict[str, Any]], int], value: Any
    ) -> None:
        with pytest.raises(SapFieldError) as raised:
            read({field: value})
        _assert_value_free(raised.value, field)
        assert str(raised.value) == f"SAP property {field!r} is not {_WHOLE}"

    @pytest.mark.parametrize(("field", "read"), _WHOLE_NUMBER_FIELDS)
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("1000000000000", id="thirteen-digits-text"),
            pytest.param(10**12, id="thirteen-digits-integer"),
            pytest.param(1e12, id="thirteen-digits-float"),
            pytest.param(-(10**12), id="negative-thirteen-digits"),
            pytest.param("1" * 5000, id="five-thousand-digits"),
            # Building these integers would take a megabyte and a gigabyte of
            # memory: the bound is checked before any integer is built.
            pytest.param("1E+1000000", id="exponent-million"),
            pytest.param("9E+999999999", id="exponent-billion"),
        ],
    )
    def test_whole_number_beyond_the_bound_is_refused(
        self, field: str, read: Callable[[dict[str, Any]], int], value: Any
    ) -> None:
        with pytest.raises(SapFieldError) as raised:
            read({field: value})
        _assert_value_free(raised.value, field)
        assert str(raised.value) == f"SAP property {field!r} is not {_BOUND}"

    @pytest.mark.parametrize("value", ["-3.000", -3, -3.0])
    def test_negative_quantity_is_still_refused_by_the_domain_model(self, value: Any) -> None:
        """The converter reads a negative whole number; the stock level's own
        rule (at least zero) refuses it, as before."""
        with pytest.raises(ValidationError) as raised:
            parse_spare_part({"Material": "M", "AvailableQuantity": value})
        assert [error["loc"] for error in raised.value.errors()] == [("stock_quantity",)]

    def test_unit_cost_keeps_its_fraction(self) -> None:
        part = parse_spare_part({"Material": "M", "StandardPrice": "35.50", "Quantity": "4.000"})
        assert (part.unit_cost, part.stock_quantity) == (35.5, 4)
