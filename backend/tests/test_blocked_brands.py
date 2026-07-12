"""Tests for the blocked-brands hard filter applied before VLM judging."""
from __future__ import annotations

from vsniper.services.search_service import _filter_blocked_brands, _normalize_blocked_brands


def test_filter_blocked_brands_drops_case_insensitive_matches() -> None:
    candidates = [
        {"id": "1", "brand": "Nike"},
        {"id": "2", "brand": "carhartt wip"},
        {"id": "3", "brand": "Adidas"},
    ]
    result = _filter_blocked_brands(candidates, ["nike", "Carhartt WIP"])
    assert [c["id"] for c in result] == ["3"]


def test_filter_blocked_brands_no_blocklist_returns_input_unchanged() -> None:
    candidates = [{"id": "1", "brand": "Nike"}]
    assert _filter_blocked_brands(candidates, []) == candidates


def test_filter_blocked_brands_missing_brand_field_is_never_blocked() -> None:
    candidates = [{"id": "1"}]
    assert _filter_blocked_brands(candidates, ["unknown"]) == candidates


def test_normalize_blocked_brands_dedupes_case_insensitively_keeping_first_casing() -> None:
    assert _normalize_blocked_brands(["Nike", " nike ", "NIKE", "Adidas", ""]) == ["Nike", "Adidas"]
