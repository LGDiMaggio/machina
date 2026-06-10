"""U3/U6 — Output-validity gate (leaked tool-call detection).

A model that emits a tool/function call as plain-text content (weak local
models) must never have that raw JSON shown as the answer. Leaked reads are
recovered through the normal path; leaked writes are never auto-executed.

U6 makes detection shape-based: any tool-call-shaped payload is intercepted
regardless of whether the tool name is a known builtin (R9). Hallucinated
tools (e.g. llama3's ``get_bearing_replacement_procedure``) are suppressed to
the fallback — never shown raw, never executed, never re-entered.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from machina.agent.runtime import _TOOL_CALL_LEAK_FALLBACK, Agent
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


class _SpyWriteConnector:
    """CREATE_WORK_ORDER connector that records whether a write executed."""

    capabilities: ClassVar[list[str]] = ["create_work_order"]

    def __init__(self) -> None:
        self.created = 0

    async def connect(self) -> None:  # pragma: no cover
        pass

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    async def create_work_order(self, wo: Any) -> Any:  # pragma: no cover
        self.created += 1
        return wo


def _leak(name: str, args: dict[str, Any], *, shape: str = "A") -> str:
    if shape == "A":
        return json.dumps({"type": "function", "function": {"name": name, "arguments": args}})
    return json.dumps({"name": name, "arguments": args})


class _LeakedReadLLM:
    """Emits a search_assets call as content once, then answers normally."""

    def __init__(self) -> None:
        self.model = "fake:model"
        self._n = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "complete-path"

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._n += 1
        if self._n == 1:
            return {"content": _leak("search_assets", {"query": "pump"}), "tool_calls": None}
        return {"content": "Here are the assets you asked about.", "tool_calls": None}


class _LeakedWriteLLM:
    def __init__(self) -> None:
        self.model = "fake:model"

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "complete-path"

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        args = {"asset_id": "P-201", "type": "corrective", "priority": "high", "description": "x"}
        return {"content": _leak("create_work_order", args), "tool_calls": None}


class _AlwaysLeaksReadLLM:
    def __init__(self) -> None:
        self.model = "fake:model"

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "complete-path"

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        return {"content": _leak("search_assets", {"query": "pump"}), "tool_calls": None}


# The verbatim payload llama3 emitted as content (hallucinated tool name not in
# the dispatch surface) — the leak U6 fixes. Includes the extra "id" key.
_HALLUCINATED_LEAK = json.dumps(
    {
        "id": "call_8h2k",
        "type": "function",
        "function": {
            "name": "get_bearing_replacement_procedure",
            "arguments": {"asset_id": "P-201"},
        },
    }
)


class _HallucinatedToolLLM:
    """Emits a hallucinated (unknown-name) tool call as content, every time."""

    def __init__(self) -> None:
        self.model = "fake:model"
        self.calls = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "complete-path"

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self.calls += 1
        return {"content": _HALLUCINATED_LEAK, "tool_calls": None}


class TestDetectLeakedToolCall:
    def test_shape_a_function_wrapper(self) -> None:
        out = Agent._detect_leaked_tool_call(_leak("search_assets", {"query": "x"}, shape="A"))
        assert out == ("search_assets", {"query": "x"})

    def test_shape_b_bare_name(self) -> None:
        out = Agent._detect_leaked_tool_call(_leak("search_assets", {"query": "x"}, shape="B"))
        assert out == ("search_assets", {"query": "x"})

    def test_string_arguments_are_parsed(self) -> None:
        raw = json.dumps({"name": "search_assets", "arguments": json.dumps({"query": "x"})})
        assert Agent._detect_leaked_tool_call(raw) == ("search_assets", {"query": "x"})

    def test_unknown_tool_is_detected_shape_a(self) -> None:
        """U6/R9: detection is shape-based — an unknown name is still a hit.

        Reproduces the llama3 leak: ``get_bearing_replacement_procedure`` is
        not a builtin tool, but the payload IS tool-call-shaped, so the
        detector must return it for the caller to disposition (suppress).
        """
        out = Agent._detect_leaked_tool_call(_HALLUCINATED_LEAK)
        assert out == ("get_bearing_replacement_procedure", {"asset_id": "P-201"})

    def test_unknown_tool_is_detected_shape_b(self) -> None:
        out = Agent._detect_leaked_tool_call(_leak("totally_made_up", {"x": 1}, shape="B"))
        assert out == ("totally_made_up", {"x": 1})

    def test_unknown_tool_string_arguments_are_parsed(self) -> None:
        raw = json.dumps(
            {"name": "totally_made_up", "arguments": json.dumps({"asset_id": "P-201"})}
        )
        assert Agent._detect_leaked_tool_call(raw) == (
            "totally_made_up",
            {"asset_id": "P-201"},
        )

    def test_ordinary_prose_is_not_a_leak(self) -> None:
        assert Agent._detect_leaked_tool_call("The pump P-201 needs a new bearing.") is None
        assert Agent._detect_leaked_tool_call('Config is {"x": 1} for now.') is None


class TestLoopLeakHandling:
    @pytest.mark.asyncio
    async def test_leaked_read_is_recovered_not_shown(self) -> None:
        agent = Agent(plant=_plant(), llm=_LeakedReadLLM(), connectors=[_ReadAssetsConnector()])
        text = await agent._llm_loop([{"role": "user", "content": "list assets"}], "c1")
        assert text == "Here are the assets you asked about."
        assert "function" not in text  # no raw tool-call JSON leaked

    @pytest.mark.asyncio
    async def test_leaked_write_is_suppressed_not_executed(self) -> None:
        spy = _SpyWriteConnector()
        agent = Agent(plant=_plant(), llm=_LeakedWriteLLM(), connectors=[spy])
        text = await agent._llm_loop([{"role": "user", "content": "make a WO"}], "c1")
        assert text == _TOOL_CALL_LEAK_FALLBACK
        assert spy.created == 0  # the leaked write never fired

    @pytest.mark.asyncio
    async def test_repeated_leak_is_bounded(self) -> None:
        agent = Agent(
            plant=_plant(), llm=_AlwaysLeaksReadLLM(), connectors=[_ReadAssetsConnector()]
        )
        text = await agent._llm_loop([{"role": "user", "content": "list assets"}], "c1")
        assert text == _TOOL_CALL_LEAK_FALLBACK

    @pytest.mark.asyncio
    async def test_hallucinated_tool_is_suppressed_never_executed(self) -> None:
        """U6/R9 loop seam: unknown-name leak → fallback; nothing executes."""
        spy = _SpyWriteConnector()
        agent = Agent(
            plant=_plant(),
            llm=_HallucinatedToolLLM(),
            connectors=[_ReadAssetsConnector(), spy],
        )
        text = await agent._llm_loop([{"role": "user", "content": "replace bearing"}], "c1")
        assert text == _TOOL_CALL_LEAK_FALLBACK
        assert "get_bearing_replacement_procedure" not in text  # raw JSON never shown
        assert spy.created == 0  # hallucinated call never executed

    @pytest.mark.asyncio
    async def test_repeated_hallucinated_leak_is_bounded(self) -> None:
        """U6/R9: a model that hallucinates every iteration cannot loop.

        An unknown tool is never re-entered, so the very first leak ends the
        turn with the fallback — no re-entry budget is even consumed.
        """
        llm = _HallucinatedToolLLM()
        agent = Agent(plant=_plant(), llm=llm, connectors=[_ReadAssetsConnector()])
        text = await agent._llm_loop([{"role": "user", "content": "replace bearing"}], "c1")
        assert text == _TOOL_CALL_LEAK_FALLBACK
        assert llm.calls == 1  # suppressed immediately, never re-entered

    @pytest.mark.asyncio
    async def test_tool_shaped_json_answer_is_suppressed_fail_closed(self) -> None:
        """False-positive pin: a deliberate JSON answer in tool-call shape is suppressed.

        If the user asks e.g. "show me an example tool-call payload" and the
        model answers with a bare JSON object matching the tool-call shape,
        the gate suppresses it to the fallback. This is the ACCEPTED
        fail-closed trade-off from the origin doc (R9): with shape-based
        detection there is no way to distinguish a quoted example from a real
        leak, and showing the fallback is strictly safer than ever showing a
        leaked call raw.
        """

        class _JsonExampleLLM:
            model = "fake:model"

            async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
                return "complete-path"

            async def complete_with_tools(
                self,
                messages: list[dict[str, str]],
                tools: list[dict[str, Any]],
                **kwargs: Any,
            ) -> dict[str, Any]:
                example = json.dumps({"name": "example_tool", "arguments": {"x": 1}})
                return {"content": example, "tool_calls": None}

        agent = Agent(plant=_plant(), llm=_JsonExampleLLM(), connectors=[])
        text = await agent._llm_loop(
            [{"role": "user", "content": "show me an example tool-call payload"}], "c1"
        )
        assert text == _TOOL_CALL_LEAK_FALLBACK


class TestFinalizeBackstop:
    def test_tool_call_text_never_reaches_user(self) -> None:
        agent = Agent()
        leaked = _leak("search_assets", {"query": "x"})
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response=leaked)
        assert resp.text == _TOOL_CALL_LEAK_FALLBACK
        assert resp.is_fallback is True

    def test_hallucinated_tool_call_never_reaches_user(self) -> None:
        """U6/R9 backstop: any detector hit is suppressed — unknown names too."""
        agent = Agent()
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response=_HALLUCINATED_LEAK)
        assert resp.text == _TOOL_CALL_LEAK_FALLBACK
        assert resp.is_fallback is True
        assert "get_bearing_replacement_procedure" not in resp.text

    def test_normal_answer_passes_backstop(self) -> None:
        agent = Agent()
        resp = agent._finalize_turn(
            chat_id="c1", user_text="q", raw_response="P-201 is a cooling pump."
        )
        assert resp.text == "P-201 is a cooling pump."
        assert resp.is_fallback is False
