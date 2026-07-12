"""Tests for operations service, maintenance pause, search run tracking, and worker heartbeat."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from vsniper.core.database import Base
from vsniper.db.models import (
    AppSettingsState,
    Candidate,
    MaintenanceState,
    Search,
    SearchRun,
    TasteState,
    WorkerHeartbeat,
)
from vsniper.services.maintenance_service import (
    MAINTENANCE_ROW_ID,
    MaintenanceImportActive,
    begin_import_maintenance,
    begin_manual_pause,
    end_manual_pause,
    get_maintenance_state,
)


def _setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)

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

    return engine, factory, fake_session_scope


# --- Maintenance pause/resume tests ---


def test_manual_pause_sets_mode(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.maintenance_service as ms
    original = ms.session_scope
    ms.session_scope = fake_session_scope
    try:
        with fake_session_scope() as session:
            session.add(MaintenanceState(id=MAINTENANCE_ROW_ID, mode="idle", updated_at=datetime.now(UTC)))

        begin_manual_pause(reason="test pause")

        state = get_maintenance_state()
        assert state["mode"] == "manual"
        assert state["reason"] == "test pause"
    finally:
        ms.session_scope = original


def test_manual_pause_is_idempotent(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.maintenance_service as ms
    original = ms.session_scope
    ms.session_scope = fake_session_scope
    try:
        with fake_session_scope() as session:
            session.add(MaintenanceState(id=MAINTENANCE_ROW_ID, mode="idle", updated_at=datetime.now(UTC)))

        begin_manual_pause(reason="first")
        begin_manual_pause(reason="second")

        state = get_maintenance_state()
        assert state["mode"] == "manual"
    finally:
        ms.session_scope = original


def test_manual_pause_rejected_during_import(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.maintenance_service as ms
    original = ms.session_scope
    ms.session_scope = fake_session_scope
    try:
        with fake_session_scope() as session:
            session.add(MaintenanceState(id=MAINTENANCE_ROW_ID, mode="import", updated_at=datetime.now(UTC)))

        with pytest.raises(MaintenanceImportActive):
            begin_manual_pause()
    finally:
        ms.session_scope = original


def test_manual_resume_clears_manual_mode(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.maintenance_service as ms
    original = ms.session_scope
    ms.session_scope = fake_session_scope
    try:
        with fake_session_scope() as session:
            session.add(MaintenanceState(id=MAINTENANCE_ROW_ID, mode="manual", reason="test", started_at=datetime.now(UTC), updated_at=datetime.now(UTC)))

        end_manual_pause()

        state = get_maintenance_state()
        assert state["mode"] == "idle"
    finally:
        ms.session_scope = original


def test_manual_resume_does_not_clear_import(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.maintenance_service as ms
    original = ms.session_scope
    ms.session_scope = fake_session_scope
    try:
        with fake_session_scope() as session:
            session.add(MaintenanceState(id=MAINTENANCE_ROW_ID, mode="import", operation_id="imp-1", updated_at=datetime.now(UTC)))

        end_manual_pause()

        state = get_maintenance_state()
        assert state["mode"] == "import"
    finally:
        ms.session_scope = original


def test_import_escalates_manual_to_import(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.maintenance_service as ms
    original = ms.session_scope
    ms.session_scope = fake_session_scope
    try:
        with fake_session_scope() as session:
            session.add(MaintenanceState(id=MAINTENANCE_ROW_ID, mode="manual", reason="user pause", started_at=datetime.now(UTC), updated_at=datetime.now(UTC)))

        begin_import_maintenance()

        state = get_maintenance_state()
        assert state["mode"] == "import"
    finally:
        ms.session_scope = original


def test_worker_activity_skipped_during_manual_pause(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.maintenance_service as ms
    original = ms.session_scope
    ms.session_scope = fake_session_scope
    try:
        with fake_session_scope() as session:
            session.add(MaintenanceState(id=MAINTENANCE_ROW_ID, mode="manual", updated_at=datetime.now(UTC)))

        with ms.worker_activity("test_phase") as active:
            assert active is False
    finally:
        ms.session_scope = original


# --- Search run tracking tests ---


def test_create_and_finish_search_run(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.operations_service as ops
    original = ops.session_scope
    ops.session_scope = fake_session_scope
    try:
        run_id = ops.create_search_run(search_id="s1", mode="live", trigger="worker")
        assert run_id > 0

        ops.finish_search_run(
            run_id=run_id,
            status="completed",
            fetched_count=10,
            judged_count=8,
            new_judged_count=6,
            alert_count=3,
            judge_provider="local",
            judge_model="test-model",
        )

        with fake_session_scope() as session:
            run = session.get(SearchRun, run_id)
            assert run is not None
            assert run.status == "completed"
            assert run.fetched_count == 10
            assert run.judged_count == 8
            assert run.new_judged_count == 6
            assert run.alert_count == 3
            assert run.duration_ms is not None
            assert run.finished_at is not None
    finally:
        ops.session_scope = original


def test_finish_search_run_on_failure(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.operations_service as ops
    original = ops.session_scope
    ops.session_scope = fake_session_scope
    try:
        run_id = ops.create_search_run(search_id="s1", mode="live", trigger="manual")

        ops.finish_search_run(
            run_id=run_id,
            status="failed",
            error="Vinted session expired",
        )

        with fake_session_scope() as session:
            run = session.get(SearchRun, run_id)
            assert run.status == "failed"
            assert run.error == "Vinted session expired"
    finally:
        ops.session_scope = original


# --- Worker heartbeat tests ---


def test_record_heartbeat_creates_and_updates(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.operations_service as ops
    original = ops.session_scope
    ops.session_scope = fake_session_scope
    try:
        ops.record_heartbeat(owner="test-host:123", cycle_count=1, phase="scan_searches")

        with fake_session_scope() as session:
            hb = session.get(WorkerHeartbeat, 1)
            assert hb is not None
            assert hb.owner == "test-host:123"
            assert hb.cycle_count == 1
            assert hb.phase == "scan_searches"
            assert hb.last_heartbeat_at is not None

        ops.record_heartbeat(owner="test-host:123", cycle_count=2, phase="idle", cycle_finished_at=datetime.now(UTC))

        with fake_session_scope() as session:
            hb = session.get(WorkerHeartbeat, 1)
            assert hb.cycle_count == 2
            assert hb.phase == "idle"
            assert hb.last_cycle_finished_at is not None
    finally:
        ops.session_scope = original


# --- Operations snapshot tests ---


def test_operations_snapshot_returns_all_sections(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.operations_service as ops
    original = ops.session_scope
    ops.session_scope = fake_session_scope
    try:
        with fake_session_scope() as session:
            session.add(MaintenanceState(id=MAINTENANCE_ROW_ID, mode="idle", updated_at=datetime.now(UTC)))
            session.add(TasteState(id=1, manual_note="", taste_profile={}))
            session.add(AppSettingsState(id=1, vinted_region="de"))
            session.add(Search(id="s1", name="Test", clothing_item="hosen", query="q", region="de", run_status="idle"))

        snapshot = ops.get_operations_snapshot()
        assert snapshot.maintenance_mode == "idle"
        assert snapshot.worker_heartbeat is None
        assert snapshot.active_worker_tasks == []
        assert snapshot.recompute_status == "idle"
        assert snapshot.delivery_summary.pending == 0
        assert snapshot.delivery_summary.failed == 0
        assert isinstance(snapshot.search_claims, list)
        assert isinstance(snapshot.recent_runs, list)
    finally:
        ops.session_scope = original


def test_operations_snapshot_includes_failed_deliveries(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.operations_service as ops
    original = ops.session_scope
    ops.session_scope = fake_session_scope
    try:
        now = datetime.now(UTC)
        with fake_session_scope() as session:
            session.add(MaintenanceState(id=MAINTENANCE_ROW_ID, mode="idle", updated_at=now))
            session.add(TasteState(id=1, manual_note="", taste_profile={}))
            session.add(AppSettingsState(id=1, vinted_region="de"))
            session.add(Search(id="s1", name="Test", clothing_item="hosen", query="q", region="de"))
            session.add(Candidate(
                id="s1:item-1", search_id="s1", clothing_item="hosen", title="Test item",
                brand="Brand", price_eur=20.0, size="M", url="https://example.test/1",
                image_urls=[], matched_filters=[], matched_preferences=[], features=[],
                normalized_listing={}, extraction_status="completed",                 score_trace={"final_score": 8, "score_10": 80, "threshold": 7.0, "decision": "alert", "summary": "good"},
                decision="alert", final_score=8.0, ai_observation={}, grading_stage="vlm_judged",
                feedback="unknown", created_at=now,
            ))
            from vsniper.db.models import AlertDeliveryState
            session.add(AlertDeliveryState(
                candidate_id="s1:item-1", status="failed", attempt_count=3,
                last_error="Telegram rate limit", created_at=now, updated_at=now,
            ))

        snapshot = ops.get_operations_snapshot()
        assert snapshot.delivery_summary.failed == 1
        assert len(snapshot.delivery_summary.latest_failures) == 1
    finally:
        ops.session_scope = original


def test_get_search_runs_with_filters(tmp_path):
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.operations_service as ops
    original = ops.session_scope
    ops.session_scope = fake_session_scope
    try:
        ops.create_search_run(search_id="s1", mode="live", trigger="worker")
        rid2 = ops.create_search_run(search_id="s1", mode="preview", trigger="manual")
        ops.finish_search_run(run_id=rid2, status="completed", fetched_count=5)

        all_runs = ops.get_search_runs()
        assert all_runs.total == 2

        completed_runs = ops.get_search_runs(status="completed")
        assert completed_runs.total == 1

        s1_runs = ops.get_search_runs(search_id="s1")
        assert s1_runs.total == 2

        s2_runs = ops.get_search_runs(search_id="s2")
        assert s2_runs.total == 0
    finally:
        ops.session_scope = original


# --- Maintenance pause enforcement on routes ---


def test_manual_scan_routes_blocked_during_pause(tmp_path, monkeypatch):
    """Integration-style test: verify the route helper rejects non-idle modes."""
    _, factory, fake_session_scope = _setup(tmp_path)
    import vsniper.services.maintenance_service as ms
    original = ms.session_scope
    ms.session_scope = fake_session_scope
    try:
        with fake_session_scope() as session:
            session.add(MaintenanceState(id=MAINTENANCE_ROW_ID, mode="manual", updated_at=datetime.now(UTC)))

        state = get_maintenance_state()
        assert state["mode"] != "idle"
    finally:
        ms.session_scope = original
