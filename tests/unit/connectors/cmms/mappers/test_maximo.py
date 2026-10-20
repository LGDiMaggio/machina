"""Unit tests for the Maximo mapper — pure ``dict`` → Entity conversions."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

import pytest

from machina.connectors.cmms.mappers.maximo import (
    ASSET_SELECT,
    IDENTITY_ATTRIBUTE,
    INVENTORY_SELECT,
    PM_SELECT,
    WORK_ORDER_SELECT,
    parse_asset,
    parse_datetime,
    parse_maintenance_plan,
    parse_spare_part,
    parse_work_order,
    resolve_asset_type,
    reverse_priority,
    reverse_status,
    reverse_worktype,
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
        asset = parse_asset({"assetnum": "A-1", "description": "Pump"})
        assert asset.id == "A-1"
        assert asset.name == "Pump"
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_empty_dict_raises_validation_error(self) -> None:
        """Empty input has no assetnum → Asset pydantic validator rejects."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="id cannot be empty"):
            parse_asset({})

    def test_asset_type_map_honoured(self) -> None:
        type_map = {"PUMPS": AssetType.ROTATING_EQUIPMENT, "VESSELS": AssetType.STATIC_EQUIPMENT}
        asset = parse_asset({"assetnum": "V-1", "classstructureid": "VESSELS"}, type_map)
        assert asset.type == AssetType.STATIC_EQUIPMENT

    def test_asset_type_map_fallback_to_rotating(self) -> None:
        type_map = {"PUMPS": AssetType.ROTATING_EQUIPMENT}
        asset = parse_asset({"assetnum": "X-1", "classstructureid": "UNKNOWN"}, type_map)
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_priority_1_is_critical(self) -> None:
        asset = parse_asset({"assetnum": "A-2", "priority": 1})
        assert asset.criticality == Criticality.A


class TestParseWorkOrderPublicAPI:
    def test_status_inprg_maps_to_in_progress(self) -> None:
        wo = parse_work_order({"wonum": "W-1", "status": "INPRG"})
        assert wo.status == WorkOrderStatus.IN_PROGRESS

    def test_priority_non_numeric_falls_back_to_medium(self) -> None:
        """Non-numeric ``wopriority`` must not raise — fallback to MEDIUM."""
        wo = parse_work_order({"wonum": "W-2", "wopriority": "garbage"})
        assert wo.priority == Priority.MEDIUM

    def test_missing_priority_defaults_to_medium(self) -> None:
        wo = parse_work_order({"wonum": "W-3"})
        assert wo.priority == Priority.MEDIUM

    def test_worktype_pm_maps_to_preventive(self) -> None:
        wo = parse_work_order({"wonum": "W-4", "worktype": "PM"})
        assert wo.type == WorkOrderType.PREVENTIVE


class TestResolveAssetType:
    def test_none_map_returns_rotating(self) -> None:
        assert (
            resolve_asset_type({"classstructureid": "anything"}, None)
            == AssetType.ROTATING_EQUIPMENT
        )

    def test_empty_map_returns_rotating(self) -> None:
        assert (
            resolve_asset_type({"classstructureid": "anything"}, {})
            == AssetType.ROTATING_EQUIPMENT
        )

    def test_assettype_used_when_classstructureid_missing(self) -> None:
        m = {"INSTR": AssetType.INSTRUMENT}
        assert resolve_asset_type({"assettype": "INSTR"}, m) == AssetType.INSTRUMENT


class TestParseDatetime:
    def test_iso_with_z(self) -> None:
        dt = parse_datetime("2024-01-01T12:00:00Z")
        assert dt == datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    def test_iso_naive_gets_utc_tzinfo(self) -> None:
        dt = parse_datetime("2024-01-01T12:00:00")
        assert dt.tzinfo == UTC


class TestReverseMaps:
    def test_reverse_priority_emergency_is_1(self) -> None:
        assert reverse_priority(Priority.EMERGENCY) == 1

    def test_reverse_priority_low_is_4(self) -> None:
        assert reverse_priority(Priority.LOW) == 4

    def test_reverse_worktype_predictive_is_cp(self) -> None:
        assert reverse_worktype(WorkOrderType.PREDICTIVE) == "CP"

    def test_reverse_status_cancelled_is_can(self) -> None:
        assert reverse_status(WorkOrderStatus.CANCELLED) == "CAN"


