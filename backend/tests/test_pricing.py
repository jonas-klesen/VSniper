from __future__ import annotations

from vsniper.integrations.openai.pricing import compute_cost


def test_compute_cost_charges_cached_tokens_at_discounted_rate() -> None:
    # gpt-5: input 1.25, output 10.0, cached 0.125 per 1M tokens.
    # 1000 input of which 800 cached, 500 output:
    #   non-cached 200 * 1.25 + cached 800 * 0.125 + output 500 * 10.0 = 250 + 100 + 5000 = 5350 / 1e6
    cost = compute_cost("gpt-5", input_tokens=1000, output_tokens=500, cached_input_tokens=800)
    assert cost == (200 * 1.25 + 800 * 0.125 + 500 * 10.0) / 1_000_000


def test_compute_cost_without_cache_matches_full_input_rate() -> None:
    cost = compute_cost("gpt-5", input_tokens=1000, output_tokens=500)
    assert cost == (1000 * 1.25 + 500 * 10.0) / 1_000_000


def test_compute_cost_clamps_cached_tokens_to_input() -> None:
    # Cached count exceeding input is clamped so cost never goes negative.
    cost = compute_cost("gpt-5", input_tokens=100, output_tokens=0, cached_input_tokens=999)
    assert cost == (100 * 0.125) / 1_000_000


def test_compute_cost_unknown_and_local_models_report_zero() -> None:
    assert compute_cost("qwen2.5-vl-7b", input_tokens=10_000, output_tokens=10_000) == 0.0
    assert compute_cost("some-future-model", input_tokens=10_000, output_tokens=10_000, cached_input_tokens=5_000) == 0.0


def test_compute_cost_family_fallback_matches_prefix() -> None:
    # Dated model id falls back to the nearest known family prefix.
    dated = compute_cost("gpt-4o-2024-11-20", input_tokens=1000, output_tokens=0)
    base = compute_cost("gpt-4o", input_tokens=1000, output_tokens=0)
    assert dated == base
