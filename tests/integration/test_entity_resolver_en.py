"""Integration tests for English informal text entity resolution.

Validates the EntityResolver against 20+ English inputs that technicians
would send via email or Telegram. Tests the rule-based resolver against
the English sample asset registry (asset_registry_en.json) from the
odl-generator-from-text template.

Target: >90% accuracy (<2 false resolutions out of 20+).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from machina.agent.entity_resolver import EntityResolver
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant

TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "templates" / "odl-generator-from-text"
)
REGISTRY_PATH = TEMPLATE_DIR / "data" / "asset_registry_en.json"


@pytest.fixture(scope="module")
def plant_en() -> Plant:
    """Build a Plant loaded with the English template asset registry."""
    p = Plant(name="Demo Plant", location="UK")
    with open(REGISTRY_PATH) as f:
        assets_data = json.load(f)
    for a in assets_data:
        asset = Asset(
            id=a["id"],
            name=a["name"],
            type=AssetType(a["type"]),
            location=a.get("location", ""),
            criticality=Criticality(a.get("criticality", "C")),
            manufacturer=a.get("manufacturer", ""),
            model=a.get("model", ""),
            failure_modes=a.get("failure_modes", []),
        )
        p.register_asset(asset)
    return p


@pytest.fixture(scope="module")
def resolver_en(plant_en: Plant) -> EntityResolver:
    """Build an EntityResolver backed by the English plant fixture."""
    return EntityResolver(plant_en)


class TestExactIdMatch:
    """Exact asset ID embedded in English text."""

    def test_pump_p201(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("pump P-201 leaking water")
        assert any(r.asset.id == "P-201" for r in results)
        assert results[0].confidence == 1.0

    def test_boiler_c3(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("boiler C-3 abnormal noise")
        assert any(r.asset.id == "C-3" for r in results)

    def test_motor_me15(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("motor ME-15 overheating")
        assert any(r.asset.id == "ME-15" for r in results)

    def test_compressor_cp101(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("compressor CP-101 low pressure")
        assert any(r.asset.id == "CP-101" for r in results)

    def test_multiple_ids_in_one_message(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve(
            "pump P-201 leaking water, boiler C-3 abnormal noise, please create WO"
        )
        ids = {r.asset.id for r in results}
        assert "P-201" in ids
        assert "C-3" in ids

    def test_plc_01(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("PLC-01 not communicating with SCADA")
        assert any(r.asset.id == "PLC-01" for r in results)

    def test_ups_01(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("UPS-01 battery alarm")
        assert any(r.asset.id == "UPS-01" for r in results)

    def test_chiller_cl201(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("chiller CL-201 not cooling")
        assert any(r.asset.id == "CL-201" for r in results)

    def test_transformer_tr401(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("transformer TR-401 oil leak")
        assert any(r.asset.id == "TR-401" for r in results)

    def test_heat_exchanger_sc501(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("heat exchanger SC-501 fouled")
        assert any(r.asset.id == "SC-501" for r in results)


class TestNameKeywordMatch:
    """Name-based matching from English informal descriptions."""

    def test_conveyor_line_3_motor(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("the conveyor belt on line 3 has stopped")
        assert any(r.asset.id == "ME-15" for r in results)

    def test_centrifugal_pump_cooling(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("the centrifugal pump in the cooling circuit is vibrating")
        matching = [r for r in results if r.asset.id in ("P-201", "P-202")]
        assert len(matching) > 0

    def test_steam_boiler(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("the steam boiler is making noise")
        assert any(r.asset.id == "C-3" for r in results)

    def test_air_compressor(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("the main air compressor is losing pressure")
        matching = [r for r in results if r.asset.id in ("CP-101", "CP-102")]
        assert len(matching) > 0

    def test_exhaust_fan_paint_booth(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("exhaust fan in the paint booth")
        assert any(r.asset.id == "VE-301" for r in results)

    def test_overhead_crane(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("the overhead crane in the assembly department")
        assert any(r.asset.id == "GRU-01" for r in results)


class TestLocationMatch:
    """Location-based resolution from English site references."""

    def test_building_b_boiler_room(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("problem in the boiler room building B")
        ids = {r.asset.id for r in results}
        assert ids & {"C-3", "C-4"}

    def test_warehouse_line_3(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("fault in warehouse line 3")
        assert any(r.asset.id == "ME-15" for r in results)

    def test_compressor_room(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("problem in the compressor room")
        ids = {r.asset.id for r in results}
        assert ids & {"CP-101", "CP-102"}


class TestEdgeCases:
    """Edge cases: unknown assets, empty text, ambiguous references."""

    def test_unknown_asset_returns_empty(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("pump X-999 leaking oil")
        assert not any(r.asset.id == "X-999" for r in results)

    def test_empty_text(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("")
        assert results == []

    def test_no_asset_reference(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("good morning, what time does the canteen close?")
        assert len(results) == 0 or all(r.confidence < 0.5 for r in results)

    def test_multiple_pumps_ambiguous(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("a centrifugal pump is leaking")
        matching = [r for r in results if r.asset.id in ("P-201", "P-202")]
        assert len(matching) >= 2

    def test_case_insensitive(self, resolver_en: EntityResolver) -> None:
        results = resolver_en.resolve("PUMP p-201 LEAKING WATER")
        assert any(r.asset.id == "P-201" for r in results)
