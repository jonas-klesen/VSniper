from __future__ import annotations

from typing import cast

from vsniper.domain.contracts import (
    CLOTHING_ITEM_DESCRIPTIONS,
    CLOTHING_ITEM_LABELS,
    ClothingItem,
    SearchFilter,
)
from vsniper.integrations.vinted.category_aliases import CATEGORY_ALIAS_IDS


_CATEGORY_FIELDS = {"category", "catalog"}

DEFAULT_CATEGORY_ALIASES_BY_ITEM: dict[ClothingItem, list[str]] = {
    "schuhe": ["schuhe", "sneakers"],
    "hosen": ["hosen", "jeans", "shorts"],
    "obenrum_warm": ["tops", "t-shirts"],
    "obenrum_mittel": ["tops", "pullovers"],
    "obenrum_kalt": ["hoodies", "pullovers", "jackets", "coats", "vests"],
    "kopf": ["hats"],
}

ALLOWED_CATEGORY_ALIASES_BY_ITEM: dict[ClothingItem, list[str]] = {
    "schuhe": [
        "schuhe",
        "shoes",
        "sneakers",
        "sneaker",
        "turnschuhe",
        "sports shoes",
        "boots",
        "stiefel",
    ],
    "hosen": [
        "hosen",
        "hose",
        "trousers",
        "pants",
        "cargo",
        "cargos",
        "cargo pants",
        "jeans",
        "denim",
        "shorts",
        "joggers",
        "chinos",
        "wide leg trousers",
    ],
    "obenrum_warm": [
        "obenrum warm",
        "obenrum_warm",
        "tops",
        "tops and t shirts",
        "tops and t-shirts",
        "t shirts and tops",
        "t-shirts and tops",
        "t shirts",
        "t-shirts",
        "tshirts",
        "shirts",
        "hemden",
        "hemd",
        "polo shirts",
        "polos",
        "tank tops",
        "top vests",
    ],
    "obenrum_mittel": [
        "obenrum mittel",
        "obenrum_mittel",
        "tops",
        "longsleeves",
        "long sleeves",
        "long sleeve tops",
        "long sleeve shirts",
        "langarmshirts",
        "shirts",
        "hemden",
        "pullovers",
        "pullover",
        "sweaters",
        "sweater",
        "crewneck",
        "crewnecks",
        "knit sweater",
        "knitted sweater",
        "strickpullover",
        "cardigan",
        "cardigans",
        "turtleneck",
        "turtleneck sweater",
    ],
    "obenrum_kalt": [
        "obenrum kalt",
        "obenrum_kalt",
        "hoodies",
        "hoodie",
        "pullis",
        "pullovers",
        "pullover",
        "sweaters",
        "outerwear",
        "jackets and coats",
        "jacken und maentel",
        "jackets",
        "jacket",
        "jacken",
        "jacke",
        "coats",
        "coat",
        "maentel",
        "mantel",
        "vests",
        "vest",
        "westen",
        "weste",
        "fleece jacket",
        "windbreaker",
        "puffer jacket",
        "bomber jacket",
        "denim jacket",
        "shacket",
        "military jacket",
        "utility jacket",
    ],
    "kopf": [
        "kopf",
        "hats",
        "caps",
        "hats and caps",
        "baseball caps",
        "baseball cap",
        "beanies",
        "beanie",
        "muetzen",
        "muetze",
        "kappen",
        "kappe",
    ],
}


# Keyword fragments matched against the (normalised) description of a Vinted account size
# group (e.g. "Schuhe Männer", "Hosen für Herren", "Kleidergrößen") to decide which clothing
# item buckets that group's sizes apply to. Substring matching (rather than exact aliases)
# keeps this resilient to gender variants Vinted reports ("Schuhe Männer"/"Schuhe Frauen").
# Generic clothing-size groups and suit-jacket sizes are intentionally applied broadly across
# the related obenrum_* buckets: a size that turns out not to apply to a given catalog is
# silently dropped during Vinted filter resolution rather than corrupting the search.
# Keywords must be unambiguous enough to never match another bucket's size group (e.g. a
# generic gender-scoped label like "Größen Männer" or "S/M/L" can legitimately describe a
# shoe size group, so those must not appear here even though they look clothing-related).
SIZE_GROUP_KEYWORDS_BY_CLOTHING_ITEM: dict[ClothingItem, list[str]] = {
    "schuhe": ["schuhe"],
    "hosen": ["hose"],
    "kopf": ["hut", "huete", "muetze"],
    "obenrum_warm": ["hemd", "shirt", "kleidergroesse"],
    "obenrum_mittel": ["hemd", "shirt", "kleidergroesse"],
    "obenrum_kalt": ["jacke", "pullover", "kleidergroesse"],
}


def clothing_items_for_size_group(description: str) -> list[ClothingItem]:
    normalized = normalise_lookup_text(description)
    return [
        item
        for item, keywords in SIZE_GROUP_KEYWORDS_BY_CLOTHING_ITEM.items()
        if any(keyword in normalized for keyword in keywords)
    ]


