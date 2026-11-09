"""Tests for SearchService._judge_candidates — the VLM judging pipeline.

Covers the riskiest outcomes: all candidates being judged one request each, reuse of candidates
already judged under the current profile version, image-download failure, and a failing listing
that must not take its neighbours down with it.
"""
from __future__ import annotations

import threading
from datetime import timedelta
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, UnidentifiedImageError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from vsniper.core.database import Base
from vsniper.db.models import AppSettingsState, Candidate, Search
from vsniper.domain.contracts import (
    CandidateJudgment,
    ScoreTrace,
    SearchRecord,
    TasteProfile,
)
from vsniper.domain.scoring.service import build_judgment_trace
from vsniper.integrations.openai.client import (
    CandidateImageInput,
    OpenAIIntegrationError,
)
from vsniper.integrations.vinted.client import VintedSearchError
from vsniper.services._mapping import ResolvedAiModel
from vsniper.services.search_service import SearchService


@pytest.fixture(autouse=True)
def memo_db(monkeypatch):
    """In-memory DB so _memoized_judgments can query the candidates table.

    The table is empty by default, so every candidate is judged; the memoization tests seed a
    previously-judged row through the returned factory.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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
    return factory


def _seed_judged_candidate(factory, candidate_id: str, score_trace: ScoreTrace, *, grading_stage: str = "vlm_judged") -> None:
    session = factory()
    session.add(
        Candidate(
            id=candidate_id,
            search_id="search-001",
            clothing_item="hosen",
            title="Previously seen",
            brand="Brand",
            price_eur=20.0,
            size="M",
            url=f"https://vinted.de/{candidate_id}",
            image_urls=[],
            matched_filters=[],
            matched_preferences=[],
            features=[],
            normalized_listing={},
            decision=score_trace.decision,
            final_score=score_trace.final_score,
            grading_stage=grading_stage,
            score_trace=score_trace.model_dump(mode="json"),
        )
    )
    session.commit()
    session.close()


def _make_service() -> SearchService:
    service = SearchService.__new__(SearchService)
    service.settings = MagicMock()
    service.vinted_client = MagicMock()
    service.taste_client = MagicMock()
    service.preferences = MagicMock()
    service.preferences.latest_labeled_anchors.return_value = ([], [])
    service.telegram = MagicMock()
    return service


def _db_settings(
    *,
    vlm_judge_parallel_requests: int = 1,
    ai_judge_provider: str = "local",
    ai_judge_model: str = "gpt-5.4-mini",
    local_judge_model: str = "gemma4-12b-quality",
    ai_judge_allow_openai_fallback: bool = False,
    ai_judge_fallback_provider: str | None = None,
    cerebras_judge_model: str = "gemma-4-31b",
    ai_judge_max_photos: int = 1,
) -> SimpleNamespace:
    """Builds a plain settings namespace plus the registry-resolved judge/fallback models that
    `_judge_candidates`/`_judge_listing` now take as explicit params (post AI-models-registry
    refactor). `ai_judge_*` kwargs here mirror the pre-registry field names purely so existing
    test call sites read the same; they are translated into `ResolvedAiModel`s below."""
    fallback_provider = ai_judge_fallback_provider
    if fallback_provider is None:
        fallback_provider = "openai" if ai_judge_allow_openai_fallback else "none"

    provider = ai_judge_provider.strip().lower()
    model_name = local_judge_model if provider == "local" else ai_judge_model
    judge_model = ResolvedAiModel(
        id="model-judge",
        provider=provider,
        model_name=model_name,
        reasoning_effort="low",
        local_base_url="http://127.0.0.1:8080/v1" if provider == "local" else None,
    )
    if fallback_provider == "openai":
        judge_fallback_model: ResolvedAiModel | None = ResolvedAiModel(
            id="model-fallback",
            provider="openai",
            model_name=ai_judge_model,
            reasoning_effort="low",
            local_base_url=None,
        )
    elif fallback_provider == "cerebras":
        judge_fallback_model = ResolvedAiModel(
            id="model-fallback",
            provider="cerebras",
            model_name=cerebras_judge_model,
            reasoning_effort="low",
            local_base_url=None,
        )
    else:
        judge_fallback_model = None

    return SimpleNamespace(
        vlm_judge_parallel_requests=vlm_judge_parallel_requests,
        ai_judge_image_max_px=512,
        ai_judge_max_photos=ai_judge_max_photos,
        judge_model=judge_model,
        judge_fallback_model=judge_fallback_model,
    )


def _call_judge(service: SearchService, raw_candidates, search, taste, db_settings, **kwargs):
    """Thin wrapper so existing tests can keep passing one `_db_settings(...)` namespace —
    `_judge_candidates` itself now takes the registry-resolved judge/fallback models as
    explicit params, which this pulls off the namespace `_db_settings()` attaches them to.
    Returns just (traces, stages) — callers that don't care about persisted counts don't have
    to unpack them."""
    kwargs.setdefault("mode", "preview")
    traces, stages, _alert_count, _queued = service._judge_candidates(
        raw_candidates,
        search,
        taste,
        db_settings,
        judge_model=db_settings.judge_model,
        judge_fallback_model=db_settings.judge_fallback_model,
        **kwargs,
    )
    return traces, stages


def _search(search_id: str = "search-001") -> SearchRecord:
    return SearchRecord(id=search_id, name="Test", clothing_item="hosen", queries=["vintage"], region="de")


def _jpeg(color: str = "red") -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="JPEG")
    return buf.getvalue()


def _make_response(content: bytes, *, content_type: str = "image/jpeg") -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {"content-type": content_type}
    resp.content = content
    return resp


def _raw(external_id: str, title: str = "A listing", brand: str = "Brand") -> dict:
    return {
        "id": external_id,
        "external_item_id": external_id,
        "title": title,
        "brand": brand,
        "price_eur": 20.0,
        "size": "M",
        "url": f"https://vinted.de/{external_id}",
        "image_urls": [f"https://images.vinted.de/{external_id}.jpg"],
        "description": "",
        "features": [],
        "raw_listing": {},
    }


def _judge_downloads(service: SearchService, raw: dict, db_settings) -> list[list[str]]:
    """Judge one listing and return the photo URL lists handed to the image download."""
    seen: list[list[str]] = []
    jpeg = _jpeg()

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        seen.append(list(image_urls))
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.return_value = CandidateJudgment(
        score=80, explanation="ok", labels=[], concerns=[]
    )
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        _call_judge(service, [raw], _search(), TasteProfile(summary="", taste_prompt="likes vintage"), db_settings)
    return seen


def test_listing_page_photos_are_judged_up_to_the_photo_limit() -> None:
    service = _make_service()
    page_photos = [f"https://images.vinted.de/page-{index}.jpg" for index in range(5)]
    service.vinted_client.fetch_listing_photo_urls.return_value = page_photos

    seen = _judge_downloads(service, _raw("item-1"), _db_settings(ai_judge_max_photos=3))

    service.vinted_client.fetch_listing_photo_urls.assert_called_once_with("https://vinted.de/item-1")
    assert seen == [page_photos[:3]]


def test_search_photo_is_judged_when_the_listing_page_fails() -> None:
    service = _make_service()
    service.vinted_client.fetch_listing_photo_urls.side_effect = VintedSearchError("Vinted unavailable", retryable=True)

    seen = _judge_downloads(service, _raw("item-1"), _db_settings(ai_judge_max_photos=4))

    assert seen == [["https://images.vinted.de/item-1.jpg"]]
    assert service.taste_client.judge_candidate.call_count == 1


def test_single_photo_limit_skips_the_listing_page() -> None:
    service = _make_service()

    seen = _judge_downloads(service, _raw("item-1"), _db_settings(ai_judge_max_photos=1))

    service.vinted_client.fetch_listing_photo_urls.assert_not_called()
    assert seen == [["https://images.vinted.de/item-1.jpg"]]


def test_all_candidates_are_judged() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw(f"item-{index}") for index in range(5)]

    jpeg = _jpeg()

    def _judge(*, candidate: CandidateImageInput, **kwargs) -> CandidateJudgment:
        return CandidateJudgment(score=80, explanation="ok", labels=[], concerns=[])

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            candidates,
            search,
            taste,
            _db_settings(),
        )

    judged = [c for c, s in stages.items() if s == "vlm_judged"]
    assert len(judged) == 5
    # One request per listing.
    assert service.taste_client.judge_candidate.call_count == 5
    assert len([trace for trace in traces.values() if trace.score_10 == 80]) == 5


def test_already_judged_candidate_is_reused_not_rejudged(memo_db) -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")  # version defaults to 1

    # item-1 was already VLM-judged under profile version 1; item-2 has never been seen.
    prior = build_judgment_trace(
        judgment=CandidateJudgment(score=80, explanation="seen before", labels=[], concerns=[]),
        taste_profile=taste,
        model=_db_settings().judge_model.model_name,
    )
    _seed_judged_candidate(memo_db, "search-001:item-1", prior)

    jpeg = _jpeg()

    def _judge(*, candidate: CandidateImageInput, **kwargs) -> CandidateJudgment:
        return CandidateJudgment(score=30, explanation="fresh", labels=[], concerns=[])

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.side_effect = _judge
    new_judged_ids: set[str] = set()
    with patch.object(service, "_download_candidate_image", side_effect=_download) as download:
        traces, stages = _call_judge(service, 
            [_raw("item-1"), _raw("item-2")],
            search,
            taste,
            _db_settings(),
            new_judged_ids=new_judged_ids,
        )

    # item-1 reused from its stored trace; only the unseen item-2 reached the VLM.
    assert stages["search-001:item-1"] == "vlm_judged"
    assert traces["search-001:item-1"].score_10 == 80
    assert traces["search-001:item-1"].explanation == "seen before"
    assert stages["search-001:item-2"] == "vlm_judged"
    assert traces["search-001:item-2"].score_10 == 30
    assert new_judged_ids == {"search-001:item-2"}
    download.assert_called_once()
    assert download.call_args.kwargs["candidate_id"] == "search-001:item-2"


def test_threshold_change_reuses_memoized_score_with_new_decision(memo_db) -> None:
    """Production 2026-11-07: a threshold change re-judged every listing on every search (hours of
    VLM time on a 2-slot server). The stored score is still valid for the same profile version, so
    only the alert decision is recomputed."""
    service = _make_service()
    search = _search().model_copy(update={"alert_threshold": 80, "effective_alert_threshold": 80})
    taste = TasteProfile(summary="", taste_prompt="likes vintage")

    prior = build_judgment_trace(
        judgment=CandidateJudgment(score=80, explanation="old threshold", labels=[], concerns=[]),
        taste_profile=taste,
        model=_db_settings().judge_model.model_name,
        alert_threshold=95,
    )
    _seed_judged_candidate(memo_db, "search-001:item-1", prior)

    with patch.object(service, "_download_candidate_image") as download:
        traces, stages = _call_judge(service, [_raw("item-1")], search, taste, _db_settings())

    trace = traces["search-001:item-1"]
    assert stages["search-001:item-1"] == "vlm_judged"
    assert trace.score_10 == 80
    assert trace.explanation == "old threshold"
    assert trace.threshold == 0.8
    assert trace.decision == "alert"
    download.assert_not_called()
    service.taste_client.judge_candidate.assert_not_called()


def test_stale_version_and_failed_candidates_are_rejudged(memo_db) -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage", version=2)

    # item-1 was judged under an older profile version; item-2's last attempt failed.
    stale = build_judgment_trace(
        judgment=CandidateJudgment(score=80, explanation="old profile", labels=[], concerns=[]),
        taste_profile=TasteProfile(summary="", taste_prompt="x", version=1),
        model="local",
    )
    _seed_judged_candidate(memo_db, "search-001:item-1", stale)
    _seed_judged_candidate(memo_db, "search-001:item-2", stale, grading_stage="failed")

    jpeg = _jpeg()

    def _judge(*, candidate: CandidateImageInput, **kwargs) -> CandidateJudgment:
        return CandidateJudgment(score=50, explanation="rejudged", labels=[], concerns=[])

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            [_raw("item-1"), _raw("item-2")],
            search,
            taste,
            _db_settings(),
        )

    # Both were re-judged: stale-version and failed rows are never reused.
    assert traces["search-001:item-1"].score_10 == 50
    assert traces["search-001:item-2"].score_10 == 50


def test_vlm_judging_includes_size_metadata() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    jpeg = _jpeg()

    def _judge(*, candidate: CandidateImageInput, **kwargs) -> CandidateJudgment:
        assert candidate.size == "M"
        return CandidateJudgment(
                    score=80,
                    explanation="ok",
                    labels=[],
                    concerns=[],
                )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            [_raw("item-1")],
            search,
            taste,
            _db_settings(),
        )

    assert stages["search-001:item-1"] == "vlm_judged"
    assert traces["search-001:item-1"].score_10 == 80


def test_image_download_failure_marks_candidate_as_failed() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")

    with patch.object(service, "_download_candidate_image", return_value=None):
        traces, stages = _call_judge(service, 
            [_raw("item-1")],
            search,
            taste,
            _db_settings(),
        )

    cid = "search-001:item-1"
    assert stages[cid] == "failed"
    assert traces[cid].decision == "discard"
    assert "No usable candidate image" in traces[cid].explanation
    service.taste_client.judge_candidate.assert_not_called()


def test_failing_listing_is_marked_failed_without_affecting_others() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    jpeg = _jpeg()

    def _judge(*, candidate: CandidateImageInput, **kwargs) -> CandidateJudgment:
        if candidate.candidate_id == "search-001:item-2":
            raise OpenAIIntegrationError("provider down")
        return CandidateJudgment(score=80, explanation="ok", labels=[], concerns=[])

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, [_raw("item-1"), _raw("item-2")], search, taste, _db_settings())

    assert stages["search-001:item-1"] == "vlm_judged"
    assert traces["search-001:item-1"].score_10 == 80
    assert stages["search-001:item-2"] == "failed"
    assert traces["search-001:item-2"].decision == "discard"
    assert "provider down" in traces["search-001:item-2"].explanation


def test_local_judge_falls_back_to_openai_when_enabled() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw("item-1")]
    jpeg = _jpeg()

    def _judge(*, candidate, ai_judge_provider, model, **kwargs):
        if ai_judge_provider == "local":
            assert model == "local-model"
            raise OpenAIIntegrationError("local down")
        assert ai_judge_provider == "openai"
        assert model == "gpt-fallback"
        return CandidateJudgment(
                    score=90,
                    explanation="Fallback judged it well.",
                    labels=["vintage"],
                    concerns=[],
                )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            candidates,
            search,
            taste,
            _db_settings(
                ai_judge_provider="local",
                local_judge_model="local-model",
                ai_judge_model="gpt-fallback",
                ai_judge_allow_openai_fallback=True,
            ),
        )

    assert stages["search-001:item-1"] == "vlm_judged"
    assert traces["search-001:item-1"].score_10 == 90
    assert traces["search-001:item-1"].model == "gpt-fallback"


def test_local_judge_falls_back_to_cerebras_when_selected() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw("item-1")]
    jpeg = _jpeg()

    def _judge(*, candidate, ai_judge_provider, model, **kwargs):
        if ai_judge_provider == "local":
            assert model == "local-model"
            raise OpenAIIntegrationError("local down")
        assert ai_judge_provider == "cerebras"
        assert model == "gemma-4-31b"
        return CandidateJudgment(
                    score=80,
                    explanation="Cerebras fallback judged it well.",
                    labels=["vintage"],
                    concerns=[],
                )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            candidates,
            search,
            taste,
            _db_settings(
                ai_judge_provider="local",
                local_judge_model="local-model",
                ai_judge_fallback_provider="cerebras",
                cerebras_judge_model="gemma-4-31b",
            ),
        )

    assert stages["search-001:item-1"] == "vlm_judged"
    assert traces["search-001:item-1"].score_10 == 80
    assert traces["search-001:item-1"].model == "gemma-4-31b"


def test_openai_primary_falls_back_to_cerebras_on_any_failure() -> None:
    """Fallback is universal now (not local-only): an OpenAI primary failure must also retry
    against a configured fallback model."""
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw("item-1")]
    jpeg = _jpeg()

    def _judge(*, candidate, ai_judge_provider, model, **kwargs):
        if ai_judge_provider == "openai":
            assert model == "gpt-primary"
            raise OpenAIIntegrationError("openai down")
        assert ai_judge_provider == "cerebras"
        assert model == "gemma-4-31b"
        return CandidateJudgment(
                    score=70,
                    explanation="Cerebras fallback judged it well.",
                    labels=["vintage"],
                    concerns=[],
                )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service,
            candidates,
            search,
            taste,
            _db_settings(
                ai_judge_provider="openai",
                ai_judge_model="gpt-primary",
                ai_judge_fallback_provider="cerebras",
                cerebras_judge_model="gemma-4-31b",
            ),
        )

    assert stages["search-001:item-1"] == "vlm_judged"
    assert traces["search-001:item-1"].score_10 == 70
    assert traces["search-001:item-1"].model == "gemma-4-31b"


def test_openai_judge_failure_does_not_fallback() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw("item-1")]
    jpeg = _jpeg()

    service.taste_client.judge_candidate.side_effect = OpenAIIntegrationError("openai down")

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    # No fallback model configured at all (ai_judge_fallback_provider left at its "none" default),
    # so a primary OpenAI failure must not retry against anything.
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service,
            candidates,
            search,
            taste,
            _db_settings(
                ai_judge_provider="openai",
                ai_judge_model="gpt-primary",
            ),
        )

    assert stages["search-001:item-1"] == "failed"
    assert traces["search-001:item-1"].model == "gpt-primary"
    assert service.taste_client.judge_candidate.call_count == 1


def test_judge_requests_use_configured_parallelism() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw(f"item-{index}") for index in range(20)]
    jpeg = _jpeg()
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    max_active = 0

    def _judge(*, taste_profile, candidate, liked_anchors, disliked_anchors, manual_note, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 4:
                release.set()
        try:
            assert release.wait(timeout=2)
            return CandidateJudgment(
                        score=80,
                        explanation="ok",
                        labels=[],
                        concerns=[],
                    )
        finally:
            with lock:
                active -= 1

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            candidates,
            search,
            taste,
            _db_settings(ai_judge_provider="local", vlm_judge_parallel_requests=4),
        )

    assert max_active == 4
    assert len([stage for stage in stages.values() if stage == "vlm_judged"]) == 20
    assert len([trace for trace in traces.values() if trace.score_10 == 80]) == 20


def test_judge_parallelism_can_be_raised_for_openai() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw(f"item-{index}") for index in range(36)]
    jpeg = _jpeg()
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    max_active = 0

    def _judge(*, taste_profile, candidate, liked_anchors, disliked_anchors, manual_note, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 8:
                release.set()
        try:
            assert release.wait(timeout=2)
            return CandidateJudgment(
                        score=80,
                        explanation="ok",
                        labels=[],
                        concerns=[],
                    )
        finally:
            with lock:
                active -= 1

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            candidates,
            search,
            taste,
            _db_settings(ai_judge_provider="openai", vlm_judge_parallel_requests=8),
        )

    assert max_active == 8
    assert len([stage for stage in stages.values() if stage == "vlm_judged"]) == 36
    assert len([trace for trace in traces.values() if trace.score_10 == 80]) == 36


def _download_service(tmp_path: Path) -> SearchService:
    service = _make_service()
    service.settings.resolve_path.side_effect = lambda p: tmp_path
    service.settings.cache_dir = "cache"
    return service


def test_download_rejects_html_error_page_with_image_content_type(tmp_path: Path) -> None:
    # Vinted occasionally serves an HTML error body with an image/* content-type. Caching it
    # would poison every future scan, so it must be rejected and never written to disk.
    service = _download_service(tmp_path)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.headers = {"content-type": "image/jpeg"}
    response.content = b"<html><body>404 not found</body></html>"

    client_cm = MagicMock()
    client_cm.__enter__.return_value.get.return_value = response
    with patch("vsniper.services.search_service.httpx.Client", return_value=client_cm):
        result = service._download_candidate_image(candidate_id="s:item-1", image_urls=["http://x/1.jpg"])

    assert result is None
    assert not service._candidate_cache_path("s:item-1").exists()


def test_download_rejects_truncated_image_bytes(tmp_path: Path) -> None:
    service = _download_service(tmp_path)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.headers = {"content-type": "image/jpeg"}
    response.content = _jpeg()[:32]  # truncated — Pillow cannot decode it

    client_cm = MagicMock()
    client_cm.__enter__.return_value.get.return_value = response
    with patch("vsniper.services.search_service.httpx.Client", return_value=client_cm):
        result = service._download_candidate_image(candidate_id="s:item-2", image_urls=["http://x/2.jpg"])

    assert result is None
    assert not service._candidate_cache_path("s:item-2").exists()


def test_download_caches_valid_image(tmp_path: Path) -> None:
    service = _download_service(tmp_path)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.headers = {"content-type": "image/jpeg"}
    response.content = _jpeg()

    client_cm = MagicMock()
    client_cm.__enter__.return_value.get.return_value = response
    with patch("vsniper.services.search_service.httpx.Client", return_value=client_cm):
        result = service._download_candidate_image(candidate_id="s:item-3", image_urls=["http://x/3.jpg"])

    assert result is not None
    assert service._candidate_cache_path("s:item-3").exists()


def test_download_packs_usable_secondary_listing_images(tmp_path: Path) -> None:
    service = _download_service(tmp_path)
    responses = {
        "http://x/1.jpg": _make_response(_jpeg("red")),
        "http://x/2.jpg": _make_response(b"<html>not an image</html>", content_type="image/jpeg"),
        "http://x/3.jpg": _make_response(_jpeg("blue")),
    }

    def _get_side_effect(url: str, **_: object) -> MagicMock:
        return responses[url]

    client_cm = MagicMock()
    client_cm.__enter__.return_value.get.side_effect = _get_side_effect
    with patch("vsniper.services.search_service.httpx.Client", return_value=client_cm):
        result = service._download_candidate_image(
            candidate_id="s:item-4",
            image_urls=["http://x/1.jpg", "http://x/2.jpg", "http://x/3.jpg"],
        )

    assert result is not None
    assert len(result.extra_image_bytes) == 1
    assert len(result.cache_paths) == 2
    assert service._candidate_cache_path("s:item-4").exists()
    assert service._candidate_cache_path("s:item-4", image_index=2).exists()


def test_cancel_check_stops_dispatching_further_batches_but_keeps_partial_work(memo_db) -> None:
    """Cancellation is checked between groups of parallel judge requests (one listing per group
    here), so a cancel mid-run stops new work but must not discard what already judged and
    persisted."""
    from vsniper.db.models import Search

    session = memo_db()
    session.add(Search(id="search-001", name="Test", clothing_item="hosen", queries=["q"], region="de", run_status="running"))
    session.commit()
    session.close()

    service = _make_service()
    service.telegram = MagicMock()
    service.telegram.queue_delivery.return_value = False
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw(f"item-{index}") for index in range(4)]
    jpeg = _jpeg()
    judged_calls: list[str] = []

    def _judge(*, candidate: CandidateImageInput, **kwargs) -> CandidateJudgment:
        item = candidate
        judged_calls.append(item.candidate_id)
        return CandidateJudgment(score=80, explanation="ok", labels=[], concerns=[])

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate.side_effect = _judge

    cancel_after = 1

    def cancel_check() -> bool:
        return len(judged_calls) >= cancel_after

    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages, alert_count, queued = service._judge_candidates(
            candidates,
            search,
            taste,
            _db_settings(vlm_judge_parallel_requests=1),
            judge_model=_db_settings().judge_model,
            judge_fallback_model=None,
            mode="live",
            cancel_check=cancel_check,
        )

    # Only the first candidate was judged before cancellation stopped further dispatch.
    assert len(judged_calls) == 1
    judged_ids = {cid for cid, stage in stages.items() if stage == "vlm_judged"}
    assert judged_ids == set(judged_calls)

    # What was judged before the cancel must already be persisted, not discarded.
    session = memo_db()
    persisted = session.query(Candidate).filter(Candidate.id.in_(judged_ids)).all()
    assert {c.id for c in persisted} == judged_ids
    session.close()


def test_undecodable_cached_image_marks_failed_and_deletes_cache(tmp_path: Path) -> None:
    # A pre-existing poisoned cache file (written before decode-validation) reaches
    # listing-image assembly and raises UnidentifiedImageError. This must degrade to a
    # failed candidate and delete the poison file, not crash the whole scan run.
    service = _make_service()
    service.settings.resolve_path.side_effect = lambda p: tmp_path
    service.settings.cache_dir = "cache"
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")

    poison_path = service._candidate_cache_path("search-001:item-1")
    poison_path.parent.mkdir(parents=True, exist_ok=True)
    poison_path.write_bytes(b"not an image")

    service.taste_client.judge_candidate.side_effect = UnidentifiedImageError("cannot identify image")

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=b"not an image")

    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            [_raw("item-1")],
            search,
            taste,
            _db_settings(),
        )

    assert stages["search-001:item-1"] == "failed"
    assert "could not be decoded" in traces["search-001:item-1"].explanation
    assert not poison_path.exists()


VERIFIER = ResolvedAiModel(
    id="model-verify", provider="openai", model_name="gpt-verify", reasoning_effort="low", local_base_url=None
)


def _cascade_judge(local_scores: dict[str, int], verified_scores: dict[str, int | Exception]):
    """Fake judge: the local model scores by title, the verifier by title or raises."""
    calls: list[tuple[str, str]] = []

    def _judge(*, candidate, ai_judge_provider, model, **kwargs):
        calls.append((model, candidate.title))
        if ai_judge_provider == "local":
            return CandidateJudgment(score=local_scores[candidate.title], explanation="local", labels=[], concerns=[])
        result = verified_scores[candidate.title]
        if isinstance(result, Exception):
            raise result
        return CandidateJudgment(score=result, explanation="verified", labels=["v"], concerns=["deep V-neck"])

    return _judge, calls


def _judge_with_cascade(service, raws, search, taste, **kwargs):
    jpeg = _jpeg()

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    with patch.object(service, "_download_candidate_image", side_effect=_download):
        return _call_judge(service, raws, search, taste, _db_settings(), **kwargs)


def test_shortlisted_listings_are_verified_and_the_verifier_decides_the_alert() -> None:
    """Production 2026-11-08: the local 2B judge scored deep-V-neck cardigans the user disliked at
    76-95, above their alert threshold. A strong hosted model re-judges only listings that reach
    the search threshold; its score and the verified alert threshold decide the alert."""
    service = _make_service()
    search = _search().model_copy(update={"alert_threshold": 80, "effective_alert_threshold": 80})
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    judge, calls = _cascade_judge({"vneck": 85, "great": 84, "meh": 60}, {"vneck": 40, "great": 91})
    service.taste_client.judge_candidate.side_effect = judge

    traces, stages = _judge_with_cascade(
        service, [_raw("1", title="vneck"), _raw("2", title="great"), _raw("3", title="meh")], search, taste,
        verify_model=VERIFIER, verify_alert_threshold=85,
    )

    vneck, great, meh = (traces[f"search-001:{i}"] for i in ("1", "2", "3"))
    assert (vneck.score_10, vneck.decision, vneck.screen_score, vneck.model) == (40, "discard", 85, "gpt-verify")
    assert vneck.concerns == ["deep V-neck"]
    assert (great.score_10, great.decision, great.screen_score, great.threshold) == (91, "alert", 84, 0.85)
    assert (meh.score_10, meh.decision, meh.screen_score) == (60, "review", None)
    assert sorted(c for c in calls if c[0] == "gpt-verify") == [("gpt-verify", "great"), ("gpt-verify", "vneck")]
    assert set(stages.values()) == {"vlm_judged"}


def test_failed_verification_holds_the_alert_and_is_not_rejudged(memo_db) -> None:
    """Without a working verifier (no API key, exhausted credits, a moderation false positive) the
    local score alone must not alert, the listing must not be re-judged on every run, and the
    failures surface as one error per run rather than one per listing."""
    service = _make_service()
    service.errors = MagicMock()
    search = _search().model_copy(update={"alert_threshold": 80, "effective_alert_threshold": 80})
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    failure = OpenAIIntegrationError("insufficient credits")
    judge, _ = _cascade_judge({"a": 88, "b": 90}, {"a": failure, "b": failure})
    service.taste_client.judge_candidate.side_effect = judge

    traces, _ = _judge_with_cascade(
        service, [_raw("1", title="a"), _raw("2", title="b")], search, taste,
        verify_model=VERIFIER, verify_alert_threshold=75,
    )

    trace = traces["search-001:1"]
    assert (trace.score_10, trace.decision, trace.screen_score) == (88, "review", None)
    assert "insufficient credits" in (trace.verify_error or "")
    assert service.errors.record.call_count == 1
    assert service.errors.record.call_args.kwargs["details"]["failed"] == 2

    _seed_judged_candidate(memo_db, "search-001:1", trace)
    reused = service._memoized_judgments(
        candidate_ids=["search-001:1"], profile_version=taste.version, alert_threshold=80,
        verify_model=VERIFIER, verify_alert_threshold=75,
    )
    assert reused["search-001:1"].decision == "review"


def test_daily_verification_limit_bounds_the_verifier_calls() -> None:
    service = _make_service()
    service.errors = MagicMock()
    search = _search().model_copy(update={"alert_threshold": 80, "effective_alert_threshold": 80})
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    judge, calls = _cascade_judge({"a": 88, "b": 90}, {"a": 91, "b": 92})
    service.taste_client.judge_candidate.side_effect = judge

    traces, _ = _judge_with_cascade(
        service, [_raw("1", title="a"), _raw("2", title="b")], search, taste,
        verify_model=VERIFIER, verify_alert_threshold=75, verify_budget=1,
    )

    assert sum(model == "gpt-verify" for model, _ in calls) == 1
    decisions = sorted((t.decision, t.verify_error is not None) for t in traces.values())
    assert decisions == [("alert", False), ("review", True)]


def test_memoized_verified_judgment_keeps_the_verified_threshold(memo_db) -> None:
    service = _make_service()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    verified = build_judgment_trace(
        judgment=CandidateJudgment(score=86, explanation="verified", labels=[], concerns=[]),
        taste_profile=taste, model="gpt-verify", alert_threshold=90,
    ).model_copy(update={"screen_score": 82, "screen_model": "local"})
    _seed_judged_candidate(memo_db, "search-001:1", verified)
    unverified_low = build_judgment_trace(
        judgment=CandidateJudgment(score=70, explanation="local", labels=[], concerns=[]),
        taste_profile=taste, model="local", alert_threshold=80,
    )
    _seed_judged_candidate(memo_db, "search-001:2", unverified_low)

    with_verifier = service._memoized_judgments(
        candidate_ids=["search-001:1", "search-001:2"], profile_version=taste.version, alert_threshold=80,
        verify_model=VERIFIER, verify_alert_threshold=85,
    )
    without_verifier = service._memoized_judgments(
        candidate_ids=["search-001:1", "search-001:2"], profile_version=taste.version, alert_threshold=80,
        verify_model=None, verify_alert_threshold=85,
    )

    assert (with_verifier["search-001:1"].decision, with_verifier["search-001:1"].threshold) == ("alert", 0.85)
    assert with_verifier["search-001:2"].decision == "review"
    # Without a verifier the stored verified score is on another scale: judge the listing again.
    assert set(without_verifier) == {"search-001:2"}


def test_verified_today_counts_only_todays_verified_judgments(memo_db) -> None:
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    base = build_judgment_trace(
        judgment=CandidateJudgment(score=90, explanation="x", labels=[], concerns=[]),
        taste_profile=taste, model="gpt-verify", alert_threshold=75,
    )
    _seed_judged_candidate(memo_db, "search-001:1", base.model_copy(update={"screen_score": 85}))
    yesterday = base.judged_at - timedelta(days=1)
    _seed_judged_candidate(memo_db, "search-001:2", base.model_copy(update={"screen_score": 85, "judged_at": yesterday}))
    _seed_judged_candidate(memo_db, "search-001:3", base)

    with memo_db() as session:
        assert SearchService._verified_today(session) == 1


def test_concurrent_runs_share_the_local_judge_slots() -> None:
    """Production 2026-11-09: four concurrent search runs each sent the configured two parallel
    requests to the two-slot local judge. Requests queued in the server past the 300 s client
    timeout, the retries kept the abandoned work coming, and the local judge produced nothing for
    eight hours. Requests to one local server must share its slot count across runs."""
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    jpeg = _jpeg()
    lock = threading.Lock()
    active = 0
    max_active = 0

    def _judge(**kwargs) -> CandidateJudgment:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        threading.Event().wait(0.02)
        with lock:
            active -= 1
        return CandidateJudgment(score=60, explanation="ok", labels=[], concerns=[])

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    def _run(search_id: str) -> None:
        service = _make_service()
        service.taste_client.judge_candidate.side_effect = _judge
        with patch.object(service, "_download_candidate_image", side_effect=_download):
            _call_judge(
                service, [_raw(f"{search_id}-{i}") for i in range(8)], _search(search_id), taste,
                _db_settings(vlm_judge_parallel_requests=2),
            )

    runs = [threading.Thread(target=_run, args=(f"search-{n}",)) for n in range(3)]
    for run in runs:
        run.start()
    for run in runs:
        run.join()

    assert max_active == 2


def test_thresholds_changed_during_a_run_apply_to_its_later_decisions(memo_db) -> None:
    """Production 2026-11-09: a run that started before the verified alert score was raised from
    75 to 90 kept deciding with 75 for nine more hours and sent 89 alerts. Decisions use the
    thresholds stored when the listing is judged, not those read when the run started."""
    with memo_db() as session:
        session.add(AppSettingsState(id=1, vinted_region="de", alert_threshold=80, verify_alert_threshold=90))
        session.add(Search(id="search-001", name="Test", clothing_item="hosen", region="de", alert_threshold=84))
        session.commit()
    service = _make_service()
    search = _search().model_copy(update={"alert_threshold": 80, "effective_alert_threshold": 80})
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    judge, calls = _cascade_judge({"short": 86, "below": 82}, {"short": 88})
    service.taste_client.judge_candidate.side_effect = judge

    traces, _ = _judge_with_cascade(
        service, [_raw("1", title="short"), _raw("2", title="below")], search, taste,
        verify_model=VERIFIER, verify_alert_threshold=75,
    )

    short, below = traces["search-001:1"], traces["search-001:2"]
    assert (short.score_10, short.decision, short.threshold) == (88, "review", 0.9)
    assert (below.score_10, below.decision, below.screen_score) == (82, "review", None)
    assert [title for model, title in calls if model == "gpt-verify"] == ["short"]


def test_rejudging_after_a_recompute_is_capped_per_run_and_new_listings_go_first(memo_db, monkeypatch) -> None:
    """A taste recompute invalidates every stored judgment. Re-judging a search's few hundred
    listings in one run kept that run going for nine hours, so new listings waited and the run held
    stale settings. Each run judges every new listing first and only a bounded number of stale
    ones; the rest keep their stored judgment until a later run."""
    monkeypatch.setattr("vsniper.services.search_service.STALE_REJUDGES_PER_RUN", 1)
    service = _make_service()
    taste = TasteProfile(summary="", taste_prompt="likes vintage", version=2)
    stale = build_judgment_trace(
        judgment=CandidateJudgment(score=70, explanation="old profile", labels=[], concerns=[]),
        taste_profile=TasteProfile(summary="", taste_prompt="x", version=1),
        model="local",
    )
    for item in ("old-1", "old-2", "old-3"):
        _seed_judged_candidate(memo_db, f"search-001:{item}", stale)
    order: list[str] = []

    def _judge(*, candidate: CandidateImageInput, **kwargs) -> CandidateJudgment:
        order.append(candidate.candidate_id.split(":")[1])
        return CandidateJudgment(score=40, explanation="current profile", labels=[], concerns=[])

    service.taste_client.judge_candidate.side_effect = _judge
    new_judged_ids: set[str] = set()
    raws = [_raw("old-1"), _raw("new-1"), _raw("old-2"), _raw("new-2"), _raw("old-3")]
    traces, _ = _judge_with_cascade(service, raws, _search(), taste, new_judged_ids=new_judged_ids)

    assert order == ["new-1", "new-2", "old-1"]
    assert set(traces) == {"search-001:new-1", "search-001:new-2", "search-001:old-1"}
    assert new_judged_ids == set(traces)


def test_switching_the_judge_model_rejudges_stored_judgments(memo_db) -> None:
    """Scores from another judge model sit on another scale; reusing them would compare an old
    model's score with thresholds tuned for the new one."""
    service = _make_service()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    db_settings = _db_settings()
    for item, model in (("old", "retired-local-model"), ("same", db_settings.judge_model.model_name)):
        _seed_judged_candidate(
            memo_db,
            f"search-001:{item}",
            build_judgment_trace(
                judgment=CandidateJudgment(score=70, explanation=model, labels=[], concerns=[]),
                taste_profile=taste, model=model,
            ),
        )
    service.taste_client.judge_candidate.return_value = CandidateJudgment(
        score=40, explanation="current model", labels=[], concerns=[]
    )

    traces, _ = _judge_with_cascade(service, [_raw("old"), _raw("same")], _search(), taste)

    assert traces["search-001:old"].score_10 == 40
    assert traces["search-001:same"].score_10 == 70
