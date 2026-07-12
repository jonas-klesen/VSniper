from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vsniper.api.main import app
from vsniper.domain.contracts import SearchFilter
from vsniper.integrations.vinted.categories import (
    ALLOWED_CATEGORY_ALIASES_BY_ITEM,
    CategoryFilterError,
    category_ids_for_alias,
    clothing_items_for_size_group,
    default_category_filter_for_clothing_item,
    ensure_category_filter_for_clothing_item,
    resolved_category_ids_for_aliases,
)


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


def test_every_allowed_bucket_alias_resolves() -> None:
    for clothing_item, aliases in ALLOWED_CATEGORY_ALIASES_BY_ITEM.items():
        unresolved = [alias for alias in aliases if not category_ids_for_alias(alias)]
        assert unresolved == [], f"{clothing_item} has unresolved aliases: {unresolved}"
        assert resolved_category_ids_for_aliases(aliases)


def test_default_category_filter_is_available_for_every_bucket() -> None:
    for clothing_item in ALLOWED_CATEGORY_ALIASES_BY_ITEM:
        filter_item = default_category_filter_for_clothing_item(clothing_item)
        assert filter_item.field == "category"
        assert filter_item.mode == "include"
        assert filter_item.values
        assert resolved_category_ids_for_aliases(filter_item.values)


@pytest.mark.parametrize(
    ("description", "expected_items"),
    [
        ("Schuhe Männer", ["schuhe"]),
        ("Schuhe Frauen", ["schuhe"]),
        ("Hosen für Herren", ["hosen"]),
        ("Hüte für Erwachsene", ["kopf"]),
        ("Anzugjacken für Herren", ["obenrum_kalt"]),
        ("Kleidergrößen", ["obenrum_warm", "obenrum_mittel", "obenrum_kalt"]),
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
    filters = ensure_category_filter_for_clothing_item(
        [SearchFilter(field="size", label="Sizes", values=["M"], mode="include")],
        "obenrum_warm",
        strict=True,
    )

    assert filters[0].field == "category"
    assert filters[0].values == ["tops", "t-shirts"]
    assert filters[1].field == "size"


def test_unresolved_category_filter_is_replaced_in_lenient_mode() -> None:
    filters = ensure_category_filter_for_clothing_item(
        [SearchFilter(field="category", label="Category", values=["space wizard"], mode="include")],
        "hosen",
        strict=False,
    )

    assert filters == [default_category_filter_for_clothing_item("hosen")]


def test_mismatched_category_filter_is_rejected_in_strict_mode() -> None:
    with pytest.raises(CategoryFilterError, match="outside Hosen"):
        ensure_category_filter_for_clothing_item(
            [SearchFilter(field="category", label="Category", values=["schuhe"], mode="include")],
            "hosen",
            strict=True,
        )


def test_search_category_options_route_exposes_bucket_metadata(monkeypatch) -> None:
    fake_searches = SimpleNamespace(
        category_options=lambda: {
            clothing_item: {
                "label": clothing_item.title(),
                "description": f"{clothing_item} description",
                "default_aliases": [aliases[0]],
                "allowed_aliases": aliases,
                "alias_catalog_ids": {alias: category_ids_for_alias(alias) for alias in aliases},
                "resolved_catalog_ids": resolved_category_ids_for_aliases(aliases),
            }
            for clothing_item, aliases in ALLOWED_CATEGORY_ALIASES_BY_ITEM.items()
        }
    )
    monkeypatch.setattr("vsniper.api.routes.searches.get_state", lambda: SimpleNamespace(searches=fake_searches))

    response = TestClient(app).get("/api/searches/category-options")

    assert response.status_code == 200
    data = response.json()
    assert set(data) == set(ALLOWED_CATEGORY_ALIASES_BY_ITEM)
    assert data["hosen"]["default_aliases"] == [ALLOWED_CATEGORY_ALIASES_BY_ITEM["hosen"][0]]
    assert "cargo" in data["hosen"]["allowed_aliases"]
    assert data["hosen"]["alias_catalog_ids"]["cargo"] == category_ids_for_alias("cargo")
    assert data["hosen"]["resolved_catalog_ids"]