class CategoryFilterError(ValueError):
    """Raised when a saved search contains an unusable Vinted category filter."""


_ALLOWED_ITEM_KEYS = set(CLOTHING_ITEM_LABELS)


def normalise_lookup_text(value: str) -> str:
    return (
        str(value)
        .strip()
        .lower()
        .replace("&", "and")
        .replace("_", " ")
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )


def category_ids_for_alias(value: str) -> list[str]:
    normalized = normalise_lookup_text(value)
    direct = CATEGORY_ALIAS_IDS.get(normalized)
    if direct:
        return list(direct)
    # Preserve backwards compatibility with aliases that deliberately contain underscores.
    underscored = str(value).strip().lower()
    return list(CATEGORY_ALIAS_IDS.get(underscored, []))


def resolved_category_ids_for_aliases(values: list[str]) -> list[str]:
    resolved: list[str] = []
    for value in values:
        resolved.extend(category_ids_for_alias(value))
    return list(dict.fromkeys(resolved))


def default_category_filter_for_clothing_item(clothing_item: ClothingItem) -> SearchFilter:
    return SearchFilter(
        field="category",
        label="Vinted category",
        mode="include",
        values=list(DEFAULT_CATEGORY_ALIASES_BY_ITEM[clothing_item]),
    )


def category_options_by_clothing_item() -> dict[str, dict[str, object]]:
    options: dict[str, dict[str, object]] = {}
    for clothing_item in CLOTHING_ITEM_LABELS:
        aliases = list(ALLOWED_CATEGORY_ALIASES_BY_ITEM[clothing_item])
        options[clothing_item] = {
            "label": CLOTHING_ITEM_LABELS[clothing_item],
            "description": CLOTHING_ITEM_DESCRIPTIONS[clothing_item],
            "default_aliases": list(DEFAULT_CATEGORY_ALIASES_BY_ITEM[clothing_item]),
            "allowed_aliases": aliases,
            "alias_catalog_ids": {alias: category_ids_for_alias(alias) for alias in aliases},
            "resolved_catalog_ids": resolved_category_ids_for_aliases(aliases),
        }
    return options


def _is_category_field(field: str) -> bool:
    return normalise_lookup_text(field) in _CATEGORY_FIELDS


def _valid_clothing_item(value: str) -> ClothingItem:
    if value not in _ALLOWED_ITEM_KEYS:
        raise CategoryFilterError(f"Unsupported clothing item for Vinted category defaults: {value}")
    return cast(ClothingItem, value)


def _allowed_catalog_ids_for_item(clothing_item: ClothingItem) -> set[str]:
    return set(resolved_category_ids_for_aliases(ALLOWED_CATEGORY_ALIASES_BY_ITEM[clothing_item]))


def _format_allowed_aliases(clothing_item: ClothingItem) -> str:
    return ", ".join(ALLOWED_CATEGORY_ALIASES_BY_ITEM[clothing_item])


def ensure_category_filter_for_clothing_item(
    filters: list[SearchFilter],
    clothing_item: ClothingItem | str,
    *,
    strict: bool = False,
) -> list[SearchFilter]:
    """Return filters with a resolvable category filter for the given clothing bucket.

    If no category/catalog filter is present, a default one is prepended. Existing category
    filters are preserved when their aliases resolve and match the bucket's allowed catalog IDs.
    In non-strict mode, unresolved category aliases are dropped and replaced with the bucket
    default if nothing valid remains. In strict mode, bad aliases raise a user-facing error.
    """
    item = _valid_clothing_item(str(clothing_item))
    allowed_ids = _allowed_catalog_ids_for_item(item)
    category_filter_seen = False
    result: list[SearchFilter] = []

    for filter_item in filters:
        if not _is_category_field(filter_item.field):
            result.append(filter_item)
            continue

        category_filter_seen = True
        valid_values: list[str] = []
        unresolved_values: list[str] = []
        mismatched_values: list[str] = []
        for raw_value in filter_item.values:
            value = raw_value.strip()
            if not value:
                continue
            resolved_ids = set(category_ids_for_alias(value))
            if not resolved_ids:
                unresolved_values.append(value)
                continue
            if resolved_ids.isdisjoint(allowed_ids):
                mismatched_values.append(value)
                continue
            valid_values.append(value)

        if strict and (unresolved_values or mismatched_values):
            problems: list[str] = []
            if unresolved_values:
                problems.append(f"unknown category aliases: {', '.join(unresolved_values)}")
            if mismatched_values:
                problems.append(
                    f"aliases outside {CLOTHING_ITEM_LABELS[item]}: {', '.join(mismatched_values)}"
                )
            raise CategoryFilterError(
                "Invalid Vinted category filter for "
                f"{CLOTHING_ITEM_LABELS[item]} ({'; '.join(problems)}). "
                f"Allowed aliases: {_format_allowed_aliases(item)}."
            )

        if valid_values:
            result.append(filter_item.model_copy(update={"field": "category", "values": list(dict.fromkeys(valid_values))}))

    if not category_filter_seen or not any(_is_category_field(item.field) for item in result):
        return [default_category_filter_for_clothing_item(item), *result]

    return result
