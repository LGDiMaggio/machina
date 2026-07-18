"""Resolution-authority gate — confidence AND cardinality.

An entity resolution the runtime cannot stand behind must not be treated as the
definitive asset. Two independent ways that happens, both withholding the commit
(no prefetch, no ``context['asset']``) and both surfacing a directive to ask:

* **Weak** — even the best match is a guess (low band).
* **Ambiguous** — several candidates tie at the top, so ``resolved[0]`` is
  arbitrary. Confidence cannot catch this; the canonical case ties at 1.0.
"""

from __future__ import annotations

from typing import Any

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
        assert context["resolution_verdict"].commits is False
        # Candidates are still available so the agent can ask.
        assert context["resolved_entities"] == [weak]

    @pytest.mark.asyncio
    async def test_strong_match_is_committed(self) -> None:
        agent = _agent()
        strong = ResolvedEntity(asset=_asset(), confidence=1.0, match_reason="exact_id")
        context = await agent._gather_context("P-201", [strong])
        assert context.get("asset") is not None
        assert context["asset"].id == "P-201"
        assert context["resolution_verdict"].commits is True

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

        withheld = "asset" not in context
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
        assert context["resolution_verdict"].commits is True

    @pytest.mark.asyncio
    async def test_tied_name_matches_are_flagged_ambiguous(self) -> None:
        agent = _agent()
        tied = [
            ResolvedEntity(asset=_asset("P-201"), confidence=0.9, match_reason="name_match"),
            ResolvedEntity(asset=_asset("P-202"), confidence=0.9, match_reason="name_match"),
        ]
        context = await agent._gather_context("the cooling water pump", tied)
        assert context["resolution_verdict"].ambiguous is True
        # A 0.9 tie is high-band — ``confident`` alone would have committed it.
        assert context["resolution_verdict"].confident is True
        assert context["resolution_verdict"].commits is False
        assert "asset" not in context

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


class _RecordingLLM:
    """Fake LLM that records the prompt it saw and replies with a fixed answer.

    R3 has two halves and a fake can only honestly test them separately: what
    the runtime PUT in front of the model (recorded here) and whether an answer
    of that shape SURVIVES the finalization chain to the user. A fake that
    invented the disambiguation question from nothing would assert neither.
    """

    def __init__(self, answer: str = "Which one do you mean?") -> None:
        self.model = "fake:model"
        self.answer = answer
        self.seen: list[dict[str, str]] = []

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        self.seen = list(messages)
        return self.answer

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self.seen = list(messages)
        return {"content": self.answer, "tool_calls": None}


def _tie_plant() -> Plant:
    """Two DISTINCT assets sharing one name — the real-tie repro."""
    plant = Plant(name="Test Plant")
    plant.register_asset(_asset("P-201"))
    plant.register_asset(_asset("P-202"))
    return plant


class TestCardinalityGate:
    """U4 — an ambiguous resolution withholds the commit instead of guessing."""

    @pytest.mark.asyncio
    async def test_real_tie_from_the_resolver_withholds_the_commit(self) -> None:
        """The origin's minimal failing case, driven through the real resolver.

        Not hand-built candidates: the plant is registered, the resolver is
        asked, and the tie it produces is what the gate sees.
        """
        agent = Agent(plant=_tie_plant())
        resolved = agent._resolver.resolve("the cooling water pump")

        # Precondition — if the cascade stops producing a tie this test must
        # fail loudly here rather than pass vacuously on a one-candidate list.
        assert len(resolved) >= 2
        assert resolved[0].confidence == resolved[1].confidence
        assert resolved[0].match_reason != "exact_id"

        context = await agent._gather_context("the cooling water pump", resolved)

        assert context["resolution_verdict"].ambiguous is True
        assert context["resolution_verdict"].commits is False
        assert "asset" not in context
        # No prefetch ran — the connector-derived keys are absent entirely.
        assert "work_orders" not in context
        assert "spare_parts" not in context

    @pytest.mark.asyncio
    async def test_single_strong_candidate_commits_and_prefetches(self) -> None:
        agent = Agent(plant=_tie_plant())
        resolved = agent._resolver.resolve("P-201")

        assert len(resolved) == 1
        context = await agent._gather_context("P-201", resolved)

        assert context["resolution_verdict"].commits is True
        assert context["asset"].id == "P-201"

    @pytest.mark.asyncio
    async def test_exact_id_tie_is_multiplicity_and_still_commits(self) -> None:
        """ "Compare P-201 and P-202" must not become an unanswerable refusal."""
        agent = Agent(plant=_tie_plant())
        resolved = agent._resolver.resolve("compare P-201 and P-202")

        assert len(resolved) == 2
        assert resolved[0].confidence == resolved[1].confidence
        assert resolved[0].match_reason == "exact_id"

        context = await agent._gather_context("compare P-201 and P-202", resolved)
        assert context["resolution_verdict"].ambiguous is False
        assert context["resolution_verdict"].commits is True
        assert context["asset"] is not None

    @pytest.mark.asyncio
    async def test_withheld_turn_still_renders_the_candidates(self) -> None:
        """Withholding the commit must not hide the candidates — R3 needs them."""
        agent = Agent(plant=_tie_plant())
        resolved = agent._resolver.resolve("the cooling water pump")
        context = await agent._gather_context("the cooling water pump", resolved)

        assert context["resolved_entities"] == resolved
        rendered = format_resolved_entities(
            context["resolved_entities"], context["resolution_verdict"]
        )
        assert "P-201" in rendered
        assert "P-202" in rendered
        assert "Ambiguous" in rendered

    @pytest.mark.asyncio
    async def test_empty_resolution_is_not_an_ambiguity_claim(self) -> None:
        """Nothing found stays nothing found — no verdict, no withhold event."""
        agent = Agent(plant=_tie_plant())
        context = await agent._gather_context("what is the weather", [])

        assert context["resolved_entities"] == []
        assert "asset" not in context
        assert "resolution_verdict" not in context


class TestDisambiguationReachesTheUser:
    """R3 — the withheld state surfaces as a question naming the candidates."""

    @pytest.mark.asyncio
    async def test_withheld_turn_asks_the_model_to_disambiguate_by_name(self) -> None:
        """Both candidates AND an explicit directive reach the model."""
        llm = _RecordingLLM()
        agent = Agent(plant=_tie_plant(), llm=llm)
        await agent.handle_message_full("the cooling water pump", chat_id="c1")

        prompt = "\n".join(m.get("content", "") for m in llm.seen)
        assert "P-201" in prompt
        assert "P-202" in prompt
        assert "Ambiguous" in prompt
        # A directive, not merely a confidence number — the number was already
        # rendered before this gate existed and changed nothing.
        assert "Ask the user which one they mean" in prompt
        assert "do not act on any of them" in prompt

    @pytest.mark.asyncio
    async def test_the_disambiguation_answer_reaches_the_user_intact(self) -> None:
        """The response half of R3, asserted on ``AgentResponse``.

        A finalization chain that suppressed or hedged this answer would leave
        the user with no way to resolve the ambiguity the gate just created.
        """
        answer = "I found two matches: P-201 and P-202. Which one do you mean?"
        agent = Agent(plant=_tie_plant(), llm=_RecordingLLM(answer))
        response = await agent.handle_message_full("the cooling water pump", chat_id="c1")

        assert response.text == answer
        assert response.is_fallback is False
        assert "P-201" in response.text
        assert "P-202" in response.text
