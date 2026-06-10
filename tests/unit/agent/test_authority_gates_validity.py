"""U3/U6/U7 — Output-validity gate (leaked tool calls, reasoning-block scrub).

A model that emits a tool/function call as plain-text content (weak local
models) must never have that raw JSON shown as the answer. Leaked reads are
recovered through the normal path; leaked writes are never auto-executed.

U6 makes detection shape-based: any tool-call-shaped payload is intercepted
regardless of whether the tool name is a known builtin (R9). Hallucinated
tools (e.g. llama3's ``get_bearing_replacement_procedure``) are suppressed to
the fallback — never shown raw, never executed, never re-entered.

Disposition is per-agent: "known" means on THIS agent instance's tool surface
(``_known_tool_names``, derived from the same ``_get_available_tools`` source
dispatch uses), so capability-derived reads like ``get_work_order`` recover,
while the same name with no enabling connector stays suppressed.

U7 prepends a ``<think>...</think>`` scrub to the gate's validator chain:
reasoning models (deepseek-r1) emit their chain of thought in content, and it
must never reach the user (see :class:`TestThinkBlockScrub`).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from machina.agent.runtime import (
    _EMPTY_RESPONSE_FALLBACK,
    _REPEATED_RESPONSE_FALLBACK,
    _TOOL_CALL_LEAK_FALLBACK,
    Agent,
)
from machina.connectors.capabilities import Capability
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant
from machina.domain.work_order import WorkOrder, WorkOrderType


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


class _WorkOrderReadConnector:
    """GET_WORK_ORDER connector — puts the capability-derived read tool on the surface."""

    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.GET_WORK_ORDER})

    def __init__(self) -> None:
        self.fetched = 0

    async def connect(self) -> None:  # pragma: no cover
        pass

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    async def get_work_order(self, work_order_id: str) -> WorkOrder | None:
        self.fetched += 1
        return WorkOrder(
            id=work_order_id,
            type=WorkOrderType.CORRECTIVE,
            asset_id="P-201",
            description="Bearing replacement",
        )


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
    if shape == "C":
        return json.dumps({"function": name, "arguments": args})
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


class _LeakedCapabilityReadLLM:
    """Emits a get_work_order call as content once, then answers normally.

    ``get_work_order`` is a capability-derived tool (Capability.GET_WORK_ORDER),
    NOT one of the always-on builtins — the leak the 2026-06-10 eval baseline
    showed being misclassified as hallucinated by the static known-name set.
    """

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
            return {
                "content": _leak("get_work_order", {"work_order_id": "WO-001"}),
                "tool_calls": None,
            }
        return {
            "content": "WO-001 is a corrective bearing replacement on P-201.",
            "tool_calls": None,
        }


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

    def test_shape_c_function_string_key(self) -> None:
        """Gap family 6: the tool name is the string VALUE of a "function" key.

        The verbatim family the deepseek-r1:8b conversational eval baseline
        (2026-06-10) leaked as user-facing answer text — neither shape A
        (nested function object) nor shape B (top-level name key) matched it.
        """
        out = Agent._detect_leaked_tool_call(
            _leak("get_asset_details", {"asset_id": "P-201"}, shape="C")
        )
        assert out == ("get_asset_details", {"asset_id": "P-201"})

    def test_shape_c_parameters_key(self) -> None:
        raw = json.dumps({"function": "search_assets", "parameters": {"query": "pump"}})
        assert Agent._detect_leaked_tool_call(raw) == ("search_assets", {"query": "pump"})

    def test_shape_c_string_arguments_are_parsed(self) -> None:
        raw = json.dumps(
            {"function": "get_asset_details", "arguments": json.dumps({"asset_id": "P-201"})}
        )
        assert Agent._detect_leaked_tool_call(raw) == (
            "get_asset_details",
            {"asset_id": "P-201"},
        )

    def test_shape_c_unknown_tool_is_detected(self) -> None:
        out = Agent._detect_leaked_tool_call(_leak("totally_made_up", {"x": 1}, shape="C"))
        assert out == ("totally_made_up", {"x": 1})

    def test_shape_c_empty_function_string_is_detected(self) -> None:
        """An EMPTY-string "function" value is still a detector hit (not prose).

        Matches shapes A/B, which accept empty names via ``isinstance``: the
        empty name then dispositions as unknown in the callers and is
        suppressed fail-closed — `{"function": "", "arguments": {...}}` must
        never reach the user raw.
        """
        raw = json.dumps({"function": "", "arguments": {"x": 1}})
        assert Agent._detect_leaked_tool_call(raw) == ("", {"x": 1})

    def test_function_string_without_call_marker_is_not_a_leak(self) -> None:
        # A plain-data JSON answer with a string "function" field but no
        # arguments/parameters key is an answer, not a call (shape-based, R9).
        raw = json.dumps({"function": "pumping", "note": "centrifugal"})
        assert Agent._detect_leaked_tool_call(raw) is None

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


class TestDetectLeakedToolCallNormalization:
    """PR #55 detector-gap families (1)-(4): payloads normalized before detection."""

    def test_markdown_fenced_json_is_detected(self) -> None:
        raw = "```json\n" + _leak("search_assets", {"query": "x"}, shape="B") + "\n```"
        assert Agent._detect_leaked_tool_call(raw) == ("search_assets", {"query": "x"})

    def test_bare_fence_without_language_tag_is_detected(self) -> None:
        raw = "```\n" + _leak("search_assets", {"query": "x"}, shape="A") + "\n```"
        assert Agent._detect_leaked_tool_call(raw) == ("search_assets", {"query": "x"})

    def test_fence_with_missing_closer_is_detected(self) -> None:
        raw = "```json\n" + _leak("search_assets", {"query": "x"}, shape="B")
        assert Agent._detect_leaked_tool_call(raw) == ("search_assets", {"query": "x"})

    def test_fenced_non_call_json_is_not_a_leak(self) -> None:
        raw = '```json\n{"id": "P-201", "name": "Cooling Water Pump"}\n```'
        assert Agent._detect_leaked_tool_call(raw) is None

    def test_fenced_code_that_is_not_json_is_not_a_leak(self) -> None:
        raw = "```python\nprint('hello')\n```"
        assert Agent._detect_leaked_tool_call(raw) is None

    def test_top_level_array_first_call_wins(self) -> None:
        raw = json.dumps(
            [
                {"name": "search_assets", "arguments": {"query": "pump"}},
                {"name": "get_asset_details", "arguments": {"asset_id": "P-201"}},
            ]
        )
        assert Agent._detect_leaked_tool_call(raw) == ("search_assets", {"query": "pump"})

    def test_array_of_non_call_objects_is_not_a_leak(self) -> None:
        raw = json.dumps([{"id": "P-201", "name": "Cooling Water Pump"}])
        assert Agent._detect_leaked_tool_call(raw) is None

    def test_tool_calls_wrapper_is_unwrapped(self) -> None:
        raw = json.dumps(
            {
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_bearing_replacement_procedure",
                            "arguments": json.dumps({"asset_id": "P-201"}),
                        },
                    }
                ]
            }
        )
        assert Agent._detect_leaked_tool_call(raw) == (
            "get_bearing_replacement_procedure",
            {"asset_id": "P-201"},
        )

    def test_empty_tool_calls_wrapper_is_not_a_leak(self) -> None:
        assert Agent._detect_leaked_tool_call('{"tool_calls": []}') is None

    def test_single_quoted_pseudo_json_is_detected(self) -> None:
        raw = "{'name': 'create_work_order', 'arguments': {'asset_id': 'P-201'}}"
        assert Agent._detect_leaked_tool_call(raw) == (
            "create_work_order",
            {"asset_id": "P-201"},
        )

    def test_single_quoted_non_call_dict_is_not_a_leak(self) -> None:
        assert Agent._detect_leaked_tool_call("{'id': 'P-201', 'name': 'Pump'}") is None

    # Shape C (gap family 6) x the PR #55 normalization combos: each
    # pre-detection normalization must compose with the string-valued
    # "function" key, not just with shapes A/B.

    def test_shape_c_markdown_fenced_json_is_detected(self) -> None:
        raw = "```json\n" + _leak("get_asset_details", {"asset_id": "P-201"}, shape="C") + "\n```"
        assert Agent._detect_leaked_tool_call(raw) == (
            "get_asset_details",
            {"asset_id": "P-201"},
        )

    def test_shape_c_top_level_array_first_call_wins(self) -> None:
        raw = json.dumps(
            [
                {"function": "get_asset_details", "arguments": {"asset_id": "P-201"}},
                {"function": "search_assets", "arguments": {"query": "pump"}},
            ]
        )
        assert Agent._detect_leaked_tool_call(raw) == (
            "get_asset_details",
            {"asset_id": "P-201"},
        )

    def test_shape_c_tool_calls_wrapper_is_unwrapped(self) -> None:
        raw = json.dumps(
            {"tool_calls": [{"function": "get_asset_details", "arguments": {"asset_id": "P-201"}}]}
        )
        assert Agent._detect_leaked_tool_call(raw) == (
            "get_asset_details",
            {"asset_id": "P-201"},
        )

    def test_shape_c_single_quoted_pseudo_json_is_detected(self) -> None:
        raw = "{'function': 'get_asset_details', 'arguments': {'asset_id': 'P-201'}}"
        assert Agent._detect_leaked_tool_call(raw) == (
            "get_asset_details",
            {"asset_id": "P-201"},
        )


