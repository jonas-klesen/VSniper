"""Tests for SearchService.claim_for_run — the DB-backed run lease.

The key behavior under test (#4): a run that failed before persisting is keyed on
run_status, so it becomes reclaimable after one interval instead of being locked out
until the 3x stale cutoff, while a run still "running" is only recoverable once stale.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from vsniper.core.database import Base
from vsniper.db.models import AiModelConfig, AppSettingsState, Candidate, Search, SearchRun
from vsniper.domain.contracts import ScoreTrace, SearchFilter, SearchRecord, SearchRunResult, SearchUpdate, TasteProfile
from vsniper.integrations.vinted.client import VintedSessionError
from vsniper.integrations.vinted.categories import CategoryFilterError
from vsniper.services._mapping import build_session_health, candidate_to_contract
from vsniper.services.operations_service import CLAIM_STALE_THRESHOLD
from vsniper.services.search_defaults import CANONICAL_CLOTHING_ITEMS, canonical_search_values
from vsniper.services.search_service import (
    _CACHE_MAX_SIZE_MB,
    SearchClothingItemImmutable,
    SearchRunAlreadyClaimed,
    SearchService,
    _evict_stale_cache,
)

INTERVAL = timedelta(seconds=60)


def _setup(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'claim.db'}", future=True)

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
    monkeypatch.setattr("vsniper.services.operations_service.session_scope", fake_session_scope)

    with fake_session_scope() as session:
        session.add(Search(id="s1", name="S", clothing_item="hosen", query="q", region="de", run_status="idle"))

    service = SearchService.__new__(SearchService)
    return service, factory


def _set(factory, **values):
    session = factory()
    search = session.get(Search, "s1")
    for key, value in values.items():
        setattr(search, key, value)
    session.commit()
    session.close()


def _row(factory) -> Search:
    session = factory()
    search = session.get(Search, "s1")
    session.expunge(search)
    session.close()
    return search


def _candidate(candidate_id: str, search_id: str = "s1") -> Candidate:
    return Candidate(
        id=candidate_id,
        search_id=search_id,
        clothing_item="hosen",
        title=f"Candidate {candidate_id}",
        brand="Brand",
        price_eur=20.0,
        size="M",
        url=f"https://example.test/{candidate_id}",
        image_urls=[],
        matched_filters=[],
        matched_preferences=[],
        features=[],
        normalized_listing={},
        extraction_status="completed",
        score_trace={"final_score": 8.0, "decision": "alert"},
        decision="alert",
        final_score=8.0,
        ai_observation={},
        grading_stage="vlm_judged",
        feedback="unknown",
        feedback_comment="",
        created_at=datetime.now(UTC),
    )


def _raw_candidate(external_id: str = "item-1") -> dict:
    return {
        "id": external_id,
        "external_item_id": external_id,
        "title": "Cargos",
        "brand": "Brand",
        "price_eur": 25.0,
        "size": "M",
        "url": f"https://www.vinted.de/items/{external_id}",
        "image_urls": [],
        "description": "",
        "features": [],
        "raw_listing": {},
    }


def _latest_run(factory) -> SearchRun:
    session = factory()
    try:
        run = session.query(SearchRun).order_by(SearchRun.id.desc()).first()
        assert run is not None
        session.expunge(run)
        return run
    finally:
        session.close()


def test_fresh_search_is_claimed_and_marked_running(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)

    assert service.claim_for_run("s1", INTERVAL) is True
    row = _row(factory)
    assert row.run_status == "running"
    assert row.last_claimed_at is not None


def test_running_within_interval_is_not_reclaimed(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    service.claim_for_run("s1", INTERVAL)  # now running, claimed just now

    assert service.claim_for_run("s1", INTERVAL) is False


def test_failed_run_is_reclaimed_after_one_interval(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    # A run that failed before persisting, claimed 90s ago (> 1 interval, < 3x stale cutoff).
    _set(factory, run_status="failed", last_claimed_at=datetime.now(UTC) - timedelta(seconds=90))

    assert service.claim_for_run("s1", INTERVAL) is True
    assert _row(factory).run_status == "running"


def test_running_run_is_not_reclaimed_until_stale(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    # Still "running", claimed 90s ago: past one interval but not yet the 3x stale cutoff.
    _set(factory, run_status="running", last_claimed_at=datetime.now(UTC) - timedelta(seconds=90))

    assert service.claim_for_run("s1", INTERVAL) is False


def test_stale_running_run_is_recovered(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    # A worker crashed mid-run: "running" and claimed 200s ago (> 3x interval).
    _set(factory, run_status="running", last_claimed_at=datetime.now(UTC) - timedelta(seconds=200))

    assert service.claim_for_run("s1", INTERVAL) is True


def test_stale_reclaim_closes_orphaned_search_run_as_failed(monkeypatch, tmp_path) -> None:
    """A crashed process leaves both Search.run_status and its SearchRun row stuck "running".

    Without cleanup, that SearchRun row shows up forever in the "Recent scan runs" history as
    "running" even though it will never be touched again — this is the root cause of searches
    appearing to run more than once concurrently. Reclaiming the stale search must close it out.
    """
    service, factory = _setup(monkeypatch, tmp_path)
    _set(factory, run_status="running", last_claimed_at=datetime.now(UTC) - timedelta(seconds=200))
    with _session_scope_of(factory) as session:
        session.add(
            SearchRun(search_id="s1", mode="live", trigger="worker", status="running", started_at=datetime.now(UTC))
        )

    assert service.claim_for_run("s1", INTERVAL) is True

    with _session_scope_of(factory) as session:
        orphaned = session.query(SearchRun).filter_by(search_id="s1").one()
        assert orphaned.status == "failed"
        assert orphaned.finished_at is not None
        assert "Orphaned" in orphaned.error


def test_claim_clears_a_leftover_cancel_flag(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    # A cancel was requested but the process died before clearing the flag.
    _set(
        factory,
        run_status="failed",
        last_claimed_at=datetime.now(UTC) - timedelta(seconds=90),
        cancel_requested_at=datetime.now(UTC),
    )

    assert service.claim_for_run("s1", INTERVAL) is True
    assert _row(factory).cancel_requested_at is None


def test_manual_claim_does_not_reclaim_active_run_before_manual_stale_cutoff(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    _set(factory, run_status="running", last_claimed_at=datetime.now(UTC) - timedelta(seconds=90))

    assert service.claim_for_run("s1", timedelta(0), stale_after=timedelta(minutes=30)) is False


def test_api_live_run_claims_before_running(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_run(search_id: str, *, mode: str, trigger: str = "worker") -> SearchRunResult:
        assert _row(factory).run_status == "running"
        calls.append((search_id, mode))
        return SearchRunResult(
            search_id=search_id,
            mode="live",
            fetched_candidates=0,
            alert_candidates=0,
            summary="ok",
        )

    monkeypatch.setattr(service, "_run", fake_run)

    result = service.run_live("s1")

    assert result.summary == "ok"
    assert calls == [("s1", "live")]


def test_api_live_run_rejects_active_claim(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    _set(factory, run_status="running", last_claimed_at=datetime.now(UTC))

    with pytest.raises(SearchRunAlreadyClaimed):
        service.run_live("s1")


def test_worker_live_run_can_use_existing_claim(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    _set(factory, run_status="running", last_claimed_at=datetime.now(UTC))
    calls: list[tuple[str, str]] = []

    def fake_run(search_id: str, *, mode: str, trigger: str = "worker") -> SearchRunResult:
        calls.append((search_id, mode))
        return SearchRunResult(
            search_id=search_id,
            mode="live",
            fetched_candidates=0,
            alert_candidates=0,
            summary="ok",
        )

    monkeypatch.setattr(service, "_run", fake_run)

    assert service.run_live("s1", already_claimed=True).summary == "ok"
    assert calls == [("s1", "live")]


def test_api_live_run_preserves_missing_search_404(monkeypatch, tmp_path) -> None:
    service, _ = _setup(monkeypatch, tmp_path)

    with pytest.raises(KeyError):
        service.run_live("missing")


def test_evict_stale_cache_trims_to_cap_without_wiping_everything(tmp_path) -> None:
    cap_bytes = _CACHE_MAX_SIZE_MB * 1024 * 1024
    # Three files of 40% of the cap each -> 120% total, just over the limit. Evicting one
    # (the least-recently-used) brings it back under, so two must survive.
    chunk = b"x" * (cap_bytes * 2 // 5)
    paths = []
    for i in range(3):
        p = tmp_path / f"f{i}.bin"
        p.write_bytes(chunk)
        paths.append(p)
    # Make f0 the oldest by access time so it is the eviction target.
    import os
    now = datetime.now(UTC).timestamp()
    os.utime(paths[0], (now - 300, now - 300))
    os.utime(paths[1], (now - 100, now - 100))
    os.utime(paths[2], (now, now))

    _evict_stale_cache(tmp_path)

    survivors = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert survivors == ["f1.bin", "f2.bin"]


def test_canonical_search_values_cover_every_clothing_item() -> None:
    values_by_item = {item: canonical_search_values(item) for item in CANONICAL_CLOTHING_ITEMS}

    assert set(values_by_item) == set(CANONICAL_CLOTHING_ITEMS)
    for item, values in values_by_item.items():
        assert values["id"] == f"search-{item}"
        assert values["clothing_item"] == item
        assert values["region"] == "de"
        assert values["enabled"] is False
        assert values["filters"][0]["field"] == "category"


def test_database_rejects_duplicate_clothing_item(monkeypatch, tmp_path) -> None:
    _, factory = _setup(monkeypatch, tmp_path)
    session = factory()
    session.add(Search(id="s2", name="Other", clothing_item="hosen", query="q", region="de"))

    with pytest.raises(IntegrityError):
        session.commit()
    session.close()


def test_database_rejects_deleting_search_with_candidates(monkeypatch, tmp_path) -> None:
    _, factory = _setup(monkeypatch, tmp_path)
    session = factory()
    session.add(_candidate("candidate-1"))
    session.commit()

    with pytest.raises(IntegrityError):
        session.execute(text("DELETE FROM searches WHERE id = 's1'"))
        session.commit()

    session.rollback()
    assert session.get(Candidate, "candidate-1") is not None
    assert session.get(Search, "s1") is not None
    session.close()


def test_update_injects_category_filter_from_clothing_item(monkeypatch, tmp_path) -> None:
    service, _ = _setup(monkeypatch, tmp_path)

    record = service.update(
        "s1",
        SearchUpdate(
            name="Trousers",
            clothing_item="hosen",
            query="cargo",
            region="de",
            enabled=True,
            filters=[],
        ),
    )

    assert record.filters[0].field == "category"
    assert record.filters[0].values == ["hosen", "jeans", "shorts"]
    assert record.name == "Hosen"


def test_update_rejects_changing_clothing_item(monkeypatch, tmp_path) -> None:
    service, _ = _setup(monkeypatch, tmp_path)

    with pytest.raises(SearchClothingItemImmutable):
        service.update(
            "s1",
            SearchUpdate(
                name="Warm tops",
                clothing_item="obenrum_warm",
                query="graphic tee",
                region="de",
                enabled=True,
                filters=[],
            ),
        )


def test_update_rejects_mismatched_category_filter(monkeypatch, tmp_path) -> None:
    service, _ = _setup(monkeypatch, tmp_path)

    with pytest.raises(CategoryFilterError, match="outside Hosen"):
        service.update(
            "s1",
            SearchUpdate(
                name="Bad trousers",
                clothing_item="hosen",
                query="cargo",
                region="de",
                enabled=True,
                filters=[SearchFilter(field="category", label="Category", values=["schuhe"], mode="include")],
            ),
        )


def test_persist_run_sets_created_at_before_queueing_new_alert(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    queued_contract_ids: list[str] = []

    def queue_delivery(session, candidate) -> bool:
        queued_contract_ids.append(candidate_to_contract(candidate).id)
        return True

    service.telegram = type("Telegram", (), {"queue_delivery": staticmethod(queue_delivery)})()
    search_record = SearchRecord(
        id="s1",
        name="S",
        enabled=True,
        clothing_item="hosen",
        query="q",
        region="de",
        filters=[],
        last_run_at=None,
        last_found_count=0,
    )
    raw = {
        "id": "item-1",
        "external_item_id": "item-1",
        "title": "Great cargos",
        "brand": "Brand",
        "price_eur": 25.0,
        "size": "M",
        "url": "https://www.vinted.de/items/item-1",
        "image_urls": [],
        "description": "",
        "features": [],
        "raw_listing": {},
    }
    trace = ScoreTrace(
        final_score=8.0,
        score_10=8,
        threshold=7.0,
        decision="alert",
        components=[],
        summary="alert",
    )

    alert_count, queued_alert_deliveries, judged_count = service._persist_candidate_batch(
        raw_candidates=[raw],
        search_record=search_record,
        taste_profile=TasteProfile(summary="", taste_prompt="", transparency_labels=[]),
        score_traces={"s1:item-1": trace},
        stages={"s1:item-1": "vlm_judged"},
        mode="live",
    )
    session = factory()
    try:
        search = session.get(Search, "s1")
        result = service._persist_run(
            session=session,
            search=search,
            search_record=search_record,
            mode="live",
            fetched_count=1,
            alert_count=alert_count,
            judged_count=judged_count,
            queued_alert_deliveries=queued_alert_deliveries,
        )
        session.commit()
    finally:
        session.close()

    assert result.queued_alert_deliveries == 1
    assert queued_contract_ids == ["s1:item-1"]


def _persist_minimal(service, factory, *, mode):
    search_record = SearchRecord(
        id="s1", name="S", enabled=True, clothing_item="hosen", query="q", region="de",
        filters=[], last_run_at=None, last_found_count=0,
    )
    raw = {
        "id": "item-1", "external_item_id": "item-1", "title": "Cargos", "brand": "Brand",
        "price_eur": 25.0, "size": "M", "url": "https://www.vinted.de/items/item-1",
        "image_urls": [], "description": "", "features": [], "raw_listing": {},
    }
    trace = ScoreTrace(final_score=8.0, score_10=8, threshold=7.0, decision="alert", components=[], summary="alert")
    alert_count, queued_alert_deliveries, judged_count = service._persist_candidate_batch(
        raw_candidates=[raw],
        search_record=search_record,
        taste_profile=TasteProfile(summary="", taste_prompt="", transparency_labels=[]),
        score_traces={"s1:item-1": trace},
        stages={"s1:item-1": "vlm_judged"},
        mode=mode,
    )
    session = factory()
    try:
        search = session.get(Search, "s1")
        service._persist_run(
            session=session, search=search, search_record=search_record, mode=mode,
            fetched_count=1, alert_count=alert_count, judged_count=judged_count,
            queued_alert_deliveries=queued_alert_deliveries,
        )
        session.commit()
    finally:
        session.close()


def test_preview_persist_run_does_not_release_claim(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    service.telegram = type("Telegram", (), {"queue_delivery": staticmethod(lambda *a, **k: True)})()
    # Simulate a live worker scan holding the claim.
    claimed_at = datetime.now(UTC)
    _set(factory, run_status="running", last_claimed_at=claimed_at, last_run_at=None, last_found_count=0)

    _persist_minimal(service, factory, mode="preview")

    row = _row(factory)
    # A concurrent preview must not flip the live worker's claim to completed or touch its stats.
    assert row.run_status == "running"
    assert row.last_run_at is None
    assert row.last_found_count == 0


def test_live_persist_run_releases_claim_and_records_stats(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    service.telegram = type("Telegram", (), {"queue_delivery": staticmethod(lambda *a, **k: True)})()
    _set(factory, run_status="running", last_claimed_at=datetime.now(UTC), last_run_at=None, last_found_count=0)

    _persist_minimal(service, factory, mode="live")

    row = _row(factory)
    assert row.run_status == "completed"
    assert row.last_run_at is not None
    assert row.last_found_count == 1


# --- #3: a failed run only marks run_status="failed" for live runs (which hold the claim) ---


def _wire_run(service, factory, *, run_search):
    """Stub the Phase-1 dependencies _run reads so we can drive it end-to-end with a fake
    Vinted client. run_search is the callable used as VintedClient.run_search."""
    now = datetime.now(UTC)
    with _session_scope_of(factory) as session:
        session.add(
            AiModelConfig(
                id="model-default-judge",
                provider="openai",
                model_name="gpt-5",
                reasoning_effort="low",
                local_base_url=None,
                display_name="gpt-5 (Openai) · low",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(AppSettingsState(id=1, vinted_region="de", judge_model_id="model-default-judge"))

    profile = TasteProfile(summary="", taste_prompt="", transparency_labels=[])
    service.preferences = type(
        "Prefs",
        (),
        {
            "active_taste_profile": staticmethod(lambda *a, **k: profile),
            "get_taste_state": staticmethod(lambda *a, **k: type("S", (), {"manual_note": ""})()),
        },
    )()
    service.vinted_client = type(
        "Vinted",
        (),
        {
            "set_cookie": staticmethod(lambda *a, **k: None),
            "set_refresh_token": staticmethod(lambda *a, **k: None),
            "sync_persisted_credentials": staticmethod(lambda *a, **k: None),
            "get_session_health": staticmethod(lambda *, region, force=False: build_session_health(region=region)),
            "run_search": staticmethod(run_search),
        },
    )()
    service.telegram = type("Telegram", (), {"queue_delivery": staticmethod(lambda *a, **k: True)})()


@contextmanager
def _session_scope_of(factory):
    session = factory()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def _boom(*args, **kwargs):
    raise RuntimeError("vinted exploded")


def test_scan_run_records_usage_totals(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    _wire_run(service, factory, run_search=lambda *args, **kwargs: [_raw_candidate()])
    # _wire_run already seeds "model-default-judge" as an openai/gpt-5 judge model and points
    # judge_model_id at it, matching what this test exercises.

    def fake_judge(raw_candidates, search_record, taste_profile, db_settings, manual_note="", on_usage=None, **kwargs):
        assert on_usage is not None
        on_usage("judge_grid", "gpt-5", 1000, 500, 200)
        return (
            {
                "s1:item-1": ScoreTrace(
                    final_score=8.0,
                    score_10=8,
                    threshold=7.0,
                    decision="alert",
                    summary="alert",
                    model="gpt-5",
                )
            },
            {"s1:item-1": "vlm_judged"},
            1,
            0,
        )

    service._judge_candidates = fake_judge

    result = service._run("s1", mode="preview")
    run = _latest_run(factory)

    assert result.run_id == run.id
    assert run.status == "completed"
    assert run.vinted_status == "ok"
    assert run.vinted_detail == "Fetched 1 candidates from Vinted."
    assert run.input_tokens == 1000
    assert run.output_tokens == 500
    assert run.cost_usd > 0
    assert run.fallback_used is False


def test_scan_run_records_openai_fallback(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    _wire_run(service, factory, run_search=lambda *args, **kwargs: [_raw_candidate()])
    now = datetime.now(UTC)
    with _session_scope_of(factory) as session:
        session.add(
            AiModelConfig(
                id="model-local-judge",
                provider="local",
                model_name="local-model",
                reasoning_effort="low",
                local_base_url="http://127.0.0.1:8080/v1",
                display_name="local-model (Local) · low",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AiModelConfig(
                id="model-openai-fallback",
                provider="openai",
                model_name="gpt-fallback",
                reasoning_effort="low",
                local_base_url=None,
                display_name="gpt-fallback (Openai) · low",
                created_at=now,
                updated_at=now,
            )
        )
        settings = session.get(AppSettingsState, 1)
        settings.judge_model_id = "model-local-judge"
        settings.judge_fallback_model_id = "model-openai-fallback"

    def fake_judge(raw_candidates, search_record, taste_profile, db_settings, manual_note="", on_usage=None, **kwargs):
        return (
            {
                "s1:item-1": ScoreTrace(
                    final_score=9.0,
                    score_10=9,
                    threshold=7.0,
                    decision="alert",
                    summary="fallback alert",
                    model="gpt-fallback",
                )
            },
            {"s1:item-1": "vlm_judged"},
            1,
            0,
        )

    service._judge_candidates = fake_judge

    service._run("s1", mode="preview")
    run = _latest_run(factory)

    assert run.status == "completed"
    assert run.judge_provider == "local"
    assert run.judge_model == "local-model"
    assert run.fallback_used is True


def test_scan_run_records_vinted_session_failure(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)

    def session_failure(*args, **kwargs):
        raise VintedSessionError("Vinted rejected the configured cookie/session.", retryable=False)

    _wire_run(service, factory, run_search=session_failure)

    with pytest.raises(VintedSessionError):
        service._run("s1", mode="preview")

    run = _latest_run(factory)
    assert run.status == "failed"
    assert run.vinted_status == "session_error"
    assert run.vinted_detail == "Vinted rejected the configured cookie/session."
    assert run.error == "Vinted rejected the configured cookie/session."


def test_failed_preview_does_not_touch_a_concurrent_live_claim(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    _wire_run(service, factory, run_search=_boom)
    # A worker is mid-scan, holding the claim.
    _set(factory, run_status="running", last_claimed_at=datetime.now(UTC))

    with pytest.raises(RuntimeError):
        service._run("s1", mode="preview")

    # The preview failed but must leave the worker's claim untouched, or claim_for_run would
    # let a second worker reclaim and re-scan mid-run.
    assert _row(factory).run_status == "running"


def test_failed_live_run_marks_run_status_failed(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    _wire_run(service, factory, run_search=_boom)
    _set(factory, run_status="running", last_claimed_at=datetime.now(UTC))

    with pytest.raises(RuntimeError):
        service._run("s1", mode="live")

    # A live run owns the claim, so its failure must flip run_status to "failed" so the search
    # becomes reclaimable next interval.
    assert _row(factory).run_status == "failed"


# --- #3 (related): re-persisting a candidate upserts rather than IntegrityError-ing ---


def test_persist_run_upserts_and_preserves_feedback(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    service.telegram = type("Telegram", (), {"queue_delivery": staticmethod(lambda *a, **k: True)})()

    _persist_minimal(service, factory, mode="live")

    # Simulate the user having voted on this candidate, plus an existing first_seen_at.
    session = factory()
    candidate = session.get(Candidate, "s1:item-1")
    candidate.feedback = "like"
    original_first_seen = candidate.first_seen_at
    session.commit()
    session.close()

    # A second persist of the same candidate (e.g. a concurrent run) must update, not raise.
    _persist_minimal(service, factory, mode="live")

    session = factory()
    rows = session.query(Candidate).filter(Candidate.id == "s1:item-1").all()
    assert len(rows) == 1
    # feedback and first_seen_at are insert-only / coalesced — the user's vote and the earliest
    # sighting survive the re-persist.
    assert rows[0].feedback == "like"
    assert rows[0].first_seen_at == original_first_seen
    session.close()


# --- cancel: request_cancel() and a mid-scan cancellation keeps partial results ---


def test_request_cancel_is_a_no_op_when_not_running(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    _set(factory, run_status="idle")

    assert service.request_cancel("s1") is False
    assert _row(factory).cancel_requested_at is None


def test_request_cancel_raises_for_unknown_search(monkeypatch, tmp_path) -> None:
    service, _ = _setup(monkeypatch, tmp_path)

    with pytest.raises(KeyError):
        service.request_cancel("missing")


def test_request_cancel_sets_flag_while_running(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    _set(factory, run_status="running", last_claimed_at=datetime.now(UTC))

    assert service.request_cancel("s1") is True
    row = _row(factory)
    # Genuinely in-flight (claim is recent): cooperative flag only, the scan thread clears
    # run_status itself once it notices the flag at its next checkpoint.
    assert row.run_status == "running"
    assert row.cancel_requested_at is not None


def test_request_cancel_force_clears_a_stale_claim(monkeypatch, tmp_path) -> None:
    """Cancelling a claim the dashboard already badges "stale" must not just set a flag that
    nothing is left alive to read — it must immediately release the claim and close out any
    dangling SearchRun rows, exactly like this scenario used to require waiting for the next
    scheduled scan attempt to self-heal via claim_for_run's stale-recovery path."""
    service, factory = _setup(monkeypatch, tmp_path)
    _set(
        factory,
        run_status="running",
        last_claimed_at=datetime.now(UTC) - CLAIM_STALE_THRESHOLD - timedelta(seconds=1),
    )
    with _session_scope_of(factory) as session:
        session.add(
            SearchRun(search_id="s1", mode="live", trigger="worker", status="running", started_at=datetime.now(UTC))
        )

    assert service.request_cancel("s1") is True

    row = _row(factory)
    assert row.run_status == "idle"
    assert row.cancel_requested_at is None

    with _session_scope_of(factory) as session:
        orphaned = session.query(SearchRun).filter_by(search_id="s1").one()
        assert orphaned.status == "failed"
        assert orphaned.finished_at is not None


