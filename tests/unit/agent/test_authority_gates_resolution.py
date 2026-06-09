"""U5 — Resolution-confidence gate.

A low-confidence entity resolution must not be treated as the definitive asset:
the runtime withholds committing to it (no prefetch, no ``context['asset']``) and
the prompt nudges the agent to ask which asset is meant. See the v0.3 plan.
"""

from __future__ import annotations

import pytest

from machina.agent.entity_resolver import RESOLUTION_MIN_CONFIDENCE, ResolvedEntity
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
