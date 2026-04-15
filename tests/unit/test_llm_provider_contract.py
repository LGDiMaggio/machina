"""Contract tests for ``LLMProvider`` against the real LiteLLM library.

The rest of ``tests/unit/test_llm_provider.py`` uses a hand-rolled fake of
``litellm.acompletion`` — which is cheap and fast, but doesn't prove that
LiteLLM itself accepts the model string ``LLMProvider`` produces. That's
exactly the blind spot that produced regression ``b48f649`` (LiteLLM
expected ``openai/gpt-4o`` but we passed ``openai:gpt-4o``; every fake-
based test was green and the bug still shipped).

These tests hit the real ``litellm.get_llm_provider`` function. They make
no network calls — ``get_llm_provider`` is a pure string parser — so they
are safe to run in CI without credentials or cassettes.
"""

from __future__ import annotations

import pytest

from machina.llm.provider import LLMProvider


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
        no longer load-bearing and can be reconsidered.
        """
        litellm = pytest.importorskip("litellm")

        with pytest.raises(litellm.exceptions.BadRequestError):
            litellm.get_llm_provider("openai:gpt-4o")
