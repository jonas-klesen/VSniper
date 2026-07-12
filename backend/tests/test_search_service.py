"""Tests for SearchService.apply_profile_sizes_to_all.

The key behavior under test: a user's flat Vinted account size list mixes shoe sizes,
trouser sizes, and shirt sizes together. apply_profile_sizes_to_all must split that list
by clothing item (via clothing_items_for_size_group) and write only the relevant sizes onto
each search, instead of blasting the same full list onto every search regardless of category.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from vsniper.core.database import Base
from vsniper.db.models import AppSettingsState, Search, TasteState
from vsniper.domain.contracts import GeneratedSearchDraft, SearchFilter, TasteProfile
from vsniper.services.search_service import SearchService


def _setup(monkeypatch, tmp_path, *, grouped_sizes):
    engine = create_engine(f"sqlite:///{tmp_path / 'sizes.db'}", future=True)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def fake_session_scope():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr("vsniper.services.search_service.session_scope", fake_session_scope)

    with fake_session_scope() as session:
        session.add(AppSettingsState(id=1, vinted_region="de"))
        session.add(Search(id="search-schuhe", name="Schuhe", clothing_item="schuhe", query="q", region="de", run_status="idle"))
        session.add(Search(id="search-hosen", name="Hosen", clothing_item="hosen", query="q", region="de", run_status="idle"))
        session.add(Search(id="search-kopf", name="Kopf", clothing_item="kopf", query="q", region="de", run_status="idle"))
        session.add(Search(id="search-obenrum-warm", name="Obenrum Warm", clothing_item="obenrum_warm", query="q", region="de", run_status="idle"))
        session.add(Search(id="search-obenrum-mittel", name="Obenrum Mittel", clothing_item="obenrum_mittel", query="q", region="de", run_status="idle"))

    service = SearchService.__new__(SearchService)
    service.vinted_client = type(
        "Vinted",
        (),
        {"fetch_user_sizes_grouped": staticmethod(lambda *, region: grouped_sizes)},
    )()
    return service, factory


def _size_filter_values(factory, search_id: str) -> list[str] | None:
    session = factory()
    try:
        search = session.get(Search, search_id)
        filters = [SearchFilter.model_validate(f) for f in search.filters]
        size_filters = [f for f in filters if f.field == "size"]
        return size_filters[0].values if size_filters else None
    finally:
        session.close()


def test_apply_profile_sizes_to_all_buckets_sizes_by_clothing_item(monkeypatch, tmp_path) -> None:
    grouped = [
        {"title": "43", "group_description": "Schuhe Männer"},
        {"title": "42.5", "group_description": "Schuhe Männer"},
        {"title": "W32 | DE 48", "group_description": "Hosen für Herren"},
        {"title": "Gürtelgröße 90", "group_description": "Gürtel für Herren"},
    ]
    service, factory = _setup(monkeypatch, tmp_path, grouped_sizes=grouped)

    service.apply_profile_sizes_to_all()

    assert _size_filter_values(factory, "search-schuhe") == ["43", "42.5"]
    assert _size_filter_values(factory, "search-hosen") == ["W32 | DE 48"]
    # "Kopf" has no matching size group in the fetched list, so its filters are untouched.
    assert _size_filter_values(factory, "search-kopf") is None


def test_apply_profile_sizes_to_all_leaves_unmapped_clothing_item_untouched(monkeypatch, tmp_path) -> None:
    service, factory = _setup(
        monkeypatch,
        tmp_path,
        grouped_sizes=[{"title": "43", "group_description": "Schuhe Männer"}],
    )

    with factory() as session:
        search = session.get(Search, "search-hosen")
        search.filters = [SearchFilter(field="size", label="Sizes", values=["existing"], mode="include").model_dump(mode="json")]
        session.commit()

    service.apply_profile_sizes_to_all()

    assert _size_filter_values(factory, "search-hosen") == ["existing"]
    assert _size_filter_values(factory, "search-schuhe") == ["43"]


def test_apply_profile_sizes_to_all_does_not_leak_shoe_sizes_into_tops_via_generic_size_group(monkeypatch, tmp_path) -> None:
    grouped = [
        {"title": "43", "group_description": "Schuhe Männer"},
        # An ambiguous, gender-scoped size-group label that Vinted can legitimately use for a
        # shoe size group. Its titles must not leak into obenrum_warm/obenrum_mittel.
        {"title": "41 | DE 54", "group_description": "Größen Männer"},
        {"title": "42 | DE 56", "group_description": "Größen Männer"},
    ]
    service, factory = _setup(monkeypatch, tmp_path, grouped_sizes=grouped)

    service.apply_profile_sizes_to_all()

    assert _size_filter_values(factory, "search-schuhe") == ["43"]
    assert _size_filter_values(factory, "search-obenrum-warm") is None
    assert _size_filter_values(factory, "search-obenrum-mittel") is None


def test_apply_generated_drafts_preserves_the_user_price_ceiling(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path, grouped_sizes=[])
    existing_price = SearchFilter(field="price", label="Maximum price", values=["20"], mode="range")
    draft_price = SearchFilter(field="price", label="Maximum price", values=["80"], mode="range")
    draft_category = SearchFilter(field="category", label="Vinted category", values=["sneakers"], mode="include")
    profile = TasteProfile(
        version=4,
        summary="",
        taste_prompt="",
        generated_searches=[
            GeneratedSearchDraft(
                id="draft-shoes",
                clothing_item="schuhe",
                name="Generated shoes",
                query="retro sneaker",
                region="de",
                filters=[draft_price, draft_category],
                created_at=datetime.now(UTC),
            )
        ],
    )
    with factory() as session:
        search = session.get(Search, "search-schuhe")
        search.filters = [existing_price.model_dump(mode="json")]
        session.add(TasteState(id=1, taste_profile=profile.model_dump(mode="json")))
        session.commit()

    service.preferences = SimpleNamespace(get_taste_state=lambda session: session.get(TasteState, 1))

    result = service.apply_generated_search_drafts(profile_version=4)

    assert result.applied == ["schuhe"]
    with factory() as session:
        search = session.get(Search, "search-schuhe")
        filters = [SearchFilter.model_validate(item) for item in search.filters]
    assert search.query == "retro sneaker"
    assert [filter_.values for filter_ in filters if filter_.field == "price"] == [["20"]]
