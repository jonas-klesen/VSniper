from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vsniper.core.database import Base
from vsniper.db.models import (
    AiModelConfig,
    AppSettingsState,
    Candidate,
    LearningSnapshotState,
    Search,
    TasteSampleState,
    TasteState,
)
from vsniper.domain.contracts import TasteOfferCreate, TasteProfile
from vsniper.services.taste_service import TasteService


def _setup(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'taste-service.db'}", future=True)
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

    monkeypatch.setattr("vsniper.services.taste_service.session_scope", fake_session_scope)
    settings = SimpleNamespace(
        upload_dir=tmp_path / "uploads",
        cache_dir=tmp_path / "cache",
        feedback_asset_dir=tmp_path / "feedback-assets",
        resolve_path=lambda value: value,
    )
    service = TasteService.__new__(TasteService)
    service.settings = settings
    service.taste_client = SimpleNamespace()
    service._test_factory = factory
    return service


def test_add_offer_hydrates_vinted_listing(monkeypatch, tmp_path) -> None:
    service = _setup(monkeypatch, tmp_path)

    class FakeVintedClient:
        def fetch_item_by_url(self, url, *, clothing_item, region):
            assert url == "https://www.vinted.de/items/12345-wide-cargos"
            assert clothing_item == "hosen"
            assert region == "de"
            return {
                "id": "12345",
                "external_item_id": "12345",
                "title": "Wide cargos",
                "brand": "Brand",
                "price_eur": 35.0,
                "size": "M",
                "url": url,
                "image_urls": ["https://images.vinted.example/12345.jpg"],
                "description": "Wide cotton trousers",
                "features": [],
                "raw_listing": {"id": 12345},
            }

    service.vinted_client = FakeVintedClient()
    monkeypatch.setattr(service, "_cache_image_urls", lambda sample_id, image_urls: ["taste-offers/12345-0.jpg"])

    sample = service.add_offer(
        TasteOfferCreate(
            vinted_url="https://www.vinted.de/items/12345-wide-cargos",
            kind="offer_like",
            clothing_item="hosen",
            note="good shape",
        )
    )

    assert sample.external_item_id == "12345"
    assert sample.title == "Wide cargos"
    assert sample.brand == "Brand"
    assert sample.price_eur == 35.0
    assert sample.size == "M"
    assert sample.description == "Wide cotton trousers"
    assert sample.image_urls == ["https://images.vinted.example/12345.jpg"]
    assert sample.cached_image_paths == ["taste-offers/12345-0.jpg"]
    assert sample.normalized_listing["raw_listing"] == {"id": 12345}


def test_add_offer_reuses_candidate_and_copies_cached_image(monkeypatch, tmp_path) -> None:
    service = _setup(monkeypatch, tmp_path)
    factory = service._test_factory
    now = datetime.now(UTC)
    with factory() as session:
        session.add(Search(id="s1", name="Search", clothing_item="hosen", query="q", region="de", enabled=True))
        session.add(
            Candidate(
                id="s1:12345",
                external_item_id="12345",
                clothing_item="hosen",
                search_id="s1",
                title="Cached cargos",
                brand="Cached brand",
                price_eur=28.0,
                size="M",
                url="https://www.vinted.de/items/12345-cached-cargos",
                image_urls=["https://images1.vinted.net/item.jpg"],
                matched_filters=[],
                matched_preferences=[],
                features=[],
                normalized_listing={"description": "Cached description", "raw_listing": {"id": 12345}},
                score_trace={},
                decision="review",
                final_score=70,
                created_at=now,
                last_seen_at=now,
            )
        )
        session.commit()

    candidate_cache = tmp_path / "cache" / "candidate-images" / "s1_12345.jpg"
    candidate_cache.parent.mkdir(parents=True)
    candidate_cache.write_bytes(b"cached image")

    class FakeVintedClient:
        def fetch_item_by_url(self, *args, **kwargs):
            raise AssertionError("known candidates must not be fetched from Vinted")

    service.vinted_client = FakeVintedClient()
    sample = service.add_offer(
        TasteOfferCreate(
            vinted_url="https://www.vinted.de/items/12345-any-slug",
            clothing_item="hosen",
        )
    )

    assert sample.title == "Cached cargos"
    assert sample.candidate_id == "s1:12345"
    assert len(sample.cached_image_paths) == 1
    copied = tmp_path / "cache" / sample.cached_image_paths[0]
    assert copied.read_bytes() == b"cached image"
    assert candidate_cache.read_bytes() == b"cached image"


