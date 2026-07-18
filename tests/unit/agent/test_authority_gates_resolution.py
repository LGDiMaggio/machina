"""U5 — Resolution-confidence gate.

A low-confidence entity resolution must not be treated as the definitive asset:
the runtime withholds committing to it (no prefetch, no ``context['asset']``) and
the prompt nudges the agent to ask which asset is meant. See the v0.3 plan.
"""

from __future__ import annotations

import pytest

from machina.agent.entity_resolver import (
    BAND_LOW,
    RESOLUTION_MIN_CONFIDENCE,
    ResolvedEntity,
    _ResolutionVerdict,
    resolution_verdict,
)
from machina.agent.prompts import format_resolved_entities
from machina.agent.runtime import Agent
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant


def _asset(asset_id: str = "P-201") -> Asset:
    return Asset(
        id=asset_id,
        name="Cooling Water Pump",
        type=AssetType.ROTATING_EQUIPMENT,
        location="Building A",
        criticality=Criticality.A,
    )


def _agent() -> Agent:
    plant = Plant(name="Test Plant")
    plant.register_asset(_asset())
    return Agent(plant=plant)


class TestGatherContextGate:
    @pytest.mark.asyncio
    async def test_low_confidence_match_is_not_committed(self) -> None:
        agent = _agent()
        weak = ResolvedEntity(asset=_asset(), confidence=0.16, match_reason="keyword_match")
        context = await agent._gather_context("the thing in building a", [weak])
        assert "asset" not in context
        assert context.get("resolution_uncertain") is True
        # Candidates are still available so the agent can ask.
        assert context["resolved_entities"] == [weak]

    @pytest.mark.asyncio
    async def test_strong_match_is_committed(self) -> None:
        agent = _agent()
        strong = ResolvedEntity(asset=_asset(), confidence=1.0, match_reason="exact_id")
        context = await agent._gather_context("P-201", [strong])
        assert context.get("asset") is not None
        assert context["asset"].id == "P-201"
        assert "resolution_uncertain" not in context

    @pytest.mark.asyncio
    async def test_match_at_floor_is_committed(self) -> None:
        agent = _agent()
        at_floor = ResolvedEntity(
            asset=_asset(), confidence=RESOLUTION_MIN_CONFIDENCE, match_reason="location_match"
        )
        context = await agent._gather_context("building a", [at_floor])
        assert context.get("asset") is not None


class TestPromptNudge:
    def test_low_confidence_adds_disambiguation_nudge(self) -> None:
        weak = ResolvedEntity(asset=_asset(), confidence=0.16, match_reason="keyword_match")
        rendered = format_resolved_entities([weak])
        assert "Low confidence" in rendered

    def test_strong_match_has_no_nudge(self) -> None:
        strong = ResolvedEntity(asset=_asset(), confidence=0.9, match_reason="name_match")
        rendered = format_resolved_entities([strong])
        assert "Low confidence" not in rendered

    def test_nudge_reflects_the_verdict_the_gate_acted_on(self) -> None:
        """The renderer consumes the passed-down verdict, not a fresh opinion."""
        strong = ResolvedEntity(asset=_asset(), confidence=0.9, match_reason="name_match")
        withheld = _ResolutionVerdict(band=BAND_LOW, ambiguous=False)
        rendered = format_resolved_entities([strong], withheld)
        assert "Low confidence" in rendered


class TestSingleSourceOfTruth:
    """R2 — the gate and the renderer cannot disagree about a candidate list."""

    @pytest.mark.parametrize(
        "confidence",
        [0.0, 0.16, 0.39, 0.4, 0.5, 0.6, 0.65, 0.69, 0.7, 0.9, 1.0],
    )
    @pytest.mark.asyncio
    async def test_withhold_and_nudge_agree_on_every_band(self, confidence: float) -> None:
        """Sweep the partition: nudge shown exactly when the commit is withheld.

        Before the collapse these were two independent derivations — the gate
        defaulting a missing confidence to 1.0, the renderer reading the
        attribute bare — so this equality was a coincidence maintained by hand.
        """
        agent = _agent()
        candidate = ResolvedEntity(
            asset=_asset(), confidence=confidence, match_reason="location_match"
        )
        context = await agent._gather_context("some asset", [candidate])

        withheld = context.get("resolution_uncertain") is True
        nudged = "Low confidence" in format_resolved_entities(
            context["resolved_entities"], context.get("resolution_verdict")
        )
        assert withheld == nudged, f"gate and renderer diverged at {confidence}"
        # And the committed/withheld split is the band split, not a third rule.
        assert withheld is (resolution_verdict([candidate]).band == BAND_LOW)

    @pytest.mark.asyncio
    async def test_gate_publishes_the_verdict_it_used(self) -> None:
        """The verdict travels down the context dict — it is not recomputed."""
        agent = _agent()
        weak = ResolvedEntity(asset=_asset(), confidence=0.16, match_reason="keyword_match")
        context = await agent._gather_context("the thing", [weak])
        assert context["resolution_verdict"] == resolution_verdict([weak])

    @pytest.mark.asyncio
    async def test_several_exact_id_hits_stay_actionable(self) -> None:
        """ "Compare P-201 and P-202" is multiplicity, not ambiguity.

        Both are whole-token ID hits, so the turn commits to the top asset
        rather than refusing with a disambiguation question.
        """
        agent = _agent()
        hits = [
            ResolvedEntity(asset=_asset("P-201"), confidence=1.0, match_reason="exact_id"),
            ResolvedEntity(asset=_asset("P-202"), confidence=1.0, match_reason="exact_id"),
        ]
        context = await agent._gather_context("compare P-201 and P-202", hits)
        assert context["resolution_verdict"].ambiguous is False
        assert context.get("asset") is not None
        assert "resolution_uncertain" not in context

    @pytest.mark.asyncio
    async def test_tied_name_matches_are_flagged_ambiguous(self) -> None:
        agent = _agent()
        tied = [
            ResolvedEntity(asset=_asset("P-201"), confidence=0.9, match_reason="name_match"),
            ResolvedEntity(asset=_asset("P-202"), confidence=0.9, match_reason="name_match"),
        ]
        context = await agent._gather_context("the cooling water pump", tied)
        assert context["resolution_verdict"].ambiguous is True

    @pytest.mark.asyncio
    async def test_clear_winner_in_the_same_band_is_not_ambiguous(self) -> None:
        """A sorted winner is a winner — shared band is not a tie.

        The gate-level counterpart of the unit pin: 0.9 over 0.75 has a correct
        answer, so the turn proceeds instead of asking.
        """
        agent = _agent()
        ranked = [
            ResolvedEntity(asset=_asset("P-201"), confidence=0.9, match_reason="name_match"),
            ResolvedEntity(asset=_asset("P-202"), confidence=0.75, match_reason="name_keywords"),
        ]
        context = await agent._gather_context("the cooling water pump", ranked)
        assert context["resolution_verdict"].ambiguous is False
        assert context.get("asset") is not None
