"""LLM provider — async wrapper around LiteLLM for maintenance-aware generation."""

from __future__ import annotations

from typing import Any


class LLMProvider:
    """Provider-agnostic LLM interface.

    Wraps LiteLLM to provide async ``complete()`` and
    ``complete_with_tools()`` methods.  Machina's agent layer uses
    this instead of calling LLM libraries directly.

    Args:
        model: Provider and model identifier (e.g. ``"openai:gpt-4o"``).
        temperature: Sampling temperature.
        max_tokens: Maximum tokens in the response.
    """

    def __init__(
        self,
        model: str = "openai:gpt-4o",
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        """Send a chat completion request and return the response text.

        Args:
            messages: Chat messages in OpenAI format.
            **kwargs: Extra parameters forwarded to LiteLLM.

        Returns:
            The assistant's response text.
        """
        try:
            import litellm
        except ImportError:
            msg = (
                "litellm is required for LLM calls. "
                "Install it with: pip install machina-ai[litellm]"
            )
            raise ImportError(msg) from None

        response = await litellm.acompletion(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            **kwargs,
        )
        return str(response.choices[0].message.content)

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion with tool/function definitions.

        Args:
            messages: Chat messages in OpenAI format.
            tools: Tool definitions (OpenAI function calling schema).
            **kwargs: Extra parameters forwarded to LiteLLM.

        Returns:
            The full response dict including any tool calls.
        """
        try:
            import litellm
        except ImportError:
            msg = (
                "litellm is required for LLM calls. "
                "Install it with: pip install machina-ai[litellm]"
            )
            raise ImportError(msg) from None

        response = await litellm.acompletion(
            model=self.model,
            messages=messages,
            tools=tools,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            **kwargs,
        )
        message = response.choices[0].message
        return {
            "content": message.content,
            "tool_calls": message.tool_calls,
        }