def test_candidate_feedback_sample_uses_durable_images(monkeypatch, tmp_path) -> None:
    service = _setup(monkeypatch, tmp_path)
    factory = service._test_factory
    now = datetime.now(UTC)

    with factory() as session:
        session.add(Search(id="s1", name="Search", clothing_item="hosen", query="q", region="de", enabled=True))
        candidate = Candidate(
            id="candidate-1",
            search_id="s1",
            clothing_item="hosen",
            title="Wide cargos",
            brand="Brand",
            price_eur=20.0,
            size="M",
            url="https://www.vinted.de/items/1",
            image_urls=["https://images1.vinted.net/item.jpg"],
            matched_filters=[],
            matched_preferences=[],
            features=[],
            normalized_listing={"description": "Wide cotton trousers"},
            extraction_status="completed",
            score_trace={"final_score": 0.8},
            decision="alert",
            final_score=8.0,
            ai_observation={},
            grading_stage="vlm_judged",
            feedback="unknown",
            feedback_comment="good fabric",
            created_at=now,
        )
        session.add(candidate)
        sample = service.upsert_candidate_feedback_sample(
            session,
            candidate=candidate,
            verdict=None,
            comment="good fabric",
            stored_image_paths=["candidate-1-0.jpg"],
        )
        session.commit()

    assert sample.kind == "offer_note"
    assert sample.stored_image_paths == ["candidate-1-0.jpg"]
    assert sample.cached_image_paths == []
    assert sample.description == "Wide cotton trousers"
    assert sample.normalized_listing["score_trace"] == {"final_score": 0.8}

    with factory() as session:
        stored = session.query(TasteSampleState).filter_by(candidate_id="candidate-1").one()
        assert stored.kind == "offer_note"
        assert stored.stored_image_paths == ["candidate-1-0.jpg"]


def test_cancel_recompute_unblocks_immediate_retry(monkeypatch, tmp_path) -> None:
    service = _setup(monkeypatch, tmp_path)
    factory = service._test_factory
    now = datetime.now(UTC)

    with factory() as session:
        session.add(
            TasteState(
                id=1,
                manual_note="",
                taste_profile={},
                recompute_status="running",
                recompute_job_id="job-stuck",
                recompute_started_at=now,
            )
        )
        session.commit()

    snapshot = service.cancel_recompute()
    assert snapshot.recompute_state.status == "cancelled"
    assert snapshot.recompute_state.job_id is None

    with factory() as session:
        state = session.get(TasteState, 1)
        assert state.recompute_status == "cancelled"
        assert state.recompute_job_id is None
        assert state.recompute_started_at is None
        assert state.recompute_error == "Cancelled by user."

    # A cancelled claim must not force the caller to wait out the 2h staleness window.
    claim = service.claim_recompute(source="api")
    assert claim.claimed is True


def test_cancel_recompute_is_a_noop_when_nothing_is_running(monkeypatch, tmp_path) -> None:
    service = _setup(monkeypatch, tmp_path)
    factory = service._test_factory

    with factory() as session:
        session.add(TasteState(id=1, manual_note="", taste_profile={}, recompute_status="idle"))
        session.commit()

    snapshot = service.cancel_recompute()
    assert snapshot.recompute_state.status == "idle"


def _seed_recompute_prereqs(factory, *, job_id: str, taste_profile: dict, now: datetime) -> None:
    with factory() as session:
        session.add(
            AiModelConfig(id="m1", provider="openai", model_name="gpt-5", reasoning_effort="medium", display_name="Test")
        )
        session.add(AppSettingsState(id=1, vinted_region="de", learn_model_id="m1", observation_model_id="m1"))
        session.add(
            TasteState(
                id=1,
                manual_note="",
                taste_profile=taste_profile,
                recompute_status="running",
                recompute_job_id=job_id,
                recompute_started_at=now,
            )
        )
        session.commit()


def test_superseded_recompute_job_does_not_clobber_newer_result(monkeypatch, tmp_path) -> None:
    service = _setup(monkeypatch, tmp_path)
    service.settings.ai_learn_image_detail = "high"
    factory = service._test_factory
    now = datetime.now(UTC)
    original_profile = TasteProfile(summary="original", taste_prompt="original").model_dump(mode="json")
    _seed_recompute_prereqs(factory, job_id="job-A", taste_profile=original_profile, now=now)
    service.taste_client.build_taste_profile = lambda **kwargs: TasteProfile(summary="s", taste_prompt="p")

    # Simulate the orphaned call's claim being superseded (e.g. by a cancel) while it was
    # still in flight, then the orphaned call finally returning.
    service.cancel_recompute()
    service._run_recompute_unlocked("job-A")

    with factory() as session:
        state = session.get(TasteState, 1)
        assert state.taste_profile["summary"] == "original"
        assert session.query(LearningSnapshotState).count() == 0


def test_matching_recompute_job_persists_result(monkeypatch, tmp_path) -> None:
    service = _setup(monkeypatch, tmp_path)
    service.settings.ai_learn_image_detail = "high"
    factory = service._test_factory
    now = datetime.now(UTC)
    _seed_recompute_prereqs(factory, job_id="job-A", taste_profile={}, now=now)
    service.taste_client.build_taste_profile = lambda **kwargs: TasteProfile(summary="s", taste_prompt="p")

    result = service._run_recompute_unlocked("job-A")

    assert result.snapshot.taste_profile.summary == "s"
    with factory() as session:
        state = session.get(TasteState, 1)
        assert state.taste_profile["summary"] == "s"
        snapshot = session.query(LearningSnapshotState).one()
        assert snapshot.old_taste_profile is None
        assert snapshot.new_taste_profile["summary"] == "s"
