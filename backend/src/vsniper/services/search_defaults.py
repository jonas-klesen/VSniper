from __future__ import annotations

from datetime import UTC, datetime

from vsniper.domain.contracts import CLOTHING_ITEM_LABELS, ClothingItem
from vsniper.integrations.vinted.categories import default_category_filter_for_clothing_item

CANONICAL_CLOTHING_ITEMS: tuple[ClothingItem, ...] = tuple(CLOTHING_ITEM_LABELS.keys())
CANONICAL_SEARCH_ORDER: dict[ClothingItem, int] = {
    clothing_item: index for index, clothing_item in enumerate(CANONICAL_CLOTHING_ITEMS)
}


def canonical_search_id(clothing_item: ClothingItem | str) -> str:
    return f"search-{clothing_item}"


def canonical_search_name(clothing_item: ClothingItem) -> str:
    return CLOTHING_ITEM_LABELS[clothing_item]


def canonical_search_values(clothing_item: ClothingItem, *, now: datetime | None = None) -> dict:
    created_at = now or datetime.now(UTC)
    category_filter = default_category_filter_for_clothing_item(clothing_item)
    return {
        "id": canonical_search_id(clothing_item),
        "name": canonical_search_name(clothing_item),
        "clothing_item": clothing_item,
        "query": "",
        "region": "de",
        "filters": [category_filter.model_dump(mode="json")],
        "alert_threshold": None,
        "enabled": False,
        "last_claimed_at": None,
        "last_run_at": None,
        "run_status": "idle",
        "last_found_count": 0,
        "created_at": created_at,
    }
