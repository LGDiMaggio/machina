"""LLM cost estimation via LiteLLM pricing data.

Provides ``estimate_cost(model, prompt_tokens, completion_tokens)``
which returns the estimated USD cost.  Unknown or local models
return 0.0 and log a warning once per model.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

_warned_models: set[str] = set()


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Estimate the USD cost of an LLM call.

    Uses LiteLLM's ``completion_cost`` for known models.  Returns 0.0
    for unknown/local models and logs a single warning per model.

    Args:
        model: LiteLLM model identifier (e.g. ``"gpt-4o"``, ``"claude-3-opus-20240229"``).
        prompt_tokens: Number of input tokens.
        completion_tokens: Number of output tokens.

    Returns:
        Estimated cost in USD, or 0.0 if the model is unknown.
    """
    if not model or prompt_tokens + completion_tokens == 0:
        return 0.0

    try:
        import litellm

        cost = litellm.completion_cost(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return float(cost)
    except ImportError:
        if model not in _warned_models:
            _warned_models.add(model)
            logger.warning("litellm_not_installed", model=model)
        return 0.0
    except Exception:
        if model not in _warned_models:
            _warned_models.add(model)
            logger.warning("cost_estimation_failed", model=model)
        return 0.0
