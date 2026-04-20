"""Integration tests for Italian informal text entity resolution.

Validates the EntityResolver against 20+ real-world-ish Italian inputs
that technicians would send via email or Telegram. Tests the rule-based
resolver against the PMI-Italia sample asset registry from the
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
REGISTRY_PATH = TEMPLATE_DIR / "data" / "asset_registry.json"


@pytest.fixture(scope="module")
def plant() -> Plant:
    """Build a Plant loaded with the template's asset registry."""
    p = Plant(name="Stabilimento Demo", location="Italia")
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
def resolver(plant: Plant) -> EntityResolver:
    return EntityResolver(plant)


class TestExactIdMatch:
    """Exact asset ID embedded in Italian text."""

    def test_pump_p201(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("pompa P-201 perde acqua")
        assert any(r.asset.id == "P-201" for r in results)
        assert results[0].confidence == 1.0

    def test_boiler_c3(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("caldaia C-3 rumore anomalo")
        assert any(r.asset.id == "C-3" for r in results)

    def test_motor_me15(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("motore ME-15 scalda troppo")
        assert any(r.asset.id == "ME-15" for r in results)

    def test_compressor_cp101(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("compressore CP-101 pressione bassa")
        assert any(r.asset.id == "CP-101" for r in results)

    def test_multiple_ids_in_one_message(self, resolver: EntityResolver) -> None:
        results = resolver.resolve(
            "pompa P-201 perde acqua, caldaia C-3 rumore anomalo, prego creare OdL"
        )
        ids = {r.asset.id for r in results}
        assert "P-201" in ids
        assert "C-3" in ids

    def test_plc_01(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("PLC-01 non comunica con SCADA")
        assert any(r.asset.id == "PLC-01" for r in results)

    def test_ups_01(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("UPS-01 allarme batteria")
        assert any(r.asset.id == "UPS-01" for r in results)

    def test_chiller_cl201(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("chiller CL-201 non raffredda")
        assert any(r.asset.id == "CL-201" for r in results)

    def test_transformer_tr401(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("trasformatore TR-401 perdita olio")
        assert any(r.asset.id == "TR-401" for r in results)

    def test_heat_exchanger_sc501(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("scambiatore SC-501 incrostato")
        assert any(r.asset.id == "SC-501" for r in results)


class TestNameKeywordMatch:
    """Name-based matching from Italian informal descriptions."""

    def test_nastro_trasportatore_linea_3(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("il nastro trasportatore in linea 3 si è fermato")
        assert any(r.asset.id == "ME-15" for r in results)

    def test_pompa_centrifuga_raffreddamento(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("la pompa centrifuga del raffreddamento vibra")
        matching = [r for r in results if r.asset.id in ("P-201", "P-202")]
        assert len(matching) > 0

    def test_caldaia_vapore(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("la caldaia a vapore fa rumore")
        assert any(r.asset.id == "C-3" for r in results)

    def test_compressore_aria(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("il compressore aria principale perde pressione")
        matching = [r for r in results if r.asset.id in ("CP-101", "CP-102")]
        assert len(matching) > 0

    def test_ventilatore_cabina_verniciatura(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("ventilatore della cabina verniciatura")
        assert any(r.asset.id == "VE-301" for r in results)

    def test_carroponte(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("il carroponte del reparto montaggio")
        assert any(r.asset.id == "GRU-01" for r in results)


class TestLocationMatch:
    """Location-based resolution from informal site references."""

    def test_edificio_b_centrale_termica(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("problema nella centrale termica edificio B")
        ids = {r.asset.id for r in results}
        assert ids & {"C-3", "C-4"}

    def test_magazzino_linea_3(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("guasto al magazzino linea 3")
        assert any(r.asset.id == "ME-15" for r in results)

    def test_sala_compressori(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("problema in sala compressori")
        ids = {r.asset.id for r in results}
        assert ids & {"CP-101", "CP-102"}


class TestEdgeCases:
    """Edge cases: unknown assets, empty text, ambiguous references."""

    def test_unknown_asset_returns_empty(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("pompa X-999 perde olio")
        assert not any(r.asset.id == "X-999" for r in results)

    def test_empty_text(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("")
        assert results == []

    def test_no_asset_reference(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("buongiorno, a che ora chiude la mensa?")
        assert len(results) == 0 or all(r.confidence < 0.5 for r in results)

    def test_multiple_pumps_ambiguous(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("una pompa centrifuga perde")
        matching = [r for r in results if r.asset.id in ("P-201", "P-202")]
        assert len(matching) >= 2

    def test_case_insensitive(self, resolver: EntityResolver) -> None:
        results = resolver.resolve("POMPA p-201 PERDE ACQUA")
        assert any(r.asset.id == "P-201" for r in results)
