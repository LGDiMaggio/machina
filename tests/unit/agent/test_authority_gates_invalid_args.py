"""U4 — Invalid tool-argument handling + self-correction.

Malformed tool-call arguments are no longer silently coerced to ``{}`` (which
masks the error and crashes tools needing required keys). A fixed, sanitized
error is fed back for self-correction, bounded by a dedicated counter. Genuine
no-arg calls still work.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from machina.agent.runtime import _INVALID_ARGS_MESSAGE, Agent
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


class _SpyReadWOConnector:
    capabilities: ClassVar[list[str]] = ["read_work_orders"]

    def __init__(self) -> None:
        self.calls = 0

    async def connect(self) -> None:  # pragma: no cover
        pass

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    async def read_work_orders(self, **kwargs: Any) -> list[Any]:
        self.calls += 1
        return []


def _tc(name: str, arguments: str, call_id: str) -> Any:
    tc = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments
    tc.id = call_id
    return tc


class _MalformedThenAnswerLLM:
    def __init__(self) -> None:
        self.model = "fake:model"
        self._n = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "forced-final"

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._n += 1
        if self._n == 1:
            return {"content": "", "tool_calls": [_tc("read_work_orders", "{bad json", "b1")]}
        return {"content": "Done answering.", "tool_calls": None}


class _AlwaysMalformedLLM:
    def __init__(self) -> None:
        self.model = "fake:model"

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "forced-final after junk"

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        return {"content": "", "tool_calls": [_tc("read_work_orders", "{still bad", "b1")]}


class _NoArgEmptyLLM:
    def __init__(self) -> None:
        self.model = "fake:model"
        self._n = 0
        self.seen_messages: list[dict[str, Any]] | None = None

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "forced"

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._n += 1
        if self._n == 1:
            # Empty arguments string — a valid no-arg call, NOT a parse failure.
            return {"content": "", "tool_calls": [_tc("list_assets", "", "e1")]}
        self.seen_messages = list(messages)
        return {"content": "Listed.", "tool_calls": None}


class TestInvalidArgs:
    @pytest.mark.asyncio
    async def test_malformed_args_fed_back_not_executed(self) -> None:
        spy = _SpyReadWOConnector()
        agent = Agent(plant=_plant(), llm=_MalformedThenAnswerLLM(), connectors=[spy])
        text = await agent._llm_loop([{"role": "user", "content": "read WOs"}], "c1")
        assert text == "Done answering."
        # The malformed call was never executed with empty args.
        assert spy.calls == 0

    @pytest.mark.asyncio
    async def test_persistent_malformed_terminates(self) -> None:
        agent = Agent(
            plant=_plant(), llm=_AlwaysMalformedLLM(), connectors=[_SpyReadWOConnector()]
        )
        text = await agent._llm_loop([{"role": "user", "content": "read WOs"}], "c1")
        # Bounded by the dedicated counter — forced final answer, no infinite loop.
        assert text == "forced-final after junk"

    @pytest.mark.asyncio
    async def test_valid_no_arg_call_is_not_an_error(self) -> None:
        llm = _NoArgEmptyLLM()
        agent = Agent(plant=_plant(), llm=llm, connectors=[_ReadAssetsConnector()])
        text = await agent._llm_loop([{"role": "user", "content": "list all"}], "c1")
        assert text == "Listed."
        dumped = json.dumps(llm.seen_messages, default=str)
        # Empty args were treated as a no-arg call: list_assets ran (its result
        # reached the model) and no invalid-args error was fed back.
        assert _INVALID_ARGS_MESSAGE not in dumped
        assert "P-201" in dumped