class TestLeakedToolCallFragmentTripwire:
    """PR #55 detector-gap family (5): truncated/partial tool-call JSON.

    The fragment never parses, so the full detector cannot return (name, args);
    the finalize-only tripwire suppresses it instead (fail-closed, R9/U6).
    """

    def test_truncated_shape_a_call_trips(self) -> None:
        raw = '{"type": "function", "function": {"name": "create_work_order", "arguments": {"asset_id": "P-2'
        assert Agent._looks_like_leaked_tool_call_fragment(raw) is True

    def test_truncated_shape_b_call_trips(self) -> None:
        raw = '{"name": "search_assets", "arguments": {"query": "pu'
        assert Agent._looks_like_leaked_tool_call_fragment(raw) is True

    def test_truncated_shape_c_call_trips(self) -> None:
        # Gap family 6, truncated: the string-valued "function" key counts as
        # the name marker, "arguments" as the call marker.
        raw = '{"function": "create_work_order", "arguments": {"asset_id": "P-2'
        assert Agent._looks_like_leaked_tool_call_fragment(raw) is True

    def test_parsable_call_does_not_trip(self) -> None:
        # A payload the full detector owns must never be double-handled here.
        raw = _leak("search_assets", {"query": "x"}, shape="B")
        assert Agent._looks_like_leaked_tool_call_fragment(raw) is False

    def test_parsable_non_call_json_does_not_trip(self) -> None:
        # A deliberate JSON answer that merely contains a "name" field parses
        # cleanly, so the tripwire (unparsable-only) leaves it alone.
        raw = json.dumps({"name": "P-201", "arguments_note": "none", "function": "pumping"})
        assert Agent._looks_like_leaked_tool_call_fragment(raw) is False

    def test_ordinary_prose_does_not_trip(self) -> None:
        assert Agent._looks_like_leaked_tool_call_fragment("The pump needs a bearing.") is False

    def test_truncated_plain_data_json_does_not_trip(self) -> None:
        # Truncated JSON WITHOUT a call-marker key is not a tool call.
        raw = '{"id": "P-201", "name": "Cooling Water Pump", "location": "Buil'
        assert Agent._looks_like_leaked_tool_call_fragment(raw) is False


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
    async def test_leaked_capability_read_is_recovered_on_surface(self) -> None:
        """A leaked capability-derived READ (get_work_order) recovers.

        Disposition derives from the agent INSTANCE's tool surface — the same
        ``_get_available_tools`` source dispatch uses — so a tool enabled by a
        connector capability is "known" even though it is not an always-on
        builtin. Pins the 2026-06-10 eval-baseline fix (the static
        ``BUILTIN_TOOLS`` set misclassified this leak as hallucinated).
        """
        conn = _WorkOrderReadConnector()
        agent = Agent(plant=_plant(), llm=_LeakedCapabilityReadLLM(), connectors=[conn])
        text = await agent._llm_loop([{"role": "user", "content": "status of WO-001?"}], "c1")
        assert text == "WO-001 is a corrective bearing replacement on P-201."
        assert conn.fetched == 1  # the read executed exactly once (bounded re-entry)

    @pytest.mark.asyncio
    async def test_leaked_capability_read_off_surface_is_suppressed(self) -> None:
        """The SAME leak with no GET_WORK_ORDER connector is suppressed.

        Without the capability the tool is not on this agent's surface, so the
        leak dispositions as hallucinated: fallback, never executed, never
        re-entered — per-instance, fail-closed.
        """
        llm = _LeakedCapabilityReadLLM()
        agent = Agent(plant=_plant(), llm=llm, connectors=[_ReadAssetsConnector()])
        text = await agent._llm_loop([{"role": "user", "content": "status of WO-001?"}], "c1")
        assert text == _TOOL_CALL_LEAK_FALLBACK
        assert llm._n == 1  # suppressed immediately, never re-entered

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