# ---------------------------------------------------------------------------
# oslc.select: what each read asks Maximo for
# ---------------------------------------------------------------------------


def _recording(value: Any, path: str, asked: set[str]) -> Any:
    """Wrap a member's nested objects and rows so their reads are recorded too."""
    if isinstance(value, dict):
        return _RecordingMember(value, prefix=f"{path}.", asked=asked)
    if isinstance(value, list):
        return [_recording(row, path, asked) for row in value]
    return value


class _RecordingMember(dict[str, Any]):
    """A member that records the attributes a mapper asks it for, as dotted
    paths: ``item.description``, ``invbalances.curbal`` (any row)."""

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        *,
        prefix: str = "",
        asked: set[str] | None = None,
    ) -> None:
        super().__init__(data or {})
        self.prefix = prefix
        self.asked: set[str] = set() if asked is None else asked

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self:
            self.asked.add(self.prefix + key)
            return default
        return self[key]

    def __getitem__(self, key: str) -> Any:
        path = self.prefix + key
        self.asked.add(path)
        return _recording(super().__getitem__(key), path, self.asked)


def _selected_paths(select: str) -> set[str]:
    """The attribute paths an oslc.select asks for: ``invbalances{curbal}``
    is ``invbalances.curbal``, ``item.description`` is itself."""
    paths: set[str] = set()
    depth = 0
    current = ""
    for char in select + ",":
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "," and depth == 0:
            name, _, children = current.partition("{")
            if children:
                paths |= {f"{name}.{child}" for child in children.rstrip("}").split(",")}
            else:
                paths.add(current)
            current = ""
            continue
        current += char
    return paths


def _leaves(paths: set[str]) -> set[str]:
    """Drop the paths of objects whose attributes were read (``item`` when
    ``item.description`` was)."""
    return {p for p in paths if not any(q.startswith(f"{p}.") for q in paths)}


# IBM-shaped records (lean=1, oslc.select): every selected attribute set, plus
# the keys OSLC adds to each member.
_ASSET = {
    "assetnum": "PUMP-201",
    "siteid": "BEDFORD",
    "description": "Centrifugal pump",
    "location": "BR300",
    "manufacturer": "GRUNDFOS",
    "serialnum": "SN-1",
    "priority": 1,
    "parent": "LINE-2",
    "classstructureid": "1020",
    "assettype": "PUMP",
}
_WORK_ORDER = {
    "wonum": "WO-1",
    "siteid": "BEDFORD",
    "description": "Replace seal",
    "status": "INPRG",
    "status_description": "In Progress",
    "wopriority": 2,
    "worktype": "CM",
    "assetnum": "PUMP-201",
    "lead": "JSMITH",
    "assignedownergroup": "MECH",
    "failurecode": "PUMPS",
    "problemcode": "LEAK",
    "reportdate": "2026-10-01T08:00:00+00:00",
    "changedate": "2026-10-02T09:30:00+00:00",
}
_INVENTORY = {
    "itemnum": "BRG-6205",
    "siteid": "BEDFORD",
    "location": "CENTRAL",
    "minlevel": 5,
    "item": {"description": "Bearing SKF 6205"},
    "invbalances": [{"curbal": 3}, {"curbal": 4.0}],
    "invcost": [{"avgcost": 12.5, "lastcost": 13.0}],
}
_PM = {
    "pmnum": "PM-1",
    "siteid": "BEDFORD",
    "assetnum": "PUMP-201",
    "description": "Quarterly inspection",
    "frequency": 3,
    "frequnit": "MONTHS",
    "status": "ACTIVE",
}
_REST_ID = "_V08tMQ--"
_INTERNAL = "http://maximo-internal:9080/maximo/oslc"
# What OSLC adds to a member besides the selected attributes. A full resource
# read (the update read-back) also carries link objects such as doclinks and,
# when the record has an image, _imagelibref (IBM's REST API guide).
_OSLC_KEYS = {
    "href": f"{_INTERNAL}/os/mxwo/{_REST_ID}",
    "_rowstamp": "3277171",
    "localref": f"{_INTERNAL}/os/mxwo/{_REST_ID}",
    "doclinks_collectionref": f"{_INTERNAL}/os/mxwo/{_REST_ID}/doclinks",
    "doclinks": {"href": f"{_INTERNAL}/os/mxwo/{_REST_ID}/doclinks"},
    "_imagelibref": f"{_INTERNAL}/images/123",
}


