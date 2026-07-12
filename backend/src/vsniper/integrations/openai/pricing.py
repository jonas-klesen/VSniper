from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)
_warned_unknown: set[str] = set()

# Standard tier prices per 1M tokens: model -> (input_usd, output_usd, cached_input_usd).
# cached_input_usd is the discounted rate OpenAI bills for prompt tokens served from its
# prompt cache (roughly 0.1x for the gpt-5 family, 0.25x–0.5x for older models). These are
# best-effort estimates; local and unknown models are intentionally absent and report $0.
_PRICING: dict[str, tuple[float, float, float]] = {
    "gpt-5.5": (5.0, 30.0, 0.5),
    "gpt-5.4": (2.5, 15.0, 0.25),
    "gpt-5.4-mini": (0.75, 4.5, 0.075),
    "gpt-5.4-nano": (0.2, 1.25, 0.02),
    "gpt-5.2": (1.75, 14.0, 0.175),
    "gpt-5.1": (1.25, 10.0, 0.125),
    "gpt-5": (1.25, 10.0, 0.125),
    "gpt-5-mini": (0.25, 2.0, 0.025),
    "gpt-5-nano": (0.05, 0.4, 0.005),
    "gpt-4.1": (2.0, 8.0, 0.5),
    "gpt-4.1-mini": (0.4, 1.6, 0.1),
    "gpt-4.1-nano": (0.1, 0.4, 0.025),
    "gpt-4o": (2.5, 10.0, 1.25),
    "gpt-4o-mini": (0.15, 0.6, 0.075),
    "o1": (15.0, 60.0, 7.5),
    "o3": (2.0, 8.0, 0.5),
    "o3-mini": (1.1, 4.4, 0.55),
    "o4-mini": (1.1, 4.4, 0.275),
    "o1-mini": (1.1, 4.4, 0.55),
    "gemma-4-31b": (0.99, 1.49, 0.99),
}


def _family_pricing(model: str) -> tuple[float, float, float] | None:
    """Return pricing for the longest known prefix of *model* (e.g. 'gpt-4o-2024-11' → 'gpt-4o')."""
    parts = model.split("-")
    for length in range(len(parts), 0, -1):
        candidate = "-".join(parts[:length])
        if candidate in _PRICING:
            return _PRICING[candidate]
    return None


def compute_cost(model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
    """Return estimated USD cost for a call.

    Cached input tokens are billed at the model's discounted cached rate; the remaining
    (non-cached) input tokens and all output tokens are billed at the standard rate. Logs a
    warning once for unknown models (which, like local models, report $0).
    """
    pricing = _PRICING.get(model)
    if pricing is None:
        if model not in _warned_unknown:
            _warned_unknown.add(model)
            family = _family_pricing(model)
            if family:
                _logger.warning(
                    "Unknown model %r not in pricing table; estimating cost from nearest family match",
                    model,
                )
            else:
                _logger.warning(
                    "Unknown model %r not in pricing table and no family match found; cost reported as $0",
                    model,
                )
        pricing = _family_pricing(model)
        if pricing is None:
            return 0.0
    input_price, output_price, cached_price = pricing
    cached = max(0, min(cached_input_tokens, input_tokens))
    non_cached = input_tokens - cached
    return (
        non_cached * input_price + cached * cached_price + output_tokens * output_price
    ) / 1_000_000