def test_cancelled_run_persists_partial_results_and_closes_run(monkeypatch, tmp_path) -> None:
    """A user cancels mid-scan: whatever was already judged must be saved as a Candidate row,
    the SearchRun history entry must be closed as "cancelled" (not left "running" forever, which
    is what caused searches to look like they were running more than once), and the search must
    become claimable again immediately (run_status back to "idle", cancel flag cleared)."""
    service, factory = _setup(monkeypatch, tmp_path)
    _wire_run(service, factory, run_search=lambda *args, **kwargs: [_raw_candidate()])
    _set(factory, run_status="running", last_claimed_at=datetime.now(UTC))

    def fake_judge(raw_candidates, search_record, taste_profile, db_settings, mode, manual_note="", on_usage=None, new_judged_ids=None, cancel_check=None, **kwargs):
        trace = ScoreTrace(final_score=8.0, score_10=8, threshold=7.0, decision="alert", summary="alert", model="gpt-5")
        alert_count, queued, _judged = service._persist_candidate_batch(
            raw_candidates=raw_candidates,
            search_record=search_record,
            taste_profile=taste_profile,
            score_traces={"s1:item-1": trace},
            stages={"s1:item-1": "vlm_judged"},
            mode=mode,
        )
        if new_judged_ids is not None:
            new_judged_ids.add("s1:item-1")
        # Simulate the user clicking "Cancel" in the dashboard while this batch was in flight.
        with _session_scope_of(factory) as session:
            session.execute(
                update(Search).where(Search.id == "s1").values(cancel_requested_at=datetime.now(UTC))
            )
        assert cancel_check() is True
        return {"s1:item-1": trace}, {"s1:item-1": "vlm_judged"}, alert_count, queued

    service._judge_candidates = fake_judge

    result = service._run("s1", mode="live")

    assert "cancelled" in result.summary
    assert result.alert_candidates == 1

    run = _latest_run(factory)
    assert run.status == "cancelled"
    assert run.judged_count == 1

    row = _row(factory)
    assert row.run_status == "idle"
    assert row.cancel_requested_at is None

    session = factory()
    candidate = session.get(Candidate, "s1:item-1")
    assert candidate is not None
    assert candidate.decision == "alert"
    session.close()