def _parse_asset_with_a_type_map(data: dict[str, Any]) -> Any:
    """Without a type map, the asset type is not read at all."""
    return parse_asset(data, {"1020": AssetType.ROTATING_EQUIPMENT})


def _asked(parse: Any, record: dict[str, Any]) -> set[str]:
    """The attribute paths a mapper asks for, over an empty and a full record:
    fields read through ``a or b`` reach ``b`` only when ``a`` is empty. The
    OSLC ``href`` every member carries is not an attribute and is left out."""
    asked: set[str] = set()
    for member in (_RecordingMember(), _RecordingMember(record)):
        with contextlib.suppress(ValueError):  # the empty record has no identity
            parse(member)
        asked |= member.asked
    return _leaves(asked) - {"href"}


_PARSERS = [
    pytest.param(_parse_asset_with_a_type_map, ASSET_SELECT, _ASSET, {"modelnum"}, id="asset"),
    pytest.param(parse_work_order, WORK_ORDER_SELECT, _WORK_ORDER, {"failureremark"}, id="wo"),
    # The older flat inventory shape is still read when the children are absent.
    pytest.param(
        parse_spare_part,
        INVENTORY_SELECT,
        _INVENTORY,
        {"description", "curbal", "avgcost", "lastcost"},
        id="inventory",
    ),
    pytest.param(parse_maintenance_plan, PM_SELECT, _PM, set(), id="pm"),
]


class TestSelections:
    @pytest.mark.parametrize(("parse", "select", "record", "fallbacks"), _PARSERS)
    def test_selection_names_every_attribute_the_mapper_reads(
        self, parse, select: str, record: dict[str, Any], fallbacks: set[str]
    ) -> None:
        """A mapped attribute that is not selected would always read as empty,
        a child or related one included."""
        assert _asked(parse, record) - fallbacks <= _selected_paths(select)

    @pytest.mark.parametrize(("parse", "select", "record", "fallbacks"), _PARSERS)
    def test_every_selected_attribute_is_read(
        self, parse, select: str, record: dict[str, Any], fallbacks: set[str]
    ) -> None:
        """Nothing is fetched that no field uses, except siteid, part of the key."""
        assert _selected_paths(select) - _asked(parse, record) <= {"siteid"}

    def test_paths_of_child_and_related_selections(self) -> None:
        assert _selected_paths(INVENTORY_SELECT) == {
            "itemnum",
            "siteid",
            "location",
            "minlevel",
            "item.description",
            "invbalances.curbal",
            "invcost.avgcost",
            "invcost.lastcost",
        }

    def test_an_unselected_child_attribute_is_seen(self) -> None:
        """The recorder sees reads below the top level."""

        def reads_a_bin_number(data: dict[str, Any]) -> Any:
            return [row.get("binnum") for row in data.get("invbalances") or []]

        assert _asked(reads_a_bin_number, {"invbalances": [{"curbal": 1}]}) == {
            "invbalances.binnum"
        }

    @pytest.mark.parametrize(
        ("object_structure", "select"),
        [
            ("mxasset", ASSET_SELECT),
            ("mxwo", WORK_ORDER_SELECT),
            ("mxinventory", INVENTORY_SELECT),
            ("mxpm", PM_SELECT),
        ],
    )
    def test_every_selection_names_its_identity(self, object_structure: str, select: str) -> None:
        """A read refuses a member without it, so it must be asked for."""
        assert IDENTITY_ATTRIBUTE[object_structure] in _selected_paths(select)