class TestThinkBlockScrub:
    """U7 — the gate scrubs reasoning-model ``<think>`` blocks before validating.

    deepseek-r1-style models emit their chain of thought as
    ``<think>...</think>`` inside message content. The scrub is
    strip-and-keep-remainder: every think block is removed (case-insensitive,
    spanning newlines) and any surviving answer proceeds through the normal
    validator chain; a think-only response strips to empty and falls into the
    existing empty-response fallback. Pinned choice: an UNCLOSED ``<think>``
    swallows everything to end-of-string — a weak model that truncates
    mid-reasoning produced no answer, so text after the opener is reasoning,
    never answer.
    """

    _ANSWER = "Replace the bearing on P-201 within 48 hours."

    def test_think_block_plus_answer_keeps_answer(self) -> None:
        agent = Agent()
        raw = f"<think>User asks about P-201. Check the bearing history.</think>\n{self._ANSWER}"
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response=raw)
        assert resp.text == self._ANSWER
        assert "<think" not in resp.text
        assert resp.is_fallback is False

    def test_scrub_is_case_insensitive_and_spans_newlines(self) -> None:
        agent = Agent()
        raw = f"<THINK>line one\nline two\nline three</THINK>{self._ANSWER}"
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response=raw)
        assert resp.text == self._ANSWER
        assert "line two" not in resp.text

    def test_multiple_think_blocks_all_removed(self) -> None:
        agent = Agent()
        raw = f"<think>first</think>{self._ANSWER}<think>second</think>"
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response=raw)
        assert resp.text == self._ANSWER
        assert "first" not in resp.text
        assert "second" not in resp.text

    def test_think_only_response_falls_back(self) -> None:
        agent = Agent()
        raw = "<think>only reasoning in here, never an answer</think>"
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response=raw)
        assert resp.text == _EMPTY_RESPONSE_FALLBACK
        assert resp.is_fallback is True
        assert "reasoning" not in resp.text

    def test_unclosed_think_block_scrubs_to_end_of_string(self) -> None:
        """Pinned: an unclosed ``<think>`` means everything after it is reasoning.

        Weak models truncate mid-reasoning; the text after the opener — even
        if it reads like an answer — is chain-of-thought, so the whole tail is
        scrubbed and the empty-response fallback fires.
        """
        agent = Agent()
        raw = f"<think>reasoning that never closes. {self._ANSWER}"
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response=raw)
        assert resp.text == _EMPTY_RESPONSE_FALLBACK
        assert resp.is_fallback is True
        assert self._ANSWER not in resp.text

    def test_orphan_closer_without_opener_drops_reasoning(self) -> None:
        """deepseek-r1 via some serving stacks emits reasoning WITHOUT the
        opener, ending with a bare ``</think>`` before the answer — the
        pre-closer text is reasoning by construction and must be dropped."""
        agent = Agent()
        raw = f"The user asks about P-201. Check the bearing history.</think>{self._ANSWER}"
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response=raw)
        assert resp.text == self._ANSWER
        assert "</think>" not in resp.text
        assert "bearing history" not in resp.text
        assert resp.is_fallback is False

    def test_nested_openers_leave_no_stray_closer(self) -> None:
        """Nested ``<think>`` openers leave a stray closer after the
        non-greedy sub; the orphan-closer pass removes it AND the trailing
        reasoning before it."""
        agent = Agent()
        raw = f"<think>outer<think>inner</think>more outer</think>{self._ANSWER}"
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response=raw)
        assert resp.text == self._ANSWER
        assert "think" not in resp.text
        assert "outer" not in resp.text
        assert resp.is_fallback is False

    def test_think_plus_echo_resolves_to_one_fallback(self) -> None:
        """Validator short-circuit: scrub then echo → exactly ONE fallback."""
        long_answer = (
            "I'm a specialized maintenance assistant powered by the Machina "
            "framework. I can help with equipment information, maintenance "
            "history, procedures and manuals, failure diagnosis, spare parts, "
            "work orders, and maintenance schedules. What shall we do today?"
        )
        agent = Agent()
        agent._finalize_turn(chat_id="c1", user_text="q1", raw_response=long_answer)
        r2 = agent._finalize_turn(
            chat_id="c1",
            user_text="q2 — a different question",
            raw_response=f"<think>they asked again, reuse my intro</think>{long_answer}",
        )
        # One fallback, not stacked: the scrubbed remainder is the echo, so the
        # echo guard fires once and its message is delivered verbatim.
        assert r2.is_fallback is True
        assert r2.text == _REPEATED_RESPONSE_FALLBACK

    def test_clean_output_byte_identical(self) -> None:
        """No spurious stripping: a normal answer passes through unchanged.

        Includes near-miss angle-bracket tags (``<thinking>``, ``<b>``) and
        leading whitespace to pin that the scrub only fires on real
        ``<think>`` blocks and never trims an untouched answer.
        """
        agent = Agent()
        text = (
            "  I think P-201's impeller is fine — my <thinking aloud> note "
            "stays.\nUse <b>bold</b> markup if needed."
        )
        resp = agent._finalize_turn(chat_id="c1", user_text="q", raw_response=text)
        assert resp.text == text
        assert resp.is_fallback is False
