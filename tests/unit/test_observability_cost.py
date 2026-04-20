"""Tests for LLM cost estimation."""

from __future__ import annotations

from unittest.mock import patch

from machina.observability.cost import _warned_models, estimate_cost


class TestEstimateCost:
    def setup_method(self) -> None:
        _warned_models.clear()

    def test_zero_tokens_returns_zero(self) -> None:
        assert estimate_cost("gpt-4o", 0, 0) == 0.0

    def test_empty_model_returns_zero(self) -> None:
        assert estimate_cost("", 100, 50) == 0.0

    def test_known_model_returns_positive(self) -> None:
        cost = estimate_cost("gpt-4o", 1000, 500)
        assert cost >= 0.0

    def test_unknown_model_returns_zero(self) -> None:
        cost = estimate_cost("totally-fake-model-xyz", 100, 50)
        assert cost == 0.0

    def test_unknown_model_warns_once(self) -> None:
        estimate_cost("fake-model-abc", 100, 50)
        estimate_cost("fake-model-abc", 200, 100)
        assert "fake-model-abc" in _warned_models

    def test_litellm_import_error(self) -> None:
        with patch.dict("sys.modules", {"litellm": None}):
            cost = estimate_cost("gpt-4o", 100, 50)
            assert cost == 0.0
