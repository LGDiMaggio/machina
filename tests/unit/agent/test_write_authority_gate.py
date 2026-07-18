"""Write-path authority gate — a write cannot land on an unconfirmed asset.

Withholding ``context["asset"]`` is a PREFETCH gate, not a write gate: the write
tool reads ``asset_id`` out of the model's own arguments, and the candidate IDs
are rendered to the model regardless. So the withheld turn hands the model a
menu of IDs and nothing stops it picking one.

Three checks, because registry existence alone is a liveness test that every
asset in the plant passes and therefore authorises nothing:

1. the ``asset_id`` exists in the registry,
2. it is one the turn actually resolved — existence is not binding,
3. the band permits a write at all (``ambiguous``/``low``/nothing-resolved
   refuse; ``mid`` asks; ``high`` executes).

Check 3 is what closes the laundering hole: ``low`` is not ambiguous, an empty
resolution is not ambiguous, and multiplicity is not ambiguous, so without it
three paths reach execution ungated — making the write path MORE permissive at
0.2 confidence than at 0.5.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from machina.agent.entity_resolver import ResolvedEntity
from machina.agent.runtime import Agent, _TurnResolution
from machina.connectors.capabilities import Capability
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant


def _asset(
    asset_id: str, name: str = "Cooling Water Pump", location: str = "Building Alpha"
) -> Asset:
    return Asset(
        id=asset_id,
        name=name,
        type=AssetType.ROTATING_EQUIPMENT,
        location=location,
        criticality=Criticality.A,
    )


def _plant() -> Plant:
    """P-201 and P-202 share a name; C-101 is unrelated but registered.

    Locations are multi-word and distinct on a token longer than one character:
    the resolver drops single-character location tokens, so "Building A" and
    "Building B" would both score a *perfect* location match on either query and
    every location question would tie.
    """
    plant = Plant(name="Test Plant")
    plant.register_asset(_asset("P-201", location="Building Alpha"))
    plant.register_asset(_asset("P-202", location="Building Beta"))
    plant.register_asset(_asset("C-101", name="Air Compressor", location="Compressor House"))
    return plant


class _SpyCmms:
    """CREATE_WORK_ORDER connector recording whether a write ever executed."""

    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CREATE_WORK_ORDER})

    def __init__(self) -> None:
        self.created: list[Any] = []

    async def connect(self) -> None:  # pragma: no cover
        pass

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    async def create_work_order(self, wo: Any) -> Any:
        self.created.append(wo)
        return wo


def _args(asset_id: str = "P-201") -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "type": "corrective",
        "priority": "high",
        "description": "Bearing noise",
    }


async def _resolve_turn(agent: Agent, text: str, chat_id: str = "c1") -> list[ResolvedEntity]:
    """Run the real resolution + gate for ``text``, as a turn would."""
    resolved = agent._resolver.resolve(text)
    await agent._gather_context(text, resolved, chat_id=chat_id)
    return resolved


def _refused(result: Any) -> bool:
    return isinstance(result, dict) and result.get("refused") is True


class TestWriteExecutesWhenTheTurnConfirmedTheAsset:
    @pytest.mark.asyncio
    async def test_high_confidence_resolution_permits_the_write(self) -> None:
        spy = _SpyCmms()
        agent = Agent(plant=_plant(), connectors=[spy], confirmations=False)
        resolved = await _resolve_turn(agent, "P-201 is making noise")
        assert resolved[0].confidence >= 0.7

        result = await agent._execute_tool("create_work_order", _args("P-201"), chat_id="c1")

        assert not _refused(result)
        assert [wo.asset_id for wo in spy.created] == ["P-201"]

    @pytest.mark.asyncio
    async def test_multiplicity_permits_a_write_to_either_named_asset(self) -> None:
        """An ``exact_id`` tie is several well-posed referents, not ambiguity."""
        spy = _SpyCmms()
        agent = Agent(plant=_plant(), connectors=[spy], confirmations=False)
        await _resolve_turn(agent, "compare P-201 and P-202")

        result = await agent._execute_tool("create_work_order", _args("P-202"), chat_id="c1")

        assert not _refused(result)
        assert [wo.asset_id for wo in spy.created] == ["P-202"]


class TestWriteRefusedOnTheBand:
    @pytest.mark.asyncio
    async def test_ambiguous_resolution_refuses_the_write(self) -> None:
        """The headline case: the model picks one candidate off the menu."""
        spy = _SpyCmms()
        agent = Agent(plant=_plant(), connectors=[spy], confirmations=False)
        resolved = await _resolve_turn(agent, "the cooling water pump")
        assert resolved[0].confidence == resolved[1].confidence  # precondition: a real tie

        result = await agent._execute_tool("create_work_order", _args("P-201"), chat_id="c1")

        assert _refused(result)
        assert result["reason"] == "ambiguous_resolution"
        assert spy.created == []

    @pytest.mark.asyncio
    async def test_ambiguous_resolution_refuses_in_sandbox_too(self) -> None:
        """A refusal is not a mutation, so sandbox does not exempt it.

        This is also the PLACEMENT guard. The sandbox early return is the
        handler's first statement and returns before ``asset_id`` is ever read,
        so a check placed at the natural-looking spot (after the connector
        lookup) would be unreachable in exactly the mode the flagship template
        defaults to.
        """
        spy = _SpyCmms()
        agent = Agent(plant=_plant(), connectors=[spy], sandbox=True, confirmations=False)
        await _resolve_turn(agent, "the cooling water pump")

        result = await agent._execute_tool("create_work_order", _args("P-201"), chat_id="c1")

        assert _refused(result)
        # Never reached the sandbox branch — that payload would look like a
        # successful dry run of a write that must not happen at all.
        assert result.get("sandbox") is not True
        assert result.get("action") != "create_work_order"

    @pytest.mark.asyncio
    async def test_low_band_resolution_refuses_the_write(self) -> None:
        """The permissiveness inversion: 0.2 must not outrank 0.5."""
        spy = _SpyCmms()
        agent = Agent(plant=_plant(), connectors=[spy], confirmations=False)
        weak = ResolvedEntity(asset=_asset("P-201"), confidence=0.16, match_reason="keyword_match")
        await agent._gather_context("the thing over there", [weak], chat_id="c1")

        result = await agent._execute_tool("create_work_order", _args("P-201"), chat_id="c1")

        assert _refused(result)
        assert result["reason"] == "low_confidence_resolution"
        assert spy.created == []

    @pytest.mark.asyncio
    async def test_empty_resolution_refuses_the_write(self) -> None:
        """The laundering hole: the model reads an ID out of its own history."""
        spy = _SpyCmms()
        agent = Agent(plant=_plant(), connectors=[spy], confirmations=False)
        resolved = await _resolve_turn(agent, "go ahead and open it")
        assert resolved == []  # precondition: this turn resolved nothing

        result = await agent._execute_tool("create_work_order", _args("P-201"), chat_id="c1")

        assert _refused(result)
        assert result["reason"] == "nothing_resolved"
        assert spy.created == []


class TestWriteRefusedOnTheTarget:
    @pytest.mark.asyncio
    async def test_registered_asset_the_turn_did_not_resolve_is_refused(self) -> None:
        """Registry existence is a liveness test — it authorises nothing."""
        spy = _SpyCmms()
        agent = Agent(plant=_plant(), connectors=[spy], confirmations=False)
        await _resolve_turn(agent, "P-201 is making noise")

        # C-101 is a perfectly real asset. It is just not what was asked about.
        result = await agent._execute_tool("create_work_order", _args("C-101"), chat_id="c1")

        assert _refused(result)
        assert result["reason"] == "asset_not_resolved_this_turn"
        assert spy.created == []

    @pytest.mark.asyncio
    async def test_asset_absent_from_the_registry_is_refused(self) -> None:
        spy = _SpyCmms()
        agent = Agent(plant=_plant(), connectors=[spy], confirmations=False)
        await _resolve_turn(agent, "P-201 is making noise")

        result = await agent._execute_tool("create_work_order", _args("P-999"), chat_id="c1")

        assert _refused(result)
        assert result["reason"] == "unknown_asset"
        assert spy.created == []

    @pytest.mark.asyncio
    async def test_empty_asset_id_is_refused(self) -> None:
        spy = _SpyCmms()
        agent = Agent(plant=_plant(), connectors=[spy], confirmations=False)
        await _resolve_turn(agent, "P-201 is making noise")

        result = await agent._execute_tool("create_work_order", _args(""), chat_id="c1")

        assert _refused(result)
        assert result["reason"] == "missing_asset_id"
        assert spy.created == []

    @pytest.mark.asyncio
    async def test_a_turn_with_no_resolution_record_at_all_is_refused(self) -> None:
        """Fail closed: no verdict is not the same as a confident one."""
        spy = _SpyCmms()
        agent = Agent(plant=_plant(), connectors=[spy], confirmations=False)

        result = await agent._execute_tool(
            "create_work_order", _args("P-201"), chat_id="never-ran"
        )

        assert _refused(result)
        assert result["reason"] == "no_resolution_for_turn"
        assert spy.created == []


class TestRefusalMessageIsHonest:
    @pytest.mark.asyncio
    async def test_refusal_says_the_work_order_was_not_created(self) -> None:
        agent = Agent(plant=_plant(), connectors=[_SpyCmms()], confirmations=False)
        await _resolve_turn(agent, "the cooling water pump")

        result = await agent._execute_tool("create_work_order", _args("P-201"), chat_id="c1")

        assert "NOT created" in result["error"]
        # Names the candidates so the model can ask a concrete question.
        assert "P-201" in result["error"]
        assert "P-202" in result["error"]


class TestConfirmationPromptCarriesTheCaveat:
    def test_mid_band_prompt_names_the_assumed_asset_and_its_uncertainty(self) -> None:
        agent = Agent(plant=_plant())
        mid = ResolvedEntity(asset=_asset("P-201"), confidence=0.6, match_reason="location_match")
        resolution = _TurnResolution.of([mid])

        prompt = agent._confirmation_prompt("create_work_order", _args(), resolution=resolution)

        assert "P-201" in prompt
        assert "partial confidence" in prompt

    def test_high_band_prompt_has_no_caveat(self) -> None:
        agent = Agent(plant=_plant())
        strong = ResolvedEntity(asset=_asset("P-201"), confidence=1.0, match_reason="exact_id")
        resolution = _TurnResolution.of([strong])

        prompt = agent._confirmation_prompt("create_work_order", _args(), resolution=resolution)

        assert "partial confidence" not in prompt

    def test_prompt_without_a_resolution_is_unchanged(self) -> None:
        """The function stays pure — it reads no instance state for this."""
        agent = Agent(plant=_plant())
        prompt = agent._confirmation_prompt("create_work_order", _args())
        assert "Create a work order?" in prompt
        assert "partial confidence" not in prompt


class _WriteThenAnswerLLM:
    """Requests create_work_order once, then answers."""

    def __init__(self, asset_id: str = "P-201") -> None:
        self.model = "fake:model"
        self.asset_id = asset_id
        self._n = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Done."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._n += 1
        if self._n == 1:
            call = type("C", (), {})()
            call.id = "call_1"
            call.function = type("F", (), {})()
            call.function.name = "create_work_order"
            call.function.arguments = __import__("json").dumps(_args(self.asset_id))
            return {"content": None, "tool_calls": [call]}
        return {"content": "Work order handled.", "tool_calls": None}


class TestTwoTurnResumePath:
    @pytest.mark.asyncio
    async def test_mid_band_propose_then_confirm_executes_the_same_asset(self) -> None:
        """The stored prompt keeps the caveat and the resumed write lands."""
        spy = _SpyCmms()
        agent = Agent(
            plant=_plant(),
            connectors=[spy],
            llm=_WriteThenAnswerLLM("P-201"),
            confirmations=True,
        )
        # A location match scores 0.6 — mid band, and Building Beta scores
        # strictly lower, so there is a clear winner rather than a tie.
        first = await agent.handle_message_full(
            "open a job in building alpha", chat_id="c1", user_id="u1"
        )
        assert spy.created == []
        pending = agent._pending_actions[("c1", "u1")]
        assert "partial confidence" in pending[2]
        assert "P-201" in first.text or "P-201" in pending[2]

        await agent.handle_message_full("yes", chat_id="c1", user_id="u1")

        assert [wo.asset_id for wo in spy.created] == ["P-201"]

    @pytest.mark.asyncio
    async def test_ambiguous_propose_is_still_refused_on_confirmation(self) -> None:
        """Fail closed across the turn boundary — the ambiguity does not decay."""
        spy = _SpyCmms()
        agent = Agent(
            plant=_plant(),
            connectors=[spy],
            llm=_WriteThenAnswerLLM("P-201"),
            confirmations=True,
        )
        await agent.handle_message_full(
            "open a job for the cooling water pump", chat_id="c1", user_id="u1"
        )
        pending = agent._pending_actions.get(("c1", "u1"))
        if pending is None:
            pytest.skip("write never reached the confirmation store")

        await agent.handle_message_full("yes", chat_id="c1", user_id="u1")

        assert spy.created == []


class TestPostWriteNarrationIsNotHedged:
    @pytest.mark.asyncio
    async def test_executed_write_narration_carries_no_failure_hedge(self) -> None:
        """Implying failure after a write invites a duplicate write."""
        spy = _SpyCmms()
        agent = Agent(
            plant=_plant(),
            connectors=[spy],
            llm=_WriteThenAnswerLLM("P-201"),
            confirmations=False,
        )
        response = await agent.handle_message_full("P-201 is making noise", chat_id="c1")

        assert [wo.asset_id for wo in spy.created] == ["P-201"]
        assert "NOT created" not in response.text
        assert "refused" not in response.text.lower()
