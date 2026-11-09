from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vsniper.api.main import app
from vsniper.domain.contracts import SearchFilter
from vsniper.domain.contracts import CLOTHING_ITEM_LABELS
from vsniper.services.search_service import SearchService
from vsniper.integrations.vinted.categories import (
    category_ids_for_alias,
    clothing_items_for_size_group,
    default_category_filter_for_clothing_item,
    ensure_category_filter_for_clothing_item,
    resolved_category_ids_for_aliases,
)

# Vinted.de Herren catalog IDs, read from the live catalog tree on 2026-11-07.
EXPECTED_CATALOG_IDS = {
    "schuhe": ["1242", "1233"],
    "hosen": ["34", "257"],
    "obenrum_warm": ["76"],
    "obenrum_mittel": ["1811", "267", "1812", "1813", "264", "265", "1814"],
    "obenrum_kalt": ["1815", "266"],
    "jacken": ["1206"],
    "kopf": ["86"],
}


@pytest.mark.parametrize(
    ("alias", "expected_ids"),
    [
        ("Obenrum Warm", ["76", "77"]),
        ("obenrum_warm", ["76", "77"]),
        ("Obenrum Mittel", ["76", "79"]),
        ("obenrum_mittel", ["76", "79"]),
        ("Obenrum Kalt", ["267", "79", "1206", "2052", "2051"]),
        ("obenrum_kalt", ["267", "79", "1206", "2052", "2051"]),
        ("Kopf", ["86"]),
    ],
)
def test_bucket_category_aliases_resolve(alias: str, expected_ids: list[str]) -> None:
    assert category_ids_for_alias(alias) == expected_ids


def test_fixed_categories_are_exact_vinted_catalogs_without_overlap() -> None:
    owner: dict[str, str] = {}
    for clothing_item in CLOTHING_ITEM_LABELS:
        filter_item = default_category_filter_for_clothing_item(clothing_item)
        assert filter_item.field == "category"
        assert filter_item.mode == "include"
        # Each fixed title resolves to exactly one catalog ID, in table order.
        assert [category_ids_for_alias(value) for value in filter_item.values] == [
            [catalog_id] for catalog_id in EXPECTED_CATALOG_IDS[clothing_item]
        ]
        for catalog_id in EXPECTED_CATALOG_IDS[clothing_item]:
            assert catalog_id not in owner, f"{catalog_id} is in both {owner.get(catalog_id)} and {clothing_item}"
            owner[catalog_id] = clothing_item


@pytest.mark.parametrize(
    ("description", "expected_items"),
    [
        ("Schuhe Männer", ["schuhe"]),
        ("Schuhe Frauen", ["schuhe"]),
        ("Hosen für Herren", ["hosen"]),
        ("Hüte für Erwachsene", ["kopf"]),
        ("Anzugjacken für Herren", ["obenrum_kalt", "jacken"]),
        ("Kleidergrößen", ["obenrum_warm", "obenrum_mittel", "obenrum_kalt", "jacken"]),
        ("Gürtel für Herren", []),
        ("Socken für Herren", []),
        # Regression: a generic/ambiguous account size-group label can legitimately describe
        # a shoe size group. It must not collide with obenrum_warm/obenrum_mittel keywords.
        ("Größen Männer", []),
        ("S/M/L", []),
    ],
)
def test_clothing_items_for_size_group(description: str, expected_items: list[str]) -> None:
    assert clothing_items_for_size_group(description) == expected_items


def test_missing_category_filter_is_injected() -> None:
    size = SearchFilter(field="size", label="Sizes", values=["M"], mode="include")

    filters = ensure_category_filter_for_clothing_item([size], "obenrum_warm")

    assert filters == [default_category_filter_for_clothing_item("obenrum_warm"), size]


def test_stored_category_filter_is_replaced_by_the_fixed_categories() -> None:
    """Taste recomputes used to pick category aliases, producing overlapping parent/child
    catalogs (tops for both warm and mid layers). Categories are fixed per bucket now."""
    size = SearchFilter(field="size", label="Sizes", values=["L"], mode="include")
    stored = SearchFilter(field="category", label="Kategorie", values=["tops", "pullovers"], mode="include")

    filters = ensure_category_filter_for_clothing_item([stored, size], "obenrum_mittel")

    assert filters == [default_category_filter_for_clothing_item("obenrum_mittel"), size]
    assert resolved_category_ids_for_aliases(filters[0].values) == EXPECTED_CATALOG_IDS["obenrum_mittel"]


def test_search_category_options_route_exposes_fixed_categories(monkeypatch) -> None:
    searches = SearchService.__new__(SearchService)
    monkeypatch.setattr("vsniper.api.routes.searches.get_state", lambda: SimpleNamespace(searches=searches))

    response = TestClient(app).get("/api/searches/category-options")

    assert response.status_code == 200
    data = response.json()
    assert set(data) == set(CLOTHING_ITEM_LABELS)
    kalt = data["obenrum_kalt"]
    assert kalt["default_aliases"] == kalt["allowed_aliases"] == ["Strickpullover für den Winter", "Strickjacken"]
    assert kalt["resolved_catalog_ids"] == EXPECTED_CATALOG_IDS["obenrum_kalt"]
    jacken = data["jacken"]
    assert jacken["default_aliases"] == jacken["allowed_aliases"] == ["Jacken & Mäntel"]
    assert jacken["resolved_catalog_ids"] == EXPECTED_CATALOG_IDS["jacken"]
