"""Tests for the EntityResolver."""

from __future__ import annotations

from machina.agent.entity_resolver import EntityResolver, ResolvedEntity
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant


def _make_plant() -> Plant:
    """Create a plant with sample assets for testing."""
    plant = Plant(name="Test Plant")
    plant.register_asset(
        Asset(
            id="P-201",
            name="Cooling Water Pump",
            type=AssetType.ROTATING_EQUIPMENT,
            location="Building A / Line 2 / Cooling System",
            manufacturer="Grundfos",
            model="CR 32-2",
            criticality=Criticality.A,
        )
    )
    plant.register_asset(
        Asset(
            id="COMP-301",
            name="Air Compressor Unit 1",
            type=AssetType.ROTATING_EQUIPMENT,
            location="Building B / Utilities / Compressed Air",
            manufacturer="Atlas Copco",
            model="GA 55",
            criticality=Criticality.A,
        )
    )
    plant.register_asset(
        Asset(
            id="HX-401",
            name="Process Heat Exchanger",
            type=AssetType.STATIC_EQUIPMENT,
            location="Building A / Line 2 / Thermal System",
            manufacturer="Alfa Laval",
            model="M10-BFG",
            criticality=Criticality.B,
        )
    )
    return plant


class TestEntityResolver:
    """Test entity resolution strategies."""

    def test_exact_id_match(self) -> None:
        resolver = EntityResolver(_make_plant())
        results = resolver.resolve("Tell me about P-201")
        assert len(results) >= 1
        assert results[0].asset.id == "P-201"
        assert results[0].confidence == 1.0
        assert results[0].match_reason == "exact_id"

    def test_name_match(self) -> None:
        resolver = EntityResolver(_make_plant())
        results = resolver.resolve("How do I maintain the cooling water pump?")
        assert len(results) >= 1
        assert results[0].asset.id == "P-201"
        assert results[0].match_reason == "name_match"

    def test_name_keywords(self) -> None:
        resolver = EntityResolver(_make_plant())
        results = resolver.resolve("What's wrong with the compressor?")
        assert len(results) >= 1
        assert results[0].asset.id == "COMP-301"

    def test_location_match(self) -> None:
        resolver = EntityResolver(_make_plant())
        results = resolver.resolve("What equipment is in building B?")
        assert len(results) >= 1
        # COMP-301 is in Building B
        assert any(r.asset.id == "COMP-301" for r in results)

    def test_no_match(self) -> None:
        resolver = EntityResolver(_make_plant())
        results = resolver.resolve("hello")
        # Very short query, might not match anything
        # or might match on keyword — just ensure no crash
        assert isinstance(results, list)

    def test_empty_plant(self) -> None:
        resolver = EntityResolver(Plant(name="Empty"))
        results = resolver.resolve("P-201")
        assert results == []

    def test_resolve_best(self) -> None:
        resolver = EntityResolver(_make_plant())
        asset = resolver.resolve_best("Tell me about P-201")
        assert asset is not None
        assert asset.id == "P-201"

    def test_resolve_best_no_match(self) -> None:
        resolver = EntityResolver(Plant(name="Empty"))
        assert resolver.resolve_best("something") is None

    def test_name_keywords_partial_match(self) -> None:
        """Test partial name keyword match with score < 1.0."""
        resolver = EntityResolver(_make_plant())
        # "cooling pump" matches 2 out of 3 significant words in "Cooling Water Pump"
        results = resolver.resolve("check the cooling pump")
        # Should find P-201 via keyword overlap
        pump_results = [r for r in results if r.asset.id == "P-201"]
        assert len(pump_results) >= 1
        assert pump_results[0].match_reason in ("name_keywords", "name_match")

    def test_manufacturer_keyword_match(self) -> None:
        resolver = EntityResolver(_make_plant())
        results = resolver.resolve("Where is the Grundfos pump?")
        assert len(results) >= 1
        # Should match P-201 (Grundfos) via keyword
        assert any(r.asset.id == "P-201" for r in results)


class TestResolvedEntity:
    """Test ResolvedEntity representation."""

    def test_repr(self) -> None:
        asset = Asset(
            id="P-201",
            name="Test",
            type=AssetType.ROTATING_EQUIPMENT,
        )
        entity = ResolvedEntity(asset, confidence=0.9, match_reason="test")
        assert "P-201" in repr(entity)
        assert "0.90" in repr(entity)
