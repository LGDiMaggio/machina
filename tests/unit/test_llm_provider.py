"""Tests for the LLM provider abstraction layer."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from machina.llm.provider import LLMProvider


def _make_fake_litellm(acompletion_mock: AsyncMock) -> ModuleType:
    """Create a fake litellm module with a mock acompletion."""
    mod = ModuleType("litellm")
    mod.acompletion = acompletion_mock  # type: ignore[attr-defined]
    return mod


class TestLLMProviderInit:
    """Test LLMProvider initialization."""

    def test_default_parameters(self) -> None:
        provider = LLMProvider()
        assert provider.model == "openai/gpt-4o"
        assert provider.temperature == 0.1
        assert provider.max_tokens == 4096

    def test_custom_parameters(self) -> None:
        provider = LLMProvider(
            model="ollama:llama3",
            temperature=0.7,
            max_tokens=2048,
        )
        assert provider.model == "ollama/llama3"
        assert provider.temperature == 0.7
        assert provider.max_tokens == 2048


class TestLLMProviderComplete:
    """Test LLMProvider.complete() method."""

    @pytest.mark.asyncio
    async def test_complete_returns_text(self) -> None:
        provider = LLMProvider(model="test-model")
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="Bearing replacement procedure"))
            ]
        )
        mock_acompletion = AsyncMock(return_value=mock_response)
        fake_litellm = _make_fake_litellm(mock_acompletion)
        with patch.dict(sys.modules, {"litellm": fake_litellm}):
            result = await provider.complete(
                messages=[{"role": "user", "content": "How to replace bearing?"}]
            )
        assert result == "Bearing replacement procedure"

    @pytest.mark.asyncio
    async def test_complete_passes_parameters(self) -> None:
        provider = LLMProvider(model="test-model", temperature=0.5, max_tokens=1024)
        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )
        mock_acompletion = AsyncMock(return_value=mock_response)
        fake_litellm = _make_fake_litellm(mock_acompletion)
        with patch.dict(sys.modules, {"litellm": fake_litellm}):
            await provider.complete(messages=[{"role": "user", "content": "test"}])

        mock_acompletion.assert_called_once_with(
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            temperature=0.5,
            max_tokens=1024,
            timeout=120.0,
        )

    @pytest.mark.asyncio
    async def test_complete_forwards_kwargs(self) -> None:
        provider = LLMProvider(model="test-model")
        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )
        mock_acompletion = AsyncMock(return_value=mock_response)
        fake_litellm = _make_fake_litellm(mock_acompletion)
        with patch.dict(sys.modules, {"litellm": fake_litellm}):
            await provider.complete(
                messages=[{"role": "user", "content": "test"}],
                top_p=0.9,
            )

        call_kwargs = mock_acompletion.call_args.kwargs
        assert call_kwargs["top_p"] == 0.9

    @pytest.mark.asyncio
    async def test_complete_raises_on_missing_litellm(self) -> None:
        provider = LLMProvider()
        with (
            patch.dict(sys.modules, {"litellm": None}),
            pytest.raises(ImportError, match="litellm is required"),
        ):
            await provider.complete(messages=[{"role": "user", "content": "test"}])


class TestLLMProviderCompleteWithTools:
    """Test LLMProvider.complete_with_tools() method."""

    @pytest.mark.asyncio
    async def test_complete_with_tools_returns_dict(self) -> None:
        provider = LLMProvider(model="test-model")
        mock_tool_calls = [
            SimpleNamespace(
                function=SimpleNamespace(
                    name="create_work_order", arguments='{"asset_id": "P-201"}'
                )
            )
        ]
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=mock_tool_calls))
            ]
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "create_work_order",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        mock_acompletion = AsyncMock(return_value=mock_response)
        fake_litellm = _make_fake_litellm(mock_acompletion)
        with patch.dict(sys.modules, {"litellm": fake_litellm}):
            result = await provider.complete_with_tools(
                messages=[{"role": "user", "content": "Create WO for P-201"}],
                tools=tools,
            )
        assert result["content"] is None
        assert result["tool_calls"] == mock_tool_calls

    @pytest.mark.asyncio
    async def test_complete_with_tools_passes_tools(self) -> None:
        provider = LLMProvider(model="test-model")
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="No tool needed", tool_calls=None))
            ]
        )
        tools = [{"type": "function", "function": {"name": "test_tool"}}]
        mock_acompletion = AsyncMock(return_value=mock_response)
        fake_litellm = _make_fake_litellm(mock_acompletion)
        with patch.dict(sys.modules, {"litellm": fake_litellm}):
            result = await provider.complete_with_tools(
                messages=[{"role": "user", "content": "hello"}],
                tools=tools,
            )

        call_kwargs = mock_acompletion.call_args.kwargs
        assert call_kwargs["tools"] == tools
        assert result["content"] == "No tool needed"
        assert result["tool_calls"] is None

    @pytest.mark.asyncio
    async def test_complete_with_tools_raises_on_missing_litellm(self) -> None:
        provider = LLMProvider()
        with (
            patch.dict(sys.modules, {"litellm": None}),
            pytest.raises(ImportError, match="litellm is required"),
        ):
            await provider.complete_with_tools(
                messages=[{"role": "user", "content": "test"}],
                tools=[],
            )


# ---------------------------------------------------------------------------
# Contract tests against the real LiteLLM library
# ---------------------------------------------------------------------------
#
# The classes above exercise LLMProvider through a hand-rolled litellm fake.
# Cheap and fast, but they cannot detect a future LiteLLM separator change —
# that is the blind spot that produced regression b48f649 (we passed
# "openai:gpt-4o" to LiteLLM which expected "openai/gpt-4o", every fake-based
# test was green and the bug still shipped). The tests below hit the real
# litellm.get_llm_provider parser, which is pure string logic (no network),
# so they stay fast and credential-free while anchoring the real contract.


class TestEmitTrace:
    """Test _emit_trace cost/token instrumentation."""

    def test_emit_trace_records_entry(self) -> None:
        from machina.observability.tracing import ActionTracer

        tracer = ActionTracer(max_entries=10)
        provider = LLMProvider(model="test-model", tracer=tracer)

        mock_response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50)
        )
        provider._emit_trace(mock_response, conversation_id="conv-1")

        entries = tracer.entries
        assert len(entries) == 1
        entry = entries[0]
        assert entry.action == "llm_call"
        assert entry.prompt_tokens == 100
        assert entry.completion_tokens == 50
        assert entry.total_tokens == 150
        assert entry.conversation_id == "conv-1"
        assert entry.model == "test-model"

    def test_emit_trace_handles_missing_usage(self) -> None:
        from machina.observability.tracing import ActionTracer

        tracer = ActionTracer(max_entries=10)
        provider = LLMProvider(model="test-model", tracer=tracer)

        mock_response = SimpleNamespace()
        provider._emit_trace(mock_response)

        entries = tracer.entries
        assert len(entries) == 1
        assert entries[0].prompt_tokens == 0
        assert entries[0].completion_tokens == 0

    def test_emit_trace_noop_without_tracer(self) -> None:
        provider = LLMProvider(model="test-model")
        mock_response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        )
        provider._emit_trace(mock_response)

    @pytest.mark.asyncio
    async def test_complete_calls_emit_trace(self) -> None:
        from machina.observability.tracing import ActionTracer

        tracer = ActionTracer(max_entries=10)
        provider = LLMProvider(model="test-model", tracer=tracer)

        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=50, completion_tokens=25),
        )
        mock_acompletion = AsyncMock(return_value=mock_response)
        fake_litellm = _make_fake_litellm(mock_acompletion)
        with patch.dict(sys.modules, {"litellm": fake_litellm}):
            await provider.complete(
                messages=[{"role": "user", "content": "test"}],
                conversation_id="conv-2",
            )

        entries = tracer.entries
        assert len(entries) == 1
        assert entries[0].prompt_tokens == 50
        assert entries[0].conversation_id == "conv-2"


class TestLiteLLMModelStringContract:
    """``LLMProvider.model`` must be a form LiteLLM accepts."""

    @pytest.mark.parametrize(
        ("user_input", "expected_normalized"),
        [
            ("openai:gpt-4o", "openai/gpt-4o"),
            ("anthropic:claude-opus-4-5", "anthropic/claude-opus-4-5"),
            ("ollama:llama3", "ollama/llama3"),
            # Users passing the already-normalized form must not see a
            # double-normalization that breaks LiteLLM (e.g. "openai//gpt-4o").
            ("openai/gpt-4o", "openai/gpt-4o"),
        ],
    )
    def test_normalizes_to_litellm_accepted_form(
        self, user_input: str, expected_normalized: str
    ) -> None:
        provider = LLMProvider(model=user_input)
        assert provider.model == expected_normalized

    def test_normalized_form_is_accepted_by_real_litellm(self) -> None:
        """Exercise the real LiteLLM parser — not a fake.

        This is the load-bearing test. If LiteLLM changes its expected
        separator in a future release, this test fails and we catch it
        before users do.
        """
        litellm = pytest.importorskip("litellm")

        provider = LLMProvider(model="openai:gpt-4o")
        model, custom_llm_provider, *_ = litellm.get_llm_provider(provider.model)
        assert model == "gpt-4o"
        assert custom_llm_provider == "openai"

    def test_unnormalized_colon_form_is_rejected_by_real_litellm(self) -> None:
        """Anchor the regression: the form we *used* to pass must still fail.

        If LiteLLM ever starts accepting the colon form, this test starts
        failing — at which point the normalization in ``LLMProvider`` is
        no longer load-bearing and can be reconsidered. The ``match=``
        guard keeps the failure readable if LiteLLM swaps the exception
        message text without changing its type.
        """
        litellm = pytest.importorskip("litellm")

        with pytest.raises(
            litellm.exceptions.BadRequestError,
            match="LLM Provider NOT provided",
        ):
            litellm.get_llm_provider("openai:gpt-4o")
