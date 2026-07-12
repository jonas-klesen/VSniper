"""Tests for SearchService._judge_candidates — the VLM judging pipeline.

Covers the riskiest outcomes: all candidates being judged, reuse of candidates already judged
under the current profile version, image-download failure, and a partial grid response where the
model omits a position.
"""
from __future__ import annotations

import threading
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
from vsniper.db.models import Candidate
from vsniper.domain.contracts import (
    CandidateJudgment,
    ScoreTrace,
    SearchRecord,
    TasteProfile,
)
from vsniper.domain.scoring.service import build_judgment_trace
from vsniper.integrations.openai.client import (
    CandidateGridResult,
    CandidateImageInput,
    OpenAIIntegrationError,
)
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
    vlm_grid_size: int = 4,
    vlm_judge_parallel_requests: int = 1,
    ai_judge_provider: str = "local",
    ai_judge_model: str = "gpt-5.4-mini",
    local_judge_model: str = "gemma4-12b-quality",
    ai_judge_allow_openai_fallback: bool = False,
    ai_judge_fallback_provider: str | None = None,
    cerebras_judge_model: str = "gemma-4-31b",
    vlm_pack_multiple_listing_images: bool = True,
) -> SimpleNamespace:
    """Builds a plain settings namespace plus the registry-resolved judge/fallback models that
    `_judge_candidates`/`_judge_image_batch` now take as explicit params (post AI-models-registry
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
        vlm_grid_size=vlm_grid_size,
        vlm_judge_parallel_requests=vlm_judge_parallel_requests,
        vlm_pack_multiple_listing_images=vlm_pack_multiple_listing_images,
        ai_judge_image_max_px=512,
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
    return SearchRecord(id=search_id, name="Test", clothing_item="hosen", query="vintage", region="de")


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


def test_multi_image_setting_controls_downloaded_listing_url_count() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    raw = _raw("item-1")
    raw["image_urls"] = [f"https://images.vinted.de/item-1-{index}.jpg" for index in range(6)]
    seen_counts: list[int] = []
    jpeg = _jpeg()

    def _judge(*, candidates: list[CandidateImageInput], **kwargs) -> CandidateGridResult:
        item = candidates[0]
        return CandidateGridResult(
            batch_id="b",
            image_bytes=jpeg,
            judgments={
                item.candidate_id: CandidateJudgment(position="top_left", score=80, explanation="ok", labels=[], concerns=[])
            },
        )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        seen_counts.append(len(image_urls))
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        _call_judge(service, [raw], search, taste, _db_settings(vlm_pack_multiple_listing_images=False))
        _call_judge(service, [raw], search, taste, _db_settings(vlm_pack_multiple_listing_images=True))

    assert seen_counts == [1, 4]


def test_all_candidates_are_judged() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw(f"item-{index}") for index in range(5)]

    jpeg = _jpeg()

    def _judge(*, candidates: list[CandidateImageInput], **kwargs) -> CandidateGridResult:
        return CandidateGridResult(
            batch_id="b",
            image_bytes=jpeg,
            judgments={
                item.candidate_id: CandidateJudgment(position="top_left", score=80, explanation="ok", labels=[], concerns=[])
                for item in candidates
            },
        )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            candidates,
            search,
            taste,
            _db_settings(vlm_grid_size=4),
        )

    judged = [c for c, s in stages.items() if s == "vlm_judged"]
    assert len(judged) == 5
    assert service.taste_client.judge_candidate_grid.call_count == 2
    assert len([trace for trace in traces.values() if trace.score_10 == 80]) == 5


def test_vlm_grid_size_one_judges_each_candidate_individually() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw(f"item-{index}") for index in range(5)]

    jpeg = _jpeg()

    def _judge(*, candidates: list[CandidateImageInput], **kwargs) -> CandidateGridResult:
        assert len(candidates) == 1
        item = candidates[0]
        return CandidateGridResult(
            batch_id="b",
            image_bytes=jpeg,
            judgments={
                item.candidate_id: CandidateJudgment(
                    position="top_left",
                    score=80,
                    explanation="ok",
                    labels=[],
                    concerns=[],
                )
            },
        )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            candidates,
            search,
            taste,
            _db_settings(vlm_grid_size=1),
        )

    assert service.taste_client.judge_candidate_grid.call_count == 5
    assert len([stage for stage in stages.values() if stage == "vlm_judged"]) == 5
    assert len([trace for trace in traces.values() if trace.score_10 == 80]) == 5


def test_already_judged_candidate_is_reused_not_rejudged(memo_db) -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")  # version defaults to 1

    # item-1 was already VLM-judged under profile version 1; item-2 has never been seen.
    prior = build_judgment_trace(
        judgment=CandidateJudgment(position="top_left", score=80, explanation="seen before", labels=[], concerns=[]),
        taste_profile=taste,
        model="local",
        batch_id="prior",
    )
    _seed_judged_candidate(memo_db, "search-001:item-1", prior)

    jpeg = _jpeg()

    def _judge(*, candidates: list[CandidateImageInput], **kwargs) -> CandidateGridResult:
        return CandidateGridResult(
            batch_id="b",
            image_bytes=jpeg,
            judgments={
                item.candidate_id: CandidateJudgment(position="top_left", score=30, explanation="fresh", labels=[], concerns=[])
                for item in candidates
            },
        )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
    new_judged_ids: set[str] = set()
    with patch.object(service, "_download_candidate_image", side_effect=_download) as download:
        traces, stages = _call_judge(service, 
            [_raw("item-1"), _raw("item-2")],
            search,
            taste,
            _db_settings(vlm_grid_size=4),
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


def test_threshold_change_rejudges_memoized_candidate(memo_db) -> None:
    service = _make_service()
    search = _search().model_copy(update={"alert_threshold": 80, "effective_alert_threshold": 80})
    taste = TasteProfile(summary="", taste_prompt="likes vintage")

    prior = build_judgment_trace(
        judgment=CandidateJudgment(position="top_left", score=80, explanation="old threshold", labels=[], concerns=[]),
        taste_profile=taste,
        model="local",
        batch_id="prior",
        alert_threshold=95,
    )
    _seed_judged_candidate(memo_db, "search-001:item-1", prior)

    jpeg = _jpeg()

    def _judge(*, candidates: list[CandidateImageInput], **kwargs) -> CandidateGridResult:
        return CandidateGridResult(
            batch_id="b",
            image_bytes=jpeg,
            judgments={
                item.candidate_id: CandidateJudgment(position="top_left", score=80, explanation="new threshold", labels=[], concerns=[])
                for item in candidates
            },
        )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download) as download:
        traces, stages = _call_judge(service, 
            [_raw("item-1")],
            search,
            taste,
            _db_settings(vlm_grid_size=1),
        )

    assert stages["search-001:item-1"] == "vlm_judged"
    assert traces["search-001:item-1"].threshold == 0.8
    assert traces["search-001:item-1"].decision == "alert"
    download.assert_called_once()


def test_stale_version_and_failed_candidates_are_rejudged(memo_db) -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage", version=2)

    # item-1 was judged under an older profile version; item-2's last attempt failed.
    stale = build_judgment_trace(
        judgment=CandidateJudgment(position="top_left", score=80, explanation="old profile", labels=[], concerns=[]),
        taste_profile=TasteProfile(summary="", taste_prompt="x", version=1),
        model="local",
        batch_id="old",
    )
    _seed_judged_candidate(memo_db, "search-001:item-1", stale)
    _seed_judged_candidate(memo_db, "search-001:item-2", stale, grading_stage="failed")

    jpeg = _jpeg()

    def _judge(*, candidates: list[CandidateImageInput], **kwargs) -> CandidateGridResult:
        return CandidateGridResult(
            batch_id="b",
            image_bytes=jpeg,
            judgments={
                item.candidate_id: CandidateJudgment(position="top_left", score=50, explanation="rejudged", labels=[], concerns=[])
                for item in candidates
            },
        )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            [_raw("item-1"), _raw("item-2")],
            search,
            taste,
            _db_settings(vlm_grid_size=4),
        )

    # Both were re-judged: stale-version and failed rows are never reused.
    assert traces["search-001:item-1"].score_10 == 50
    assert traces["search-001:item-2"].score_10 == 50


def test_vlm_judging_includes_size_metadata() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    jpeg = _jpeg()

    def _judge(*, candidates: list[CandidateImageInput], **kwargs) -> CandidateGridResult:
        assert candidates[0].size == "M"
        return CandidateGridResult(
            batch_id="b",
            image_bytes=jpeg,
            judgments={
                candidates[0].candidate_id: CandidateJudgment(
                    position="top_left",
                    score=80,
                    explanation="ok",
                    labels=[],
                    concerns=[],
                )
            },
        )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
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
    service.taste_client.judge_candidate_grid.assert_not_called()


def test_partial_grid_null_marks_missing_position_as_failed() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw("item-1"), _raw("item-2")]

    jpeg = _jpeg()
    # Model only returns a judgment for item-1; item-2 position is omitted (null)
    service.taste_client.judge_candidate_grid.return_value = CandidateGridResult(
        batch_id="b",
        image_bytes=jpeg,
        judgments={
            "search-001:item-1": CandidateJudgment(position="top_left", score=90, explanation="great", labels=[], concerns=[]),
        },
    )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            candidates,
            search,
            taste,
            _db_settings(),
        )

    assert stages["search-001:item-1"] == "vlm_judged"
    assert traces["search-001:item-1"].score_10 == 90

    assert stages["search-001:item-2"] == "failed"
    assert "null or omitted" in traces["search-001:item-2"].explanation


def test_grid_failure_recovers_by_judging_items_individually() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw("item-1"), _raw("item-2")]
    jpeg = _jpeg()

    # The multi-item grid call fails (e.g. transient timeout); singleton retries succeed.
    def _judge(*, taste_profile, candidates, liked_anchors, disliked_anchors, manual_note, **kwargs):
        if len(candidates) > 1:
            raise OpenAIIntegrationError("grid call failed")
        cid = candidates[0].candidate_id
        return CandidateGridResult(
            batch_id="b",
            image_bytes=jpeg,
            judgments={cid: CandidateJudgment(position="top_left", score=80, explanation="ok", labels=[], concerns=[])},
        )

    service.taste_client.judge_candidate_grid.side_effect = _judge

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            candidates,
            search,
            taste,
            _db_settings(vlm_grid_size=4),
        )

    assert stages["search-001:item-1"] == "vlm_judged"
    assert stages["search-001:item-2"] == "vlm_judged"
    assert traces["search-001:item-1"].score_10 == 80


def test_grid_failure_marks_failed_when_individual_retry_also_fails() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw("item-1"), _raw("item-2")]
    jpeg = _jpeg()

    # Every call fails — even the per-item retries — so both candidates end up failed.
    service.taste_client.judge_candidate_grid.side_effect = OpenAIIntegrationError("provider down")

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            candidates,
            search,
            taste,
            _db_settings(vlm_grid_size=4),
        )

    assert stages["search-001:item-1"] == "failed"
    assert stages["search-001:item-2"] == "failed"
    assert "provider down" in traces["search-001:item-1"].explanation


def test_local_judge_falls_back_to_openai_when_enabled() -> None:
    service = _make_service()
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")
    candidates = [_raw("item-1")]
    jpeg = _jpeg()

    def _judge(*, candidates, ai_judge_provider, model, **kwargs):
        if ai_judge_provider == "local":
            assert model == "local-model"
            raise OpenAIIntegrationError("local down")
        assert ai_judge_provider == "openai"
        assert model == "gpt-fallback"
        return CandidateGridResult(
            batch_id="fallback",
            image_bytes=jpeg,
            judgments={
                candidates[0].candidate_id: CandidateJudgment(
                    position="top_left",
                    score=90,
                    explanation="Fallback judged it well.",
                    labels=["vintage"],
                    concerns=[],
                )
            },
        )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
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

    def _judge(*, candidates, ai_judge_provider, model, **kwargs):
        if ai_judge_provider == "local":
            assert model == "local-model"
            raise OpenAIIntegrationError("local down")
        assert ai_judge_provider == "cerebras"
        assert model == "gemma-4-31b"
        return CandidateGridResult(
            batch_id="cerebras-fallback",
            image_bytes=jpeg,
            judgments={
                candidates[0].candidate_id: CandidateJudgment(
                    position="top_left",
                    score=80,
                    explanation="Cerebras fallback judged it well.",
                    labels=["vintage"],
                    concerns=[],
                )
            },
        )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
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

    def _judge(*, candidates, ai_judge_provider, model, **kwargs):
        if ai_judge_provider == "openai":
            assert model == "gpt-primary"
            raise OpenAIIntegrationError("openai down")
        assert ai_judge_provider == "cerebras"
        assert model == "gemma-4-31b"
        return CandidateGridResult(
            batch_id="cerebras-fallback",
            image_bytes=jpeg,
            judgments={
                candidates[0].candidate_id: CandidateJudgment(
                    position="top_left",
                    score=70,
                    explanation="Cerebras fallback judged it well.",
                    labels=["vintage"],
                    concerns=[],
                )
            },
        )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
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

    service.taste_client.judge_candidate_grid.side_effect = OpenAIIntegrationError("openai down")

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
    assert service.taste_client.judge_candidate_grid.call_count == 1


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

    def _judge(*, taste_profile, candidates, liked_anchors, disliked_anchors, manual_note, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 4:
                release.set()
        try:
            assert release.wait(timeout=2)
            return CandidateGridResult(
                batch_id="b",
                image_bytes=jpeg,
                judgments={
                    item.candidate_id: CandidateJudgment(
                        position="top_left",
                        score=80,
                        explanation="ok",
                        labels=[],
                        concerns=[],
                    )
                    for item in candidates
                },
            )
        finally:
            with lock:
                active -= 1

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
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

    def _judge(*, taste_profile, candidates, liked_anchors, disliked_anchors, manual_note, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 8:
                release.set()
        try:
            assert release.wait(timeout=2)
            return CandidateGridResult(
                batch_id="b",
                image_bytes=jpeg,
                judgments={
                    item.candidate_id: CandidateJudgment(
                        position="top_left",
                        score=80,
                        explanation="ok",
                        labels=[],
                        concerns=[],
                    )
                    for item in candidates
                },
            )
        finally:
            with lock:
                active -= 1

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge
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
    """Cancellation is checked between judge-batch groups (grid_size=1 -> one candidate per
    group here), so a cancel mid-run stops new work but must not discard what already judged
    and persisted."""
    from vsniper.db.models import Search

    session = memo_db()
    session.add(Search(id="search-001", name="Test", clothing_item="hosen", query="q", region="de", run_status="running"))
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

    def _judge(*, candidates: list[CandidateImageInput], **kwargs) -> CandidateGridResult:
        item = candidates[0]
        judged_calls.append(item.candidate_id)
        return CandidateGridResult(
            batch_id="b",
            image_bytes=jpeg,
            judgments={item.candidate_id: CandidateJudgment(position="top_left", score=80, explanation="ok", labels=[], concerns=[])},
        )

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=jpeg)

    service.taste_client.judge_candidate_grid.side_effect = _judge

    cancel_after = 1

    def cancel_check() -> bool:
        return len(judged_calls) >= cancel_after

    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages, alert_count, queued = service._judge_candidates(
            candidates,
            search,
            taste,
            _db_settings(vlm_grid_size=1, vlm_judge_parallel_requests=1),
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
    # contact-sheet assembly and raises UnidentifiedImageError. This must degrade to a
    # failed candidate and delete the poison file, not crash the whole scan run.
    service = _make_service()
    service.settings.resolve_path.side_effect = lambda p: tmp_path
    service.settings.cache_dir = "cache"
    search = _search()
    taste = TasteProfile(summary="", taste_prompt="likes vintage")

    poison_path = service._candidate_cache_path("search-001:item-1")
    poison_path.parent.mkdir(parents=True, exist_ok=True)
    poison_path.write_bytes(b"not an image")

    service.taste_client.judge_candidate_grid.side_effect = UnidentifiedImageError("cannot identify image")

    def _download(*, candidate_id: str, image_urls: list[str]) -> CandidateImageInput:
        return CandidateImageInput(candidate_id=candidate_id, image_bytes=b"not an image")

    with patch.object(service, "_download_candidate_image", side_effect=_download):
        traces, stages = _call_judge(service, 
            [_raw("item-1")],
            search,
            taste,
            _db_settings(vlm_grid_size=4),
        )

    assert stages["search-001:item-1"] == "failed"
    assert "could not be decoded" in traces["search-001:item-1"].explanation
    assert not poison_path.exists()
