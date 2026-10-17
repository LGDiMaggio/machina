"""Loop-level behaviour of a failing tool: reads degrade, writes keep aborting.

The read-tool guard and its unit tests live in ``machina.agent.tool_errors`` /
``tests/unit/agent/test_tool_errors.py`` and ``TestReadToolFailureDegrades`` in
``tests/unit/test_agent_runtime.py``. The tests here drive whole turns, and
were ported from PR #114 (an alternative implementation of the same guard),
keeping the scenarios that suite does not cover:

- a transient read failure recovers on the model's retry;
- a failed read is logged with the agent, tool and argument names;
- a failed read whose retry is spent is replayed with a note saying it failed;
- a raising write still aborts the turn, so it is never executed twice, on the
  direct path and on the two-turn confirmation path.

An exception from a write leaves its outcome unknown (a POST can be applied
and still answer 5xx or time out), and an error result would let the model
re-issue it in the same turn: the per-turn memo deliberately skips error
results, so the re-issue would execute again.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, NoReturn
from unittest.mock import MagicMock

import pytest
import structlog

from machina.agent.runtime import _FAILED_READ_REPLAY_NOTE, Agent
from machina.connectors.capabilities import Capability
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import ConnectorError, LLMError, MachinaError

_ANSWER = "The maintenance system did not answer, so I could not look that up."

_CREATE_ARGS = {
    "asset_id": "P-201",
    "type": "corrective",
    "priority": "high",
    "description": "Replace bearing",
}


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


class _FailingConnector:
    """Declares one capability; every operation it offers raises ``error``."""

    def __init__(self, capability: Capability, error: BaseException) -> None:
        self.capabilities = frozenset({capability})
        self._error = error
        self.calls = 0

    async def connect(self) -> None:  # pragma: no cover
        pass

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    def _fail(self) -> NoReturn:
        self.calls += 1
        raise self._error

    async def read_work_orders(self, **kwargs: Any) -> list[WorkOrder]:
        self._fail()

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        self._fail()


class _FlakyWorkOrders:
    """``read_work_orders`` fails on the first call and answers on the next."""

    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READ_WORK_ORDERS})

    def __init__(self) -> None:
        self.calls = 0

    async def connect(self) -> None:  # pragma: no cover
        pass

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    async def read_work_orders(self, **kwargs: Any) -> list[WorkOrder]:
        self.calls += 1
        if self.calls == 1:
            raise ConnectorError("CMMS answered HTTP 503")
        return [
            WorkOrder(
                id="WO-001",
                type=WorkOrderType.CORRECTIVE,
                priority=Priority.HIGH,
                asset_id="P-201",
                description="Replace bearing",
            )
        ]


class _ScriptedLLM:
    """Plays one step per completion: a tool call ``(name, args)`` or text.

    Every message list it is sent is recorded, so a test can read back exactly
    what the runtime fed the model.
    """

    def __init__(self, *steps: tuple[str, dict[str, Any]] | str) -> None:
        self.model = "fake:model"
        self._steps = steps
        self.sent: list[list[dict[str, Any]]] = []

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.sent.append([dict(m) for m in messages])
        return _ANSWER

    async def complete_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self.sent.append([dict(m) for m in messages])
        step = self._steps[min(len(self.sent), len(self._steps)) - 1]
        if isinstance(step, str):
            return {"content": step, "tool_calls": None}
        name, args = step
        call = MagicMock()
        call.function.name = name
        call.function.arguments = json.dumps(args)
        call.id = f"call_{len(self.sent):03d}"
        return {"content": "", "tool_calls": [call]}

    def tool_results(self) -> list[Any]:
        """Every tool result fed back to the model so far, decoded, in order."""
        return [json.loads(m["content"]) for m in self.sent[-1] if m.get("role") == "tool"]


class TestReadFailureDegradesToToolError:
    """A failed read is fed back to the model and the turn completes."""

    @pytest.mark.asyncio
    async def test_failed_read_is_retried_not_replayed_from_cache(self) -> None:
        """The first error is not cached, so the model's retry reaches the CMMS again."""
        conn = _FlakyWorkOrders()
        agent = Agent(plant=_plant(), connectors=[conn])
        call = ("read_work_orders", {"asset_id": "P-201"})
        llm = _ScriptedLLM(call, call, _ANSWER)
        agent._llm = llm  # type: ignore[assignment]

        resp = await agent.handle_message_full("Is anything wrong?")

        assert resp.text == _ANSWER
        assert conn.calls == 2
        failed, retried = llm.tool_results()
        assert failed == {"error": "CMMS answered HTTP 503"}
        assert [wo["id"] for wo in retried] == ["WO-001"]

    @pytest.mark.asyncio
    async def test_spent_failed_read_is_replayed_with_a_failure_note(self) -> None:
        """After its one retry, a failing read is served from the cache as a failure."""
        conn = _FailingConnector(
            Capability.READ_WORK_ORDERS, ConnectorError("CMMS answered HTTP 500")
        )
        agent = Agent(plant=_plant(), connectors=[conn])
        call = ("read_work_orders", {"asset_id": "P-201"})
        llm = _ScriptedLLM(call, call, call, _ANSWER)
        agent._llm = llm  # type: ignore[assignment]

        await agent.handle_message_full("Is anything wrong?")

        assert conn.calls == 2  # first attempt + one retry; the third is replayed
        replayed = llm.tool_results()[2]
        assert replayed["already_retrieved"] is True
        assert replayed["note"] == _FAILED_READ_REPLAY_NOTE
        assert replayed["result"] == {"error": "CMMS answered HTTP 500"}

    @pytest.mark.asyncio
    async def test_failure_is_logged_with_agent_tool_and_operation(self) -> None:
        conn = _FailingConnector(
            Capability.READ_WORK_ORDERS, ConnectorError("CMMS answered HTTP 500")
        )
        agent = Agent(plant=_plant(), connectors=[conn])
        events: list[tuple[str, dict[str, Any]]] = []

        def _capture(_logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
            events.append((method, dict(event_dict)))
            return event_dict

        structlog.configure(processors=[_capture, structlog.processors.JSONRenderer()])
        try:
            await agent._execute_tool("read_work_orders", {"asset_id": "P-201", "status": "open"})
        finally:
            structlog.reset_defaults()

        failures = [(m, e) for m, e in events if e.get("event") == "read_tool_failed"]
        assert len(failures) == 1
        method, event = failures[0]
        assert method == "warning"
        assert event["agent"] == agent.name
        assert event["tool"] == "read_work_orders"
        assert event["operation"] == "execute_tool"
        assert event["error"] == "CMMS answered HTTP 500"
        # Argument names, not values: values can carry the user's free text.
        assert event["arg_keys"] == ["asset_id", "status"]
        assert "args" not in event


class TestWriteFailureIsNotDegraded:
    """A raising write keeps aborting the turn, so it is never executed twice."""

    @pytest.mark.asyncio
    async def test_failed_write_aborts_the_turn_instead_of_inviting_a_retry(self) -> None:
        """The model re-issues the same write; it must not reach the CMMS again.

        Were the failure fed back as ``{"error": ...}``, the second identical
        call would execute: error results are deliberately not memoised.
        """
        conn = _FailingConnector(
            Capability.CREATE_WORK_ORDER, ConnectorError("CMMS answered HTTP 500")
        )
        agent = Agent(plant=_plant(), connectors=[conn], confirmations=False)
        write = ("create_work_order", _CREATE_ARGS)
        agent._llm = _ScriptedLLM(write, write, _ANSWER)  # type: ignore[assignment]

        with pytest.raises(LLMError):
            await agent.handle_message_full("Create a work order for P-201: replace the bearing")

        assert conn.calls == 1

    @pytest.mark.asyncio
    async def test_failed_write_approved_by_a_confirmer_aborts_the_turn(self) -> None:
        """Synchronous-confirmer path (a CLI): the approved write fails once, is not re-run."""
        conn = _FailingConnector(
            Capability.CREATE_WORK_ORDER, ConnectorError("CMMS answered HTTP 500")
        )
        agent = Agent(plant=_plant(), connectors=[conn])
        write = ("create_work_order", _CREATE_ARGS)
        agent._llm = _ScriptedLLM(write, write, _ANSWER)  # type: ignore[assignment]
        asked: list[str] = []

        async def approve(prompt: str) -> bool:
            asked.append(prompt)
            return True

        with pytest.raises(LLMError):
            await agent.handle_message_full(
                "Create a work order for P-201: replace the bearing", confirmer=approve
            )

        assert len(asked) == 1
        assert conn.calls == 1

    @pytest.mark.asyncio
    async def test_failed_confirmed_write_is_not_left_pending(self) -> None:
        """Two-turn path: the confirmed write fails once and cannot be confirmed again."""
        conn = _FailingConnector(
            Capability.CREATE_WORK_ORDER, ConnectorError("CMMS answered HTTP 500")
        )
        agent = Agent(plant=_plant(), connectors=[conn])
        propose = _ScriptedLLM(("create_work_order", _CREATE_ARGS), "Shall I create it?")
        agent._llm = propose  # type: ignore[assignment]
        await agent.handle_message_full(
            "Create a work order for P-201: replace the bearing", chat_id="c1", user_id="userA"
        )
        assert ("c1", "userA") in agent._pending_actions

        # The connector's own exception or a wrapping one — either way the turn fails.
        with pytest.raises(MachinaError):
            await agent.handle_message_full("yes", chat_id="c1", user_id="userA")

        assert conn.calls == 1
        assert ("c1", "userA") not in agent._pending_actions
