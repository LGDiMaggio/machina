"""Tests for the EntityResolver."""

from __future__ import annotations

import pytest

from machina.agent.entity_resolver import (
    BAND_HIGH,
    BAND_LOW,
    BAND_MID,
    EntityResolver,
    ResolvedEntity,
    _band_for,
    resolution_verdict,
)
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant


def _asset(asset_id: str) -> Asset:
    """A minimal asset for verdict tests, which never inspect asset fields."""
    return Asset(id=asset_id, name=f"Asset {asset_id}", type=AssetType.ROTATING_EQUIPMENT)


def _entity(asset_id: str, confidence: float, match_reason: str) -> ResolvedEntity:
    """A resolution candidate with an explicitly stated confidence."""
    return ResolvedEntity(_asset(asset_id), confidence=confidence, match_reason=match_reason)


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


def _aliased_plant(*pairs: tuple[str, str, list[str]]) -> Plant:
    """A plant of ``(id, name, aliases)`` triples."""
    plant = Plant(name="Test Plant")
    for asset_id, name, aliases in pairs:
        plant.register_asset(
            Asset(
                id=asset_id,
                name=name,
                type=AssetType.ROTATING_EQUIPMENT,
                aliases=aliases,
            )
        )
    return plant


class TestAliasResolution:
    """R6/R7 — curated per-plant synonyms resolve at name-level authority."""

    def test_curated_italian_alias_resolves_an_english_named_asset(self) -> None:
        """AE4 — the whole point: the plant's own word for the machine works."""
        plant = _aliased_plant(("P-201", "Cooling Water Pump", ["pompa acqua raffreddamento"]))
        results = EntityResolver(plant).resolve("la pompa acqua raffreddamento perde")

        assert [r.asset.id for r in results] == ["P-201"]
        assert results[0].match_reason == "alias_match"
        assert results[0].confidence == 0.9

    def test_alias_scores_at_name_authority_not_the_keyword_tail(self) -> None:
        """Stage 2, not stage 4 — where the confidence scale is still coherent."""
        plant = _aliased_plant(("P-201", "Cooling Water Pump", ["bomba"]))
        by_alias = EntityResolver(plant).resolve("controlla la bomba")
        by_name = EntityResolver(plant).resolve("controlla la cooling water pump")

        assert by_alias[0].confidence == by_name[0].confidence

    def test_alias_matching_is_case_insensitive(self) -> None:
        plant = _aliased_plant(("P-201", "Cooling Water Pump", ["Bomba Grande"]))
        results = EntityResolver(plant).resolve("la BOMBA GRANDE fa rumore")

        assert [r.asset.id for r in results] == ["P-201"]

    def test_asset_without_aliases_is_unchanged(self) -> None:
        """No regression: the default empty list behaves exactly as before."""
        plant = _make_plant()
        results = EntityResolver(plant).resolve("the cooling water pump")

        assert [r.asset.id for r in results] == ["P-201"]
        assert results[0].match_reason == "name_match"

    def test_registered_name_still_outranks_nothing_and_wins_alone(self) -> None:
        plant = _aliased_plant(
            ("P-201", "Cooling Water Pump", ["bomba"]),
            ("P-202", "Backup Pump", []),
        )
        results = EntityResolver(plant).resolve("cooling water pump")

        assert results[0].asset.id == "P-201"
        assert results[0].match_reason == "name_match"

    def test_alias_shared_by_two_assets_ties_rather_than_picking_one(self) -> None:
        """Aliases must not bypass ambiguity — the tie reaches U4's gate."""
        plant = _aliased_plant(
            ("P-201", "Cooling Water Pump", ["bomba"]),
            ("P-202", "Fire Water Pump", ["bomba"]),
        )
        results = EntityResolver(plant).resolve("la bomba perde")
        verdict = resolution_verdict(results)

        assert len(results) == 2
        assert results[0].confidence == results[1].confidence
        assert verdict.ambiguous is True

    def test_one_assets_name_matching_anothers_alias_is_ambiguous(self) -> None:
        """A tie across reasons is still a tie — new shape, same gate.

        Aliases sit at name authority precisely so this collides rather than
        letting the weaker signal quietly lose.
        """
        plant = _aliased_plant(
            ("P-201", "Bomba", []),
            ("P-202", "Fire Water Pump", ["bomba"]),
        )
        results = EntityResolver(plant).resolve("la bomba perde")

        assert {r.match_reason for r in results} == {"name_match", "alias_match"}
        assert resolution_verdict(results).ambiguous is True

    def test_exact_id_still_beats_an_alias(self) -> None:
        """Stage 1 returns before stage 2 — an ID in the text is decisive."""
        plant = _aliased_plant(
            ("P-201", "Cooling Water Pump", ["bomba"]),
            ("P-202", "Backup Pump", ["bomba"]),
        )
        results = EntityResolver(plant).resolve("la bomba P-202 perde")

        assert [r.asset.id for r in results] == ["P-202"]
        assert results[0].match_reason == "exact_id"


