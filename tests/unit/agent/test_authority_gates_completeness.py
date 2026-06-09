"""U2 — Completeness-aware finalization.

When the loop is forced to finalize a turn before the agent confirmed it had
retrieved everything, the answer is hedged for the user and flagged
``completeness="partial"`` — without corrupting the ``is_fallback`` signal or the
clean history baseline. See the v0.3 authority-gates plan.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from machina.agent.runtime import _PARTIAL_COMPLETENESS_HEDGE, Agent
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant


def _plant() -> Plant:
    plant = Plant(name="Test Plant")
    plant.register_asset(
        Asset(
            id="P-201",
            name="Cooling Water Pump",
            type=AssetType.ROTATING_EQUIPMENT,
            location="Building A",
            criticality=Criticality.A,
        )
    )
    return plant


class _ReadAssetsConnector:
    capabilities: ClassVar[list[str]] = ["read_assets"]

    async def connect(self) -> None:  # pragma: no cover
        pass

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    async def read_assets(self, **kwargs: Any) -> list[Asset]:
        return list(_plant().assets.values())


class _RepeatingToolLLM:
    """Always re-issues the same tool call → the loop force-finalizes."""

    def __init__(self) -> None:
        self.model = "fake:model"

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Here is a partial answer."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        tc = MagicMock()
        tc.function.name = "search_assets"
        tc.function.arguments = json.dumps({"query": "pump"})
        tc.id = "call_loop"
        return {"content": "", "tool_calls": [tc]}


class TestLoopMarksForcedFinalization:
    @pytest.mark.asyncio
    async def test_forced_final_marks_turn_partial(self) -> None:
        agent = Agent(plant=_plant(), llm=_RepeatingToolLLM(), connectors=[_ReadAssetsConnector()])
        text = await agent._llm_loop([{"role": "user", "content": "list it all"}], "c1")
        assert text == "Here is a partial answer."
        assert agent._turn_completeness.get("c1") == "partial"


class TestFinalizeHedging:
    def test_partial_turn_is_hedged_and_flagged(self) -> None:
        agent = Agent()
        agent._turn_completeness["c1"] = "partial"
        resp = agent._finalize_turn(
            chat_id="c1", user_text="q", raw_response="Assets: P-201, P-202."
        )
        assert resp.completeness == "partial"
        assert resp.is_fallback is False
        assert resp.text.endswith(_PARTIAL_COMPLETENESS_HEDGE)

    def test_history_baseline_excludes_the_hedge(self) -> None:
        # Regression guard: the user sees the hedge, but history stores the clean
        # answer so echo detection and source follow-ups stay accurate.
        agent = Agent()
        agent._turn_completeness["c1"] = "partial"
        agent._finalize_turn(chat_id="c1", user_text="q", raw_response="Clean answer.")
        stored = agent._histories["c1"][-1]["content"]
        assert "Clean answer." in stored
        assert _PARTIAL_COMPLETENESS_HEDGE not in stored

    def test_complete_turn_is_not_hedged(self) -> None:
        agent = Agent()  # no partial marker
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response="A complete answer.")
        assert resp.completeness == "complete"
        assert _PARTIAL_COMPLETENESS_HEDGE not in resp.text

    def test_fallback_is_not_hedged(self) -> None:
        # An empty completion becomes a synthetic fallback; that must NOT be
        # relabelled partial or hedged (is_fallback and completeness are distinct).
        agent = Agent()
        agent._turn_completeness["c1"] = "partial"
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response="")
        assert resp.is_fallback is True
        assert resp.completeness == "complete"
        assert _PARTIAL_COMPLETENESS_HEDGE not in resp.text

    def test_turn_completeness_marker_is_cleared(self) -> None:
        agent = Agent()
        agent._turn_completeness["c1"] = "partial"
        agent._finalize_turn(chat_id="c1", user_text="q", raw_response="Answer.")
        assert "c1" not in agent._turn_completeness
