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


def _plant_with_ids(*ids: str) -> Plant:
    """Build a plant whose assets carry the given IDs and nothing distinctive else."""
    plant = Plant(name="ID Plant")
    for asset_id in ids:
        plant.register_asset(
            Asset(
                id=asset_id,
                name=f"Asset {asset_id}",
                type=AssetType.ROTATING_EQUIPMENT,
            )
        )
    return plant


def _exact_ids(results: list[ResolvedEntity]) -> list[str]:
    """IDs of the results that matched at stage 1 (exact_id)."""
    return [r.asset.id for r in results if r.match_reason == "exact_id"]


class TestExactIdAnchoring:
    """Stage-1 ID matching is anchored to word boundaries.

    Raw substring containment made ``P-2`` match inside ``P-201`` and return it
    at confidence 1.0 — a wrong asset presented as the definitive referent.
    These tests pin the anchoring without over-tightening it (see
    ``docs/solutions/logic-errors/asset-id-inference-too-strict-2026-05-15.md``).
    """

    def test_happy_path_id_in_sentence(self) -> None:
        resolver = EntityResolver(_make_plant())
        results = resolver.resolve("Tell me about P-201 please")
        assert _exact_ids(results) == ["P-201"]
        assert results[0].confidence == 1.0

    def test_longer_id_does_not_match_shorter_registered_id(self) -> None:
        resolver = EntityResolver(_plant_with_ids("P-201", "P-2010"))
        results = resolver.resolve("P-2010 is leaking")
        assert _exact_ids(results) == ["P-2010"]

    def test_shorter_registered_id_not_matched_inside_longer_reference(self) -> None:
        """Only P-2 exists; the user asks about P-201 — P-2 is not the referent."""
        resolver = EntityResolver(_plant_with_ids("P-2"))
        results = resolver.resolve("qual e lo stato di P-201?")
        assert _exact_ids(results) == []

    def test_id_at_start_of_text(self) -> None:
        resolver = EntityResolver(_make_plant())
        assert _exact_ids(resolver.resolve("P-201 is down")) == ["P-201"]

    def test_id_at_end_of_text(self) -> None:
        resolver = EntityResolver(_make_plant())
        assert _exact_ids(resolver.resolve("please check P-201")) == ["P-201"]

    def test_id_is_entire_text(self) -> None:
        resolver = EntityResolver(_make_plant())
        assert _exact_ids(resolver.resolve("P-201")) == ["P-201"]

    def test_id_followed_by_punctuation(self) -> None:
        resolver = EntityResolver(_make_plant())
        assert _exact_ids(resolver.resolve("P-201, che perde")) == ["P-201"]

    def test_id_inside_parentheses(self) -> None:
        resolver = EntityResolver(_make_plant())
        assert _exact_ids(resolver.resolve("the pump (P-201) is noisy")) == ["P-201"]

    def test_id_case_insensitive(self) -> None:
        resolver = EntityResolver(_make_plant())
        assert _exact_ids(resolver.resolve("controlla p-201 subito")) == ["P-201"]

    def test_id_with_regex_metacharacter_matches_literally(self) -> None:
        """An ID containing regex metacharacters is escaped, not interpreted."""
        resolver = EntityResolver(_plant_with_ids("P.201"))
        assert _exact_ids(resolver.resolve("P.201 is leaking")) == ["P.201"]
        # '.' must not act as a wildcard matching '0'
        assert _exact_ids(resolver.resolve("P0201 is leaking")) == []

    def test_every_registered_id_resolves_to_itself_only(self) -> None:
        """Over-tightening guard: each ID still resolves, and unambiguously."""
        plant = _make_plant()
        resolver = EntityResolver(plant)
        for asset in plant.list_assets():
            results = resolver.resolve(asset.id)
            assert _exact_ids(results) == [asset.id], f"{asset.id} regressed"


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
