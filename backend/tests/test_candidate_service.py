from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sqlalchemy import func, select

from vsniper.core.database import Base
from vsniper.db.models import AiUsageEvent, AlertDeliveryState, Candidate, Search, TasteSampleState
from vsniper.domain.contracts import ReferenceObservation
from vsniper.services.candidate_service import CandidateService


def _setup(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'candidate-service.db'}", future=True)
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

    monkeypatch.setattr("vsniper.services.candidate_service.session_scope", fake_session_scope)
    return CandidateService.__new__(CandidateService), factory


def _candidate(candidate_id: str, *, created_at: datetime) -> Candidate:
    return Candidate(
        id=candidate_id,
        search_id="s1",
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
        score_trace={
            "final_score": 0.8,
            "score_10": 80,
            "threshold": 0.7,
            "decision": "alert",
            "components": [],
            "summary": "Strong match.",
            "raw_response": {},
        },
        decision="alert",
        final_score=8.0,
        ai_observation={},
        grading_stage="vlm_judged",
        feedback="unknown",
        feedback_comment="",
        created_at=created_at,
    )


def test_dashboard_candidates_today_excludes_older_candidates(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    # "Today" is anchored to the user's local (Europe/Berlin) calendar day, then queried in UTC.
    local_now = datetime.now(ZoneInfo("Europe/Berlin"))
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    with factory() as session:
        session.add(Search(id="s1", name="Search", clothing_item="hosen", query="q", region="de", enabled=True))
        session.add(_candidate("old", created_at=today_start - timedelta(seconds=1)))
        session.add(_candidate("today", created_at=today_start + timedelta(seconds=1)))
        session.commit()

    stats = service.get_dashboard_stats()

    assert stats.active_searches == 1
    assert stats.candidates_today == 1


def test_score_distribution_bins_by_final_score_and_respects_window(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    now = datetime.now(UTC)

    with factory() as session:
        session.add(Search(id="s1", name="Search", clothing_item="hosen", query="q", region="de", enabled=True))
        recent_scored = _candidate("recent-high", created_at=now - timedelta(hours=1))
        recent_scored.final_score = 0.85  # falls in the 81-90 bin
        old_scored = _candidate("old-mid", created_at=now - timedelta(days=10))
        old_scored.final_score = 0.55  # falls in the 51-60 bin, outside the 7d window
        failed = _candidate("failed", created_at=now - timedelta(hours=1))
        failed.final_score = 0.0
        failed.grading_stage = "failed"
        session.add_all([recent_scored, old_scored, failed])
        session.commit()

    distribution_7d = service.get_score_distribution("7d")
    assert distribution_7d.total_count == 1
    high_bin = next(b for b in distribution_7d.bins if b.min_score <= 85 <= b.max_score)
    assert high_bin.count == 1
    assert high_bin.percentage == 100.0

    distribution_all = service.get_score_distribution("all")
    assert distribution_all.total_count == 2
    mid_bin = next(b for b in distribution_all.bins if b.min_score <= 55 <= b.max_score)
    assert mid_bin.count == 1
    assert mid_bin.percentage == 50.0


def test_candidate_page_defaults_to_recent_unreviewed_candidates_sorted_by_score(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    now = datetime.now(UTC)

    with factory() as session:
        session.add(Search(id="s1", name="Search", clothing_item="hosen", query="q", region="de", enabled=True))
        lower_score = _candidate("recent-low", created_at=now - timedelta(hours=1))
        lower_score.final_score = 0.6
        higher_score = _candidate("recent-high", created_at=now - timedelta(hours=2))
        higher_score.final_score = 0.9
        older = _candidate("older", created_at=now - timedelta(days=8))
        older.final_score = 1.0
        session.add_all([lower_score, higher_score, older])
        session.commit()

    page = service.page(feedback="unknown")

    assert page.total == 2
    assert [candidate.id for candidate in page.items] == ["recent-high", "recent-low"]


def test_feedback_observation_is_applied_before_taste_sample_upsert(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    seen_observation: dict = {}

    class PreferencesRecorder:
        def existing_feedback_image_paths(self, session, *, candidate_id):
            return []

        def precache_feedback_images(self, *, candidate_id, image_urls):
            return []

        def upsert_candidate_feedback_sample(
            self, session, *, candidate, verdict, comment, stored_image_paths=None
        ):
            seen_observation.update(candidate.ai_observation or {})

    service.preferences = PreferencesRecorder()
    service.settings = SimpleNamespace()

    observation_payload = ReferenceObservation(
        image_id="candidate",
        file_name="candidate.jpg",
        clothing_item="hosen",
        garment_type="cargo trousers",
    ).model_dump(mode="json")

    # apply_feedback computes the observation in its network phase (no DB txn held) and applies
    # it in the write phase before the taste sample is upserted. Stub the settings lookup +
    # network call so the test does not need a real settings row or VLM call.
    monkeypatch.setattr(CandidateService, "_get_settings_state", staticmethod(lambda candidate_model: object()))
    monkeypatch.setattr(service, "_observation_settings", lambda session, db_settings: object())
    monkeypatch.setattr(
        service, "_compute_candidate_observation", lambda candidate_model, obs_settings: observation_payload
    )

    with factory() as session:
        session.add(Search(id="s1", name="Search", clothing_item="hosen", query="q", region="de", enabled=True))
        session.add(_candidate("candidate-1", created_at=datetime.now(UTC)))
        session.commit()

    service.apply_feedback("candidate-1", verdict="like")

    assert seen_observation["garment_type"] == "cargo trousers"


def test_feedback_comment_can_be_cleared(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    seen_comment: list[str] = []

    class PreferencesRecorder:
        def existing_feedback_image_paths(self, session, *, candidate_id):
            return []

        def precache_feedback_images(self, *, candidate_id, image_urls):
            return []

        def upsert_candidate_feedback_sample(
            self, session, *, candidate, verdict, comment, stored_image_paths=None
        ):
            seen_comment.append(comment)

    service.preferences = PreferencesRecorder()
    service.settings = SimpleNamespace()

    with factory() as session:
        session.add(Search(id="s1", name="Search", clothing_item="hosen", query="q", region="de", enabled=True))
        candidate = _candidate("candidate-1", created_at=datetime.now(UTC))
        candidate.feedback = "like"
        candidate.feedback_comment = "old comment"
        candidate.ai_observation = {"garment_type": "trousers"}
        session.add(candidate)
        session.commit()

    updated, snapshot = service.apply_feedback("candidate-1", verdict="like", comment="")

    assert snapshot is not None
    assert updated.feedback_comment == ""
    assert seen_comment == [""]


def test_feedback_idempotency_requires_same_comment(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)

    class PreferencesRecorder:
        def existing_feedback_image_paths(self, session, *, candidate_id):
            return []

        def precache_feedback_images(self, *, candidate_id, image_urls):
            return []

        def upsert_candidate_feedback_sample(
            self, session, *, candidate, verdict, comment, stored_image_paths=None
        ):
            return None

    service.preferences = PreferencesRecorder()
    service.settings = SimpleNamespace()

    with factory() as session:
        session.add(Search(id="s1", name="Search", clothing_item="hosen", query="q", region="de", enabled=True))
        candidate = _candidate("candidate-1", created_at=datetime.now(UTC))
        candidate.feedback = "like"
        candidate.feedback_comment = "same"
        candidate.ai_observation = {"garment_type": "trousers"}
        session.add(candidate)
        session.commit()

    unchanged, skipped_snapshot = service.apply_feedback(
        "candidate-1",
        verdict="like",
        comment="same",
        skip_if_unchanged=True,
    )
    changed, changed_snapshot = service.apply_feedback(
        "candidate-1",
        verdict="like",
        comment="changed",
        skip_if_unchanged=True,
    )

    assert unchanged.feedback_comment == "same"
    assert skipped_snapshot is None
    assert changed.feedback_comment == "changed"
    assert changed_snapshot is not None


def test_note_only_feedback_keeps_unknown_verdict_and_upserts_sample(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    seen: dict = {}

    class PreferencesRecorder:
        def existing_feedback_image_paths(self, session, *, candidate_id):
            return []

        def precache_feedback_images(self, *, candidate_id, image_urls):
            return ["candidate-1-0.jpg"]

        def upsert_candidate_feedback_sample(
            self, session, *, candidate, verdict, comment, stored_image_paths=None
        ):
            seen.update(
                {
                    "feedback": candidate.feedback,
                    "verdict": verdict,
                    "comment": comment,
                    "stored_image_paths": stored_image_paths,
                }
            )
            session.add(
                TasteSampleState(
                    id="taste-note",
                    kind="offer_note",
                    clothing_item=candidate.clothing_item,
                    note=comment,
                    candidate_id=candidate.id,
                    stored_image_paths=stored_image_paths or [],
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )

    service.preferences = PreferencesRecorder()
    service.settings = SimpleNamespace()

    with factory() as session:
        session.add(Search(id="s1", name="Search", clothing_item="hosen", query="q", region="de", enabled=True))
        candidate = _candidate("candidate-1", created_at=datetime.now(UTC))
        candidate.ai_observation = {"garment_type": "trousers"}
        session.add(candidate)
        session.commit()

    updated, snapshot = service.apply_note("candidate-1", comment="great texture")

    assert updated.feedback == "unknown"
    assert updated.feedback_comment == "great texture"
    assert snapshot is not None
    assert seen == {
        "feedback": "unknown",
        "verdict": None,
        "comment": "great texture",
        "stored_image_paths": ["candidate-1-0.jpg"],
    }


def test_prune_old_records_drops_aged_rows_and_keeps_pending(monkeypatch, tmp_path) -> None:
    service, factory = _setup(monkeypatch, tmp_path)
    service.settings = SimpleNamespace(
        candidate_retention_days=365,
        delivery_retention_days=365,
        ai_usage_retention_days=365,
    )
    now = datetime.now(UTC)
    old = now - timedelta(days=400)

    def _usage(called_at: datetime) -> AiUsageEvent:
        return AiUsageEvent(
            called_at=called_at, operation="judge_grid", model="m",
            input_tokens=1, output_tokens=1, cost_usd=0.1,
        )

    with factory() as session:
        session.add(Search(id="s1", name="Search", clothing_item="hosen", query="q", region="de", enabled=True))
        session.add(_candidate("recent", created_at=now))
        session.add(_candidate("old", created_at=old))  # pruned (aged out)
        session.add(_candidate("recent-terminal", created_at=now))
        session.add(_candidate("recent-pending", created_at=now))
        session.add(_usage(now))
        session.add(_usage(old))  # pruned (aged out)
        # One active delivery per candidate (unique index on candidate+channel).
        session.add(AlertDeliveryState(candidate_id="old", channel="telegram", status="sent", updated_at=now))
        session.add(AlertDeliveryState(candidate_id="recent-terminal", channel="telegram", status="sent", updated_at=old))
        session.add(AlertDeliveryState(candidate_id="recent-pending", channel="telegram", status="pending", updated_at=old))
        session.commit()

    counts = service.prune_old_records()

    # 'recent-terminal' (terminal + aged) and 'old' (cascade with its candidate) are removed;
    # the aged 'recent-pending' survives because pending deliveries are never pruned.
    assert counts == {"candidates": 1, "alert_deliveries": 2, "ai_usage_events": 1, "learning_snapshots": 0}

    with factory() as session:
        assert session.get(Candidate, "old") is None
        assert session.get(Candidate, "recent") is not None
        assert session.scalar(select(func.count()).select_from(AiUsageEvent)) == 1
        surviving = {d.candidate_id: d.status for d in session.scalars(select(AlertDeliveryState)).all()}
        assert surviving == {"recent-pending": "pending"}