class TestAliasMatchingIsAnchored:
    """An alias matches as a WHOLE TOKEN, never as a substring.

    Aliases are short shop-floor acronyms by design, which is exactly the shape
    raw containment mangles — and an alias hit lands at 0.9 / high band, so the
    runtime prefetches, states no assumption, and the write gate authorises. The
    positive tests above all pass either way; only these fail without anchoring.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "check the CWPS unit",  # longer acronym, alias is a prefix
            "cwp2 is noisy",  # alias followed by a digit
            "the SCWP line is down",  # alias is a suffix
        ],
    )
    def test_acronym_alias_does_not_match_inside_a_longer_token(self, text: str) -> None:
        plant = _aliased_plant(("P-201", "Cooling Water Pump", ["CWP"]))
        assert EntityResolver(plant).resolve(text) == []

    @pytest.mark.parametrize("text", ["il bombardamento continua", "la bombola è vuota"])
    def test_word_alias_does_not_match_inside_a_longer_word(self, text: str) -> None:
        plant = _aliased_plant(("P-999", "Pompa Olio", ["bomba"]))
        assert EntityResolver(plant).resolve(text) == []

    def test_the_alias_still_matches_when_it_is_a_whole_token(self) -> None:
        """The anchoring must not cost the ordinary hit, punctuation included."""
        plant = _aliased_plant(("P-201", "Cooling Water Pump", ["CWP"]))
        resolver = EntityResolver(plant)

        for text in ["check the CWP", "(CWP) is noisy", "CWP, please", "cwp"]:
            results = resolver.resolve(text)
            assert [r.asset.id for r in results] == ["P-201"], text
            assert results[0].match_reason == "alias_match"


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


def _plant_with_locations(*pairs: tuple[str, str]) -> Plant:
    """Build a plant whose assets differ only in their location string."""
    plant = Plant(name="Location Plant")
    for asset_id, location in pairs:
        plant.register_asset(
            Asset(
                id=asset_id,
                name=f"Asset {asset_id}",
                type=AssetType.ROTATING_EQUIPMENT,
                location=location,
            )
        )
    return plant


class TestLocationTokenGranularity:
    """Stage-3 location matching keeps single-character tokens.

    Real plant locations put the discriminator in one-character tokens — the
    building letter, the floor number, the bay number ("Edificio A / Piano 1 /
    Campata 3"). A ``len(part) > 1`` filter dropped all of them, collapsing
    every such location onto its shared nouns: "Edificio A" and "Edificio B"
    became indistinguishable, so every location query tied and the authority
    gate withheld a commit that no follow-up answer could ever unblock.
    """

    def test_building_letter_separates_otherwise_identical_locations(self) -> None:
        """The pin: two locations differing only by the building letter."""
        plant = _plant_with_locations(
            ("P-201", "Edificio A / Piano 1"),
            ("P-202", "Edificio B / Piano 1"),
        )
        resolver = EntityResolver(plant)
        results = resolver.resolve("controlla la pompa in Edificio B Piano 1")

        assert [r.asset.id for r in results] == ["P-202", "P-201"]
        assert results[0].match_reason == "location_match"
        # Not a tie — this is what the filter destroyed, and an exact tie is
        # exactly what ``resolution_verdict`` classifies as ambiguous.
        assert results[0].confidence > results[1].confidence

    def test_named_building_resolves_instead_of_withholding(self) -> None:
        """The consequence: a location query can actually commit to an asset."""
        plant = _plant_with_locations(
            ("P-201", "Edificio A / Piano 1"),
            ("P-202", "Edificio B / Piano 1"),
        )
        resolver = EntityResolver(plant)
        verdict = resolution_verdict(resolver.resolve("controlla la pompa in Edificio B Piano 1"))

        assert verdict.ambiguous is False
        assert verdict.commits is True

    def test_floor_digit_separates_otherwise_identical_locations(self) -> None:
        """The floor number is a single character too, and equally load-bearing."""
        plant = _plant_with_locations(
            ("C-3", "Edificio B / Piano 0"),
            ("C-4", "Edificio B / Piano 1"),
        )
        resolver = EntityResolver(plant)
        results = resolver.resolve("la caldaia in Edificio B Piano 0")

        assert results[0].asset.id == "C-3"
        assert results[0].confidence > results[1].confidence

    def test_genuinely_shared_location_still_ties(self) -> None:
        """Keeping the tokens must not disarm ambiguity where it is real.

        Two assets in the *same* room are still undecidable from location
        alone; the gate must keep withholding there.
        """
        plant = _plant_with_locations(
            ("C-3", "Edificio B / Piano 0 / Centrale Termica"),
            ("C-4", "Edificio B / Piano 0 / Centrale Termica"),
        )
        resolver = EntityResolver(plant)
        results = resolver.resolve("guasto in Edificio B Piano 0 Centrale Termica")

        assert results[0].confidence == results[1].confidence
        assert resolution_verdict(results).ambiguous is True

    def test_empty_parts_from_leading_separator_are_dropped(self) -> None:
        """A leading separator yields an empty split part, which must not count.

        An empty part would inflate ``len(loc_parts)`` and silently deflate
        every score for that asset.
        """
        plant = _plant_with_locations(("P-201", "/ Edificio A"))
        resolver = EntityResolver(plant)
        results = resolver.resolve("la pompa in Edificio A")

        # One real part, fully overlapped -> the stage's maximum, 0.6.
        assert results[0].confidence == 0.6


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


class TestBandPartition:
    """The band partition is closed: every float lands in exactly one band."""

    @pytest.mark.parametrize(
        ("confidence", "expected"),
        [
            (1.0, BAND_HIGH),
            (0.9, BAND_HIGH),
            (0.7, BAND_HIGH),
            (0.69, BAND_MID),
            # The former dead zone. The origin doc said "~0.7" and left
            # (0.6, 0.7) unassigned; an if/elif/else would have swept it into
            # whichever branch was `else`. If that was `high`, 0.65 acts
            # silently on a guess.
            (0.65, BAND_MID),
            (0.5, BAND_MID),
            # 0.4 is mid-INCLUSIVE: the pre-existing gate committed at the
            # floor and must keep doing so.
            (0.4, BAND_MID),
            (0.39, BAND_LOW),
            (0.16, BAND_LOW),
            (0.0, BAND_LOW),
        ],
    )
    def test_boundaries(self, confidence: float, expected: str) -> None:
        assert _band_for(confidence) == expected

    def test_bands_are_exhaustive_and_disjoint(self) -> None:
        """No float falls outside the three bands, and none lands in two."""
        for step in range(-50, 151):
            value = step / 100
            assert _band_for(value) in (BAND_HIGH, BAND_MID, BAND_LOW)

    @pytest.mark.parametrize(
        "indeterminable",
        [None, float("nan"), "0.9", object()],
    )
    def test_indeterminable_confidence_fails_closed(self, indeterminable: object) -> None:
        """A confidence nobody can read is not a high confidence."""
        assert _band_for(indeterminable) == BAND_LOW


class TestResolutionVerdict:
    """The single derivation both the gate and the renderer read."""

    def test_single_strong_candidate_is_high_and_unambiguous(self) -> None:
        verdict = resolution_verdict([_entity("P-201", 0.95, "name_match")])
        assert verdict.band == BAND_HIGH
        assert verdict.ambiguous is False
        assert verdict.confident is True

    def test_single_mid_candidate_is_mid_and_unambiguous(self) -> None:
        verdict = resolution_verdict([_entity("P-201", 0.5, "location_match")])
        assert verdict.band == BAND_MID
        assert verdict.ambiguous is False
        assert verdict.confident is True

    def test_exactly_one_is_high(self) -> None:
        verdict = resolution_verdict([_entity("P-201", 1.0, "exact_id")])
        assert verdict.band == BAND_HIGH

    def test_missing_confidence_is_not_confident(self) -> None:
        """Fail closed: an entity whose confidence cannot be read is withheld.

        ``ResolvedEntity`` now requires ``confidence``, so this models the
        duck-typed / partially-constructed candidate that used to sail through
        the runtime's ``getattr(top, "confidence", 1.0)`` as maximally trusted.
        """

        class _Unscored:
            asset = _asset("P-201")
            match_reason = "keyword_match"

        verdict = resolution_verdict([_Unscored()])  # type: ignore[list-item]
        assert verdict.band == BAND_LOW
        assert verdict.confident is False

    def test_tie_at_name_match_is_ambiguous(self) -> None:
        """Identical confidence is the one case where ``resolved[0]`` is arbitrary."""
        verdict = resolution_verdict(
            [_entity("P-201", 0.9, "name_match"), _entity("P-202", 0.9, "name_match")]
        )
        assert verdict.ambiguous is True

    def test_tie_at_exact_id_is_multiplicity_not_ambiguity(self) -> None:
        """ "Compare P-201 and P-202" is a well-posed question, not a muddle."""
        verdict = resolution_verdict(
            [_entity("P-201", 1.0, "exact_id"), _entity("P-202", 1.0, "exact_id")]
        )
        assert verdict.band == BAND_HIGH
        assert verdict.ambiguous is False
        assert verdict.confident is True

    def test_same_band_but_different_confidence_is_not_ambiguous(self) -> None:
        """Ambiguity is an exact tie, NOT a shared band.

        0.9 and 0.75 are both ``high``, but the sort put a clear winner first —
        there is a correct answer, so asking the user would be noise. This is
        exactly the case a band-equality predicate gets wrong; pinned so nobody
        re-derives band-equality from the "top band" phrasing later.
        """
        verdict = resolution_verdict(
            [_entity("P-201", 0.9, "name_match"), _entity("P-202", 0.75, "name_keywords")]
        )
        assert verdict.band == BAND_HIGH
        assert verdict.ambiguous is False

    def test_same_mid_band_but_different_confidence_is_not_ambiguous(self) -> None:
        """The same rule inside ``mid`` — 0.6 and 0.45 share a band, not a value."""
        verdict = resolution_verdict(
            [_entity("P-201", 0.6, "location_match"), _entity("P-202", 0.45, "location_match")]
        )
        assert verdict.band == BAND_MID
        assert verdict.ambiguous is False

    def test_lower_band_runner_up_does_not_make_it_ambiguous(self) -> None:
        verdict = resolution_verdict(
            [_entity("P-201", 0.9, "name_match"), _entity("HX-401", 0.3, "keyword_match")]
        )
        assert verdict.ambiguous is False

    def test_tie_below_the_top_does_not_make_it_ambiguous(self) -> None:
        """Only the top pair matters — the runtime never had to choose lower down."""
        verdict = resolution_verdict(
            [
                _entity("P-201", 0.9, "name_match"),
                _entity("P-202", 0.7, "name_keywords"),
                _entity("HX-401", 0.7, "name_keywords"),
            ]
        )
        assert verdict.ambiguous is False

    def test_two_unscored_candidates_do_not_tie_into_ambiguity(self) -> None:
        """Unreadable confidences are withheld, not tied — ``None == None`` is a trap."""

        class _Unscored:
            def __init__(self, asset_id: str) -> None:
                self.asset = _asset(asset_id)
                self.match_reason = "keyword_match"

        verdict = resolution_verdict([_Unscored("P-201"), _Unscored("P-202")])  # type: ignore[list-item]
        assert verdict.band == BAND_LOW
        assert verdict.ambiguous is False
        assert verdict.confident is False

    def test_empty_list_has_no_band_and_is_not_ambiguous(self) -> None:
        verdict = resolution_verdict([])
        assert verdict.band is None
        assert verdict.ambiguous is False
        assert verdict.confident is False

    def test_resolver_not_found_path_still_returns_empty(self) -> None:
        """The not-found path is unchanged by the verdict work."""
        resolver = EntityResolver(_make_plant())
        assert resolver.resolve("zzzz") == []


class TestConfidenceIsRequired:
    """The 1.0 default is gone — an unscored entity cannot be constructed."""

    def test_omitting_confidence_raises(self) -> None:
        with pytest.raises(TypeError):
            ResolvedEntity(_asset("P-201"))  # type: ignore[call-arg]