class TestOslcKeysStayOutOfMetadata:
    """href, localref, _imagelibref, *_collectionref and link objects name
    the host Maximo sees itself on, often an internal one, and metadata
    reaches the LLM and MCP clients."""

    @pytest.mark.parametrize(
        ("parse", "record"),
        [
            pytest.param(parse_asset, _ASSET, id="asset"),
            pytest.param(parse_work_order, _WORK_ORDER, id="wo"),
            pytest.param(parse_spare_part, _INVENTORY, id="inventory"),
        ],
    )
    def test_transport_keys_are_dropped_and_record_attributes_kept(
        self, parse, record: dict[str, Any]
    ) -> None:
        entity = parse({**record, **_OSLC_KEYS})
        assert not set(_OSLC_KEYS) & set(entity.metadata)
        assert entity.metadata["siteid"] == "BEDFORD"
        assert "maximo-internal" not in str(entity.metadata)

    def test_unmapped_record_attributes_stay(self) -> None:
        wo = parse_work_order({**_WORK_ORDER, **_OSLC_KEYS})
        assert wo.metadata == {
            "siteid": "BEDFORD",
            "status_description": "In Progress",
            "rest_id": _REST_ID,
        }

    def test_the_rest_id_is_kept_without_the_host(self) -> None:
        """The rest id addresses the record, for maximo_raw_attribute_update."""
        asset = parse_asset({**_ASSET, "href": f"{_INTERNAL}/os/mxasset/_QkVERk9SRC8xMDAw"})
        assert asset.metadata["rest_id"] == "_QkVERk9SRC8xMDAw"

    def test_an_escaped_rest_id_is_decoded(self) -> None:
        asset = parse_asset({**_ASSET, "href": f"{_INTERNAL}/os/mxasset/_QkVE%2DRk9S"})
        assert asset.metadata["rest_id"] == "_QkVE-Rk9S"

    @pytest.mark.parametrize(
        "href",
        [
            pytest.param(f"{_INTERNAL}/os/mxasset/", id="no-last-segment"),
            pytest.param(f"{_INTERNAL}/os/mxasset/..", id="dot-segment"),
            pytest.param(f"{_INTERNAL}/os/mxasset/a%2Fb", id="escaped-separator"),
            pytest.param(f"{_INTERNAL}/os/mxasset/%FF", id="not-utf8"),
            pytest.param("http://[x", id="unparseable"),
            pytest.param(42, id="not-text"),
        ],
    )
    def test_no_rest_id_that_could_leave_its_segment(self, href: object) -> None:
        asset = parse_asset({**_ASSET, "href": href})
        assert "rest_id" not in asset.metadata

    def test_no_href_no_rest_id(self) -> None:
        assert "rest_id" not in parse_asset(_ASSET).metadata


class TestParseAssetCriticality:
    def test_no_priority_is_criticality_c(self) -> None:
        """Maximo leaves a null priority out; it is not priority 0 (A)."""
        record = {k: v for k, v in _ASSET.items() if k != "priority"}
        assert parse_asset(record).criticality == Criticality.C


