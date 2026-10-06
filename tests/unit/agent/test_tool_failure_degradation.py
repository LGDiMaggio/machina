"""A read tool whose connector fails returns a tool error; a write does not.

A connector failure inside a READ tool — a REST CMMS answering HTTP 500, a
document store that is down — used to escape ``_execute_tool``, abort the LLM
loop and fail the whole turn as ``LLMError``, although nothing had been written
and the model could have relayed the failure or tried another way. Every read
now degrades the way ``get_work_order`` always did: a warning log, then an
``{"error": ...}`` tool result fed back to the model, on the structured
tool-call path and on the recovered-read path alike.

Writes keep propagating. An exception from a write leaves its outcome unknown
(a POST can be applied and still answer 5xx or time out), and an error result
would let the model re-issue it in the same turn: the per-turn memo
deliberately skips error results, so the re-issue would execute again.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn
from unittest.mock import MagicMock

import pytest
import structlog

from machina.agent.runtime import Agent
from machina.connectors.capabilities import Capability
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import ConnectorError, LLMError, MachinaError

if TYPE_CHECKING:
    from machina.domain.failure_mode import FailureMode

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

    async def get_work_order(self, work_order_id: str) -> WorkOrder | None:
        self._fail()

    async def search(self, query: str, **kwargs: Any) -> list[Any]:
        self._fail()

    async def read_spare_parts(self, **kwargs: Any) -> list[Any]:
        self._fail()

    async def read_maintenance_plans(self) -> list[Any]:
        self._fail()

    async def read_failure_modes(self) -> list[FailureMode]:
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

    @pytest.mark.parametrize(
        ("tool", "args", "capability", "error"),
        [
            (
                "read_work_orders",
                {"asset_id": "P-201"},
                Capability.READ_WORK_ORDERS,
                ConnectorError("CMMS answered HTTP 500"),
            ),
            (
                "get_work_order",
                {"work_order_id": "WO-001"},
                Capability.GET_WORK_ORDER,
                ConnectorError("CMMS answered HTTP 500"),
            ),
            (
                "search_documents",
                {"query": "bearing replacement"},
                Capability.SEARCH_DOCUMENTS,
                ConnectorError("CMMS answered HTTP 500"),
            ),
            (
                "check_spare_parts",
                {"asset_id": "P-201"},
                Capability.READ_SPARE_PARTS,
                ConnectorError("CMMS answered HTTP 500"),
            ),
            (
                "get_maintenance_schedule",
                {"asset_id": "P-201"},
                Capability.READ_MAINTENANCE_PLANS,
                ConnectorError("CMMS answered HTTP 500"),
            ),
            # Not every backend wraps its failures in ConnectorError: a vector
            # store or an httpx client raises its own exceptions.
            (
                "search_documents",
                {"query": "bearing replacement"},
                Capability.SEARCH_DOCUMENTS,
                RuntimeError("vector store unavailable"),
            ),
            # The failure-mode harvest skips a provider raising ConnectorError
            # by design (the diagnosis then says no catalog is configured);
            # any other provider failure used to escape the tool.
            (
                "diagnose_failure",
                {"asset_id": "P-201", "symptoms": ["vibration"]},
                Capability.READ_FAILURE_MODES,
                RuntimeError("CMMS answered HTTP 500"),
            ),
        ],
        ids=[
            "read_work_orders",
            "get_work_order",
            "search_documents",
            "check_spare_parts",
            "get_maintenance_schedule",
            "search_documents-store-error",
            "diagnose_failure-provider-error",
        ],
    )
    @pytest.mark.asyncio
    async def test_failure_is_fed_back_and_the_turn_completes(
        self,
        tool: str,
        args: dict[str, Any],
        capability: Capability,
        error: Exception,
    ) -> None:
        conn = _FailingConnector(capability, error)
        agent = Agent(plant=_plant(), connectors=[conn])
        llm = _ScriptedLLM((tool, args), _ANSWER)
        agent._llm = llm  # type: ignore[assignment]

        resp = await agent.handle_message_full("Is anything wrong?")

        assert resp.text == _ANSWER
        assert llm.tool_results() == [{"error": str(error)}]
        assert conn.calls == 1

    @pytest.mark.asyncio
    async def test_failure_of_a_recovered_leaked_read_is_fed_back(self) -> None:
        """A read the model leaked as text is re-entered; its failure is fed back too."""
        conn = _FailingConnector(
            Capability.READ_WORK_ORDERS, ConnectorError("CMMS answered HTTP 500")
        )
        agent = Agent(plant=_plant(), connectors=[conn])
        leaked = json.dumps({"name": "read_work_orders", "arguments": {"asset_id": "P-201"}})
        llm = _ScriptedLLM(leaked, _ANSWER)
        agent._llm = llm  # type: ignore[assignment]

        resp = await agent.handle_message_full("Is anything wrong?")

        assert resp.text == _ANSWER
        recovery = llm.sent[-1][-1]
        assert recovery["role"] == "user"
        assert json.dumps({"error": "CMMS answered HTTP 500"}) in recovery["content"]
        assert conn.calls == 1

    @pytest.mark.asyncio
    async def test_failed_read_is_retried_not_replayed_from_cache(self) -> None:
        """The error result is not cached, so the model's retry reaches the CMMS again."""
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
            await agent._execute_tool("read_work_orders", {"asset_id": "P-201"})
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

    @pytest.mark.asyncio
    async def test_failure_without_a_message_reports_its_type(self) -> None:
        """A bare timeout stringifies to ''; the model must still learn what failed."""
        conn = _FailingConnector(Capability.READ_WORK_ORDERS, TimeoutError())
        agent = Agent(plant=_plant(), connectors=[conn])

        result = await agent._execute_tool("read_work_orders", {"asset_id": "P-201"})

        assert result == {"error": "TimeoutError"}

    @pytest.mark.asyncio
    async def test_cancellation_is_not_degraded(self) -> None:
        """Only failures degrade: a cancelled read still cancels the turn."""
        conn = _FailingConnector(Capability.READ_WORK_ORDERS, asyncio.CancelledError())
        agent = Agent(plant=_plant(), connectors=[conn])

        with pytest.raises(asyncio.CancelledError):
            await agent._execute_tool("read_work_orders", {"asset_id": "P-201"})


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
