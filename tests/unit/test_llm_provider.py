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
        assert provider.model == "openai:gpt-4o"
        assert provider.temperature == 0.1
        assert provider.max_tokens == 4096

    def test_custom_parameters(self) -> None:
        provider = LLMProvider(
            model="ollama:llama3",
            temperature=0.7,
            max_tokens=2048,
        )
        assert provider.model == "ollama:llama3"
        assert provider.temperature == 0.7
        assert provider.max_tokens == 2048


class TestLLMProviderComplete:
    """Test LLMProvider.complete() method."""

    @pytest.mark.asyncio
    async def test_complete_returns_text(self) -> None:
        provider = LLMProvider(model="test-model")
        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Bearing replacement procedure"))]
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
        with patch.dict(sys.modules, {"litellm": None}), pytest.raises(
            ImportError, match="litellm is required"
        ):
            await provider.complete(messages=[{"role": "user", "content": "test"}])


class TestLLMProviderCompleteWithTools:
    """Test LLMProvider.complete_with_tools() method."""

    @pytest.mark.asyncio
    async def test_complete_with_tools_returns_dict(self) -> None:
        provider = LLMProvider(model="test-model")
        mock_tool_calls = [
            SimpleNamespace(
                function=SimpleNamespace(name="create_work_order", arguments='{"asset_id": "P-201"}')
            )
        ]
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, tool_calls=mock_tool_calls)
                )
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
                SimpleNamespace(
                    message=SimpleNamespace(content="No tool needed", tool_calls=None)
                )
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
        with patch.dict(sys.modules, {"litellm": None}), pytest.raises(
            ImportError, match="litellm is required"
        ):
            await provider.complete_with_tools(
                messages=[{"role": "user", "content": "test"}],
                tools=[],
            )