class TestParseSparePartOslcShape:
    def test_ibm_shaped_record(self) -> None:
        part = parse_spare_part({**_INVENTORY, **_OSLC_KEYS})
        assert part.sku == "BRG-6205"
        assert part.name == "Bearing SKF 6205"
        assert part.stock_quantity == 7  # the sum of the bin balances
        assert part.reorder_point == 5  # minlevel
        assert part.unit_cost == 12.5  # avgcost of the first cost row
        assert part.warehouse_location == "CENTRAL"
        assert part.metadata == {"siteid": "BEDFORD", "rest_id": _REST_ID}

    def test_last_cost_when_there_is_no_average(self) -> None:
        part = parse_spare_part({**_INVENTORY, "invcost": [{"lastcost": 9.0}]})
        assert part.unit_cost == 9.0

    def test_first_cost_row_is_used(self) -> None:
        part = parse_spare_part({**_INVENTORY, "invcost": [{"avgcost": 2.0}, {"avgcost": 8.0}]})
        assert part.unit_cost == 2.0

    def test_no_balance_rows_is_no_stock(self) -> None:
        part = parse_spare_part({**_INVENTORY, "invbalances": []})
        assert part.stock_quantity == 0

    def test_no_cost_rows_falls_back_to_the_record(self) -> None:
        part = parse_spare_part({**_INVENTORY, "invcost": [], "avgcost": 2.0})
        assert part.unit_cost == 2.0

    def test_the_item_description_wins_over_the_record(self) -> None:
        part = parse_spare_part({**_INVENTORY, "description": "Flat"})
        assert part.name == "Bearing SKF 6205"

    def test_no_item_description_falls_back_to_the_record(self) -> None:
        record = {k: v for k, v in _INVENTORY.items() if k != "item"}
        assert parse_spare_part({**record, "description": "Flat"}).name == "Flat"
        assert parse_spare_part(record).name == ""

    def test_an_item_that_is_not_an_object_falls_back_to_the_record(self) -> None:
        record = {**_INVENTORY, "item": [{"description": "In a list"}], "description": "Flat"}
        assert parse_spare_part(record).name == "Flat"

    def test_fractions_are_dropped(self) -> None:
        """Quantities are whole units; Maximo can hold 2.5 litres in a bin."""
        part = parse_spare_part(
            {**_INVENTORY, "invbalances": [{"curbal": 2.5}, {"curbal": 0.7}], "minlevel": 1.9}
        )
        assert (part.stock_quantity, part.reorder_point) == (3, 1)

    def test_minlevel_as_decimal_text(self) -> None:
        assert parse_spare_part({**_INVENTORY, "minlevel": "5.00"}).reorder_point == 5

    def test_negative_balance_is_no_stock(self) -> None:
        """An organization can allow negative balances; a SparePart cannot hold one."""
        part = parse_spare_part({**_INVENTORY, "invbalances": [{"curbal": 2}, {"curbal": -5}]})
        assert part.stock_quantity == 0
        assert part.needs_reorder
        assert part.metadata["negative_balance"] == -3.0

    def test_negative_cost_is_no_cost(self) -> None:
        part = parse_spare_part({**_INVENTORY, "invcost": [{"avgcost": -0.5}]})
        assert part.unit_cost == 0.0
        assert part.metadata["negative_unit_cost"] == -0.5

    def test_no_negative_figures_no_extra_metadata(self) -> None:
        part = parse_spare_part(_INVENTORY)
        assert not {"negative_balance", "negative_unit_cost"} & set(part.metadata)

    def test_reorder_flag_is_not_a_reorder_point(self) -> None:
        part = parse_spare_part({"itemnum": "X", "reorder": True})
        assert part.reorder_point == 0
        assert part.metadata["reorder"] is True


class TestParseMaintenancePlanFrequencyUnit:
    @pytest.mark.parametrize(
        ("unit", "interval"),
        [
            ("DAYS", Interval(days=3)),
            ("WEEKS", Interval(weeks=3)),
            ("MONTHS", Interval(months=3)),
            ("YEARS", Interval(months=36)),
            ("months", Interval(months=3)),
            (" Weeks ", Interval(weeks=3)),
        ],
    )
    def test_frequency_counts_units(self, unit: str, interval: Interval) -> None:
        plan = parse_maintenance_plan({**_PM, "frequnit": unit})
        assert plan.interval == interval

    def test_no_unit_gives_no_calendar_interval(self) -> None:
        """Maximo leaves a null frequnit out; "3" alone is not 3 days."""
        record = {k: v for k, v in _PM.items() if k != "frequnit"}
        assert parse_maintenance_plan(record).interval == Interval()

    @pytest.mark.parametrize("unit", ["", "  ", None])
    def test_empty_unit_gives_no_calendar_interval(self, unit: str | None) -> None:
        assert parse_maintenance_plan({**_PM, "frequnit": unit}).interval == Interval()

    def test_unknown_unit_gives_no_calendar_interval(self) -> None:
        """Better no due date than a wrong one: "3 FORTNIGHT" is not 3 days."""
        plan = parse_maintenance_plan({**_PM, "frequnit": "FORTNIGHT"})
        assert plan.interval == Interval()
        assert plan.interval.total_days == 0

    def test_status(self) -> None:
        assert parse_maintenance_plan(_PM).active is True
        assert parse_maintenance_plan({**_PM, "status": "INACTIVE"}).active is False
