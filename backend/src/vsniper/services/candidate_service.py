from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, object_session, selectinload

from vsniper.core.config import Settings
from vsniper.core.database import session_scope
from vsniper.db.models import AiUsageEvent, AlertDeliveryState, AppSettingsState, Candidate, LearningSnapshotState, Search
from vsniper.domain.contracts import (
    AiCategoryStats,
    AiCostStats,
    CandidatePage,
    CandidateRecord,
    ClothingItem,
    DashboardStats,
    FeedbackPayload,
    LearningSnapshot,
    ScoreDistribution,
    ScoreDistributionBin,
)
from vsniper.integrations.openai.client import OpenAIIntegrationError, OpenAITasteClient
from vsniper.services._mapping import (
    candidate_to_contract,
    extract_condition,
    resolve_ai_model,
)
from vsniper.services.taste_service import TasteService

logger = logging.getLogger(__name__)

# The app serves a single German user; "today" on the dashboard means the local calendar day.
_LOCAL_TZ = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True)
class _ObservationSettings:
    """Primitive snapshot of the settings needed to run a candidate observation, captured in a
    short read transaction so the (slow) VLM call can run without holding a DB session open."""

    provider: str
    model: str
    reasoning_effort: str
    image_detail: str
    local_base_url: str | None


class CandidateService:
    def __init__(
        self,
        settings: Settings,
        preferences: TasteService,
        taste_client: OpenAITasteClient,
    ) -> None:
        self.settings = settings
        self.preferences = preferences
        self.taste_client = taste_client

    def _candidate_image_cache_path(self, candidate_id: str) -> Path:
        cache_dir = self.settings.resolve_path(self.settings.cache_dir) / "candidate-images"
        safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in candidate_id)
        return cache_dir / f"{safe_name}.jpg"

    def _load_candidate_image_bytes(self, candidate_model: Candidate) -> tuple[bytes, str] | None:
        cache_path = self._candidate_image_cache_path(candidate_model.id)
        if cache_path.exists():
            return cache_path.read_bytes(), "image/jpeg"
        if not candidate_model.image_urls:
            return None
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                response = client.get(
                    candidate_model.image_urls[0],
                    headers={"User-Agent": "vsniper/0.1 (+https://local.vsniper)"},
                )
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        content_type = response.headers.get("content-type", "image/jpeg").split(";", maxsplit=1)[0]
        if not content_type.startswith("image/"):
            return None
        try:
            with Image.open(BytesIO(response.content)) as img:
                img.verify()
        except (UnidentifiedImageError, OSError):
            return None
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(response.content)
        return response.content, content_type

    def _observation_settings(self, session: Session, db_settings: AppSettingsState) -> _ObservationSettings | None:
        """Snapshot the registry-resolved observation model into plain values, callable while the
        settings row is still attached to a session. Returns None if no observation model is
        configured (registry row missing/unset) — caller skips the observation in that case."""
        resolved = resolve_ai_model(session, db_settings.observation_model_id)
        if resolved is None:
            return None
        return _ObservationSettings(
            provider=resolved.provider,
            model=resolved.model_name,
            reasoning_effort=resolved.reasoning_effort,
            image_detail=self.settings.ai_learn_image_detail,
            local_base_url=resolved.local_base_url,
        )

    def _compute_candidate_observation(
        self, candidate_model: Candidate, obs_settings: _ObservationSettings
    ) -> dict | None:
        """Run a structured observation on the candidate image, returning the JSON payload.

        Network + VLM only — must run *outside* any DB transaction so the SQLite write lock is
        not held across the (slow) call. `candidate_model` may be detached (its columns were
        eagerly loaded; `expire_on_commit=False` keeps them readable). Best-effort: failures are
        logged and return None instead of blocking feedback recording."""
        if candidate_model.ai_observation:
            return None
        image = self._load_candidate_image_bytes(candidate_model)
        if image is None:
            return None
        image_bytes, mime_type = image
        norm = candidate_model.normalized_listing or {}
        condition = extract_condition(norm.get("raw_listing") or {})
        try:
            observation = self.taste_client.describe_candidate_image(
                image_bytes=image_bytes,
                mime_type=mime_type,
                title=candidate_model.title,
                brand=candidate_model.brand,
                size=candidate_model.size,
                condition=condition,
                description=str(norm.get("description") or ""),
                clothing_item=cast(ClothingItem, candidate_model.clothing_item),
                provider=obs_settings.provider,
                model=obs_settings.model,
                reasoning_effort=obs_settings.reasoning_effort,
                image_detail=obs_settings.image_detail,
                local_base_url=obs_settings.local_base_url,
            )
        except OpenAIIntegrationError:
            logger.warning("Failed to describe candidate %s for learning evidence.", candidate_model.id, exc_info=True)
            return None
        return observation.model_dump(mode="json")

    @staticmethod
    def _get_settings_state(candidate_model: Candidate) -> AppSettingsState:
        session = object_session(candidate_model)
        if session is None:
            raise RuntimeError("Candidate is not attached to a database session")
        model = session.get(AppSettingsState, 1)
        if model is None:
            raise RuntimeError("Settings row is missing from SQLite storage")
        return model

    # Sort options exposed to the API; maps to (column, descending).
    _SORT_OPTIONS = {
        "newest": (Candidate.created_at, True),
        "oldest": (Candidate.created_at, False),
        "price_asc": (Candidate.price_eur, False),
        "price_desc": (Candidate.price_eur, True),
        "score_desc": (Candidate.final_score, True),
        "score_asc": (Candidate.final_score, False),
    }

    _CANDIDATE_PAGE_WINDOWS = {
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "12h": timedelta(hours=12),
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
    }

    def page(
        self,
        *,
        clothing_item: str | None = None,
        stage: str | None = None,
        decision: str | None = None,
        feedback: str | None = None,
        delivery_status: str | None = None,
        window: str = "7d",
        sort: str = "score_desc",
        limit: int = 50,
        offset: int = 0,
    ) -> CandidatePage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        sort_column, descending = self._SORT_OPTIONS.get(sort, self._SORT_OPTIONS["score_desc"])
        order = sort_column.desc() if descending else sort_column.asc()
        with session_scope() as session:
            # Filters shared by the page query and its matching total count.
            filters = []
            tab_filters = []
            if clothing_item and clothing_item != "all":
                tab_filters.append(Candidate.clothing_item == clothing_item)
            filters.extend(tab_filters)
            if stage and stage != "all":
                filters.append(Candidate.grading_stage == stage)
            if decision and decision != "all":
                filters.append(Candidate.decision == decision)
            if feedback and feedback != "all":
                filters.append(Candidate.feedback == feedback)
            cutoff = datetime.now(UTC) - self._CANDIDATE_PAGE_WINDOWS.get(
                window, self._CANDIDATE_PAGE_WINDOWS["7d"]
            )
            filters.append(Candidate.created_at >= cutoff)
            if delivery_status and delivery_status != "all":
                filters.append(
                    Candidate.id.in_(
                        select(AlertDeliveryState.candidate_id).where(AlertDeliveryState.status == delivery_status)
                    )
                )

            query = (
                select(Candidate)
                .options(selectinload(Candidate.alert_deliveries), selectinload(Candidate.search))
                .where(*filters)
                # created_at as a stable tiebreaker keeps pagination deterministic
                # when the primary sort column has ties (e.g. equal prices).
                .order_by(order, Candidate.created_at.desc())
            )
            records = session.scalars(query.limit(limit).offset(offset)).all()

            stage_counts: dict[str, int] = {}
            for stage_value, count in session.execute(
                select(Candidate.grading_stage, func.count()).where(*tab_filters).group_by(Candidate.grading_stage)
            ).all():
                stage_counts[stage_value] = int(count)
            stage_counts["all"] = sum(stage_counts.values())

            item_counts: dict[str, int] = {}
            for item_value, count in session.execute(
                select(Candidate.clothing_item, func.count()).group_by(Candidate.clothing_item)
            ).all():
                item_counts[item_value] = int(count)
            item_counts["all"] = sum(item_counts.values())

            # total reflects the active filter set (stage counts stay unfiltered, for
            # the stage buttons), so it can't be read from stage_counts once decision
            # or feedback filters are applied.
            total = session.scalar(select(func.count()).select_from(Candidate).where(*filters)) or 0
            return CandidatePage(
                items=[candidate_to_contract(item) for item in records],
                total=int(total),
                stage_counts=stage_counts,
                item_counts=item_counts,
            )

    def record_feedback(self, candidate_id: str, payload: FeedbackPayload) -> CandidateRecord:
        if payload.verdict is None:
            if not payload.comment.strip():
                raise ValueError("A verdict or non-empty comment is required.")
            candidate_record, _ = self.apply_note(
                candidate_id,
                comment=payload.comment,
                skip_if_unchanged=payload.skip_if_unchanged,
            )
        else:
            candidate_record, _ = self.apply_feedback(
                candidate_id,
                verdict=payload.verdict,
                comment=payload.comment,
                skip_if_unchanged=payload.skip_if_unchanged,
            )
        return candidate_record

    def apply_feedback(
        self,
        candidate_id: str,
        *,
        verdict: Literal["like", "dislike"],
        comment: str | None = None,
        skip_if_unchanged: bool = False,
    ) -> tuple[CandidateRecord, LearningSnapshot | None]:
        return self._apply_learning_signal(
            candidate_id,
            verdict=verdict,
            comment=comment,
            skip_if_unchanged=skip_if_unchanged,
        )

    def apply_note(
        self,
        candidate_id: str,
        *,
        comment: str,
        skip_if_unchanged: bool = False,
    ) -> tuple[CandidateRecord, LearningSnapshot | None]:
        return self._apply_learning_signal(
            candidate_id,
            verdict=None,
            comment=comment,
            skip_if_unchanged=skip_if_unchanged,
        )

    def _apply_learning_signal(
        self,
        candidate_id: str,
        *,
        verdict: Literal["like", "dislike"] | None,
        comment: str | None = None,
        skip_if_unchanged: bool = False,
    ) -> tuple[CandidateRecord, LearningSnapshot | None]:
        """Record feedback/note using the scan path's three-phase shape so image download + VLM
        observation never run while a DB write transaction (SQLite write lock) is held."""
        # Phase 1 — short read txn: decide whether an observation is needed and snapshot the
        # settings it requires. expire_on_commit=False keeps the loaded candidate columns
        # readable after the session closes, so the VLM call below can use them while detached.
        with session_scope() as session:
            candidate_model = session.get(Candidate, candidate_id)
            if candidate_model is None:
                raise KeyError(candidate_id)
            cleaned_comment = comment.strip() if comment is not None else None
            current_comment = (candidate_model.feedback_comment or "").strip()
            same_verdict = verdict is None or candidate_model.feedback == verdict
            same_comment = cleaned_comment is None or cleaned_comment == current_comment
            if skip_if_unchanged and same_verdict and same_comment:
                return candidate_to_contract(candidate_model), None
            obs_settings = (
                self._observation_settings(session, self._get_settings_state(candidate_model))
                if not candidate_model.ai_observation
                else None
            )
            candidate_image_urls = candidate_model.image_urls or []
            already_cached = self.preferences.existing_feedback_image_paths(
                session, candidate_id=candidate_id
            )

        # Phase 2 — network/VLM, no DB transaction held.
        observation = (
            self._compute_candidate_observation(candidate_model, obs_settings)
            if obs_settings is not None
            else None
        )
        # Download the feedback-sample images here, outside the Phase 3 write txn, so the durable
        # asset download (up to 6 × 20s) never holds a SQLite write lock (would risk BUSY_SNAPSHOT).
        # Skip if the sample already has durable paths (re-recording feedback on the same candidate).
        stored_image_paths = already_cached or self.preferences.precache_feedback_images(
            candidate_id=candidate_id, image_urls=candidate_image_urls
        )

        # Phase 3 — short write txn: persist feedback + the precomputed observation.
        with session_scope() as session:
            candidate_model = session.get(Candidate, candidate_id)
            if candidate_model is None:
                raise KeyError(candidate_id)
            return self.record_feedback_in_session(
                session,
                candidate_model=candidate_model,
                verdict=verdict,
                comment=comment,
                skip_if_unchanged=skip_if_unchanged,
                observation=observation,
                stored_image_paths=stored_image_paths,
            )

    def record_feedback_in_session(
        self,
        session: Session,
        *,
        candidate_model: Candidate,
        verdict: Literal["like", "dislike"] | None,
        comment: str | None = None,
        skip_if_unchanged: bool = False,
        observation: dict | None = None,
        stored_image_paths: list[str] | None = None,
    ) -> tuple[CandidateRecord, LearningSnapshot | None]:
        cleaned_comment = comment.strip() if comment is not None else None
        current_comment = (candidate_model.feedback_comment or "").strip()
        same_verdict = verdict is None or candidate_model.feedback == verdict
        same_comment = cleaned_comment is None or cleaned_comment == current_comment
        if skip_if_unchanged and same_verdict and same_comment:
            session.flush()
            session.refresh(candidate_model)
            return candidate_to_contract(candidate_model), None

        previous_feedback = candidate_model.feedback
        if verdict is not None:
            candidate_model.feedback = verdict
        if cleaned_comment is not None:
            candidate_model.feedback_comment = cleaned_comment
        # The observation was computed outside this transaction; apply it only if we still lack
        # one (another process may have filled it in between phases).
        if observation is not None and not candidate_model.ai_observation:
            candidate_model.ai_observation = observation
        self.preferences.upsert_candidate_feedback_sample(
            session,
            candidate=candidate_model,
            verdict=verdict,
            comment=candidate_model.feedback_comment or "",
            stored_image_paths=stored_image_paths,
        )
        signal = verdict if verdict is not None else "note"
        snapshot = LearningSnapshot(
            id=f"learn-{uuid4().hex[:8]}",
            created_at=datetime.now(UTC),
            reason=(
                f"Recorded {signal} feedback for candidate {candidate_model.id}. "
                "The taste prompt will incorporate it on the next preference refresh."
            ),
            summary=(
                f"Feedback changed from {previous_feedback} to {verdict}."
                if verdict is not None
                else "Stored note-only candidate feedback."
            ),
            source_counts={"feedback_events": 1},
        )
        session.add(
            LearningSnapshotState(
                id=snapshot.id,
                created_at=snapshot.created_at,
                reason=snapshot.reason,
                changed_weights=[],
                summary=snapshot.summary,
                old_prompt=snapshot.old_prompt,
                new_prompt=snapshot.new_prompt,
                source_counts=snapshot.source_counts,
            )
        )

        session.flush()
        session.refresh(candidate_model)
        return candidate_to_contract(candidate_model), snapshot

    def get_dashboard_stats(self) -> DashboardStats:
        # Anchor "today" to the user's local day (Europe/Berlin, DST-aware), then query in UTC.
        # Using UTC midnight would shift the boundary 1-2h and miscount around midnight.
        local_now = datetime.now(_LOCAL_TZ)
        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        with session_scope() as session:
            active_searches = session.scalar(
                select(func.count()).where(Search.enabled.is_(True))
            ) or 0
            candidates_today = session.scalar(
                select(func.count()).where(Candidate.created_at >= today_start)
            ) or 0
            likes = session.scalar(
                select(func.count()).where(Candidate.feedback == "like")
            ) or 0
            dislikes = session.scalar(
                select(func.count()).where(Candidate.feedback == "dislike")
            ) or 0
            avg_alert_score_raw = session.scalar(
                select(func.avg(Candidate.final_score)).where(Candidate.decision == "alert")
            )
            pending_deliveries = session.scalar(
                select(func.count()).where(AlertDeliveryState.status == "pending")
            ) or 0
            failed_deliveries = session.scalar(
                select(func.count()).where(AlertDeliveryState.status == "failed")
            ) or 0
            last_successful_scan_at = session.scalar(
                select(func.max(Search.last_run_at)).where(Search.last_run_at.isnot(None))
            )
            return DashboardStats(
                active_searches=active_searches,
                candidates_today=candidates_today,
                likes=likes,
                dislikes=dislikes,
                avg_alert_score=round(float(avg_alert_score_raw), 3) if avg_alert_score_raw is not None else 0.0,
                pending_deliveries=pending_deliveries,
                failed_deliveries=failed_deliveries,
                last_successful_scan_at=last_successful_scan_at,
            )

    _SCORE_DISTRIBUTION_WINDOWS: dict[str, timedelta | None] = {
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "12h": timedelta(hours=12),
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "all": None,
    }

    def get_score_distribution(self, window: str = "7d") -> ScoreDistribution:
        """Histogram of judge scores (1-100, derived from the stored 0-1 `final_score`) over the
        given time window. Only successfully judged candidates count; failed judgments store
        final_score=0.0 and would otherwise pile up in the lowest bin."""
        delta = self._SCORE_DISTRIBUTION_WINDOWS.get(window, self._SCORE_DISTRIBUTION_WINDOWS["7d"])
        with session_scope() as session:
            query = select(Candidate.final_score).where(Candidate.grading_stage == "vlm_judged")
            if delta is not None:
                query = query.where(Candidate.created_at >= datetime.now(UTC) - delta)
            scores = session.scalars(query).all()

        bin_width = 10
        bin_counts = [0] * (100 // bin_width)
        for final_score in scores:
            score_10 = round(final_score * 100)
            bin_index = min(max(score_10 - 1, 0) // bin_width, len(bin_counts) - 1)
            bin_counts[bin_index] += 1

        total = len(scores)
        bins = [
            ScoreDistributionBin(
                min_score=index * bin_width + 1,
                max_score=(index + 1) * bin_width,
                count=count,
                percentage=round(count / total * 100, 2) if total else 0.0,
            )
            for index, count in enumerate(bin_counts)
        ]
        return ScoreDistribution(
            window=cast(Literal["1h", "6h", "12h", "1d", "7d", "30d", "all"], window), total_count=total, bins=bins
        )

    def get_ai_cost_stats(self) -> AiCostStats:
        now = datetime.now(UTC)
        cutoff_24h = now - timedelta(hours=24)
        cutoff_7d = now - timedelta(days=7)
        cutoff_30d = now - timedelta(days=30)

        _JUDGE_OPS = ["judge_grid"]
        _LEARNING_OPS = ["describe_images", "describe_candidate", "build_taste_profile"]

        with session_scope() as session:
            def _agg(cutoff: datetime | None, ops: list[str] | None = None) -> tuple[float, int]:
                q = select(func.sum(AiUsageEvent.cost_usd), func.count(AiUsageEvent.id))
                if ops is not None:
                    q = q.where(AiUsageEvent.operation.in_(ops))
                if cutoff is not None:
                    q = q.where(AiUsageEvent.called_at >= cutoff)
                row = session.execute(q).one()
                return (float(row[0] or 0.0), int(row[1] or 0))

            def _category(ops: list[str]) -> AiCategoryStats:
                t_usd, t_calls = _agg(None, ops)
                usd_24h, calls_24h = _agg(cutoff_24h, ops)
                usd_7d, calls_7d = _agg(cutoff_7d, ops)
                usd_30d, calls_30d = _agg(cutoff_30d, ops)
                return AiCategoryStats(
                    total_usd=t_usd,
                    last_24h_usd=usd_24h,
                    last_7d_usd=usd_7d,
                    last_30d_usd=usd_30d,
                    total_calls=t_calls,
                    last_24h_calls=calls_24h,
                    last_7d_calls=calls_7d,
                    last_30d_calls=calls_30d,
                )

            total_usd, total_calls = _agg(None)
            usd_24h, calls_24h = _agg(cutoff_24h)
            usd_7d, calls_7d = _agg(cutoff_7d)
            usd_30d, calls_30d = _agg(cutoff_30d)

            judge = _category(_JUDGE_OPS)
            learning = _category(_LEARNING_OPS)

        return AiCostStats(
            total_usd=total_usd,
            last_24h_usd=usd_24h,
            last_7d_usd=usd_7d,
            last_30d_usd=usd_30d,
            total_calls=total_calls,
            last_24h_calls=calls_24h,
            last_7d_calls=calls_7d,
            last_30d_calls=calls_30d,
            judge=judge,
            learning=learning,
        )

    def prune_old_records(self) -> dict[str, int]:
        """Delete rows older than the configured retention windows so the SQLite file and the
        stats/queue scans stay bounded. Pending/processing deliveries are never pruned. Returns
        the per-table deleted-row counts for logging."""
        now = datetime.now(UTC)
        usage_cutoff = now - timedelta(days=self.settings.ai_usage_retention_days)
        delivery_cutoff = now - timedelta(days=self.settings.delivery_retention_days)
        candidate_cutoff = now - timedelta(days=self.settings.candidate_retention_days)

        with session_scope() as session:
            usage_deleted = session.execute(
                delete(AiUsageEvent).where(AiUsageEvent.called_at < usage_cutoff)
            ).rowcount

            # Only terminal deliveries are eligible; never drop work still queued or in flight.
            deliveries_deleted = session.execute(
                delete(AlertDeliveryState).where(
                    AlertDeliveryState.status.in_(("sent", "failed")),
                    AlertDeliveryState.updated_at < delivery_cutoff,
                )
            ).rowcount

            # Delete the old candidates' delivery rows first so the FK on alert_deliveries holds.
            old_candidate_ids = select(Candidate.id).where(Candidate.created_at < candidate_cutoff)
            deliveries_deleted = (deliveries_deleted or 0) + (
                session.execute(
                    delete(AlertDeliveryState).where(AlertDeliveryState.candidate_id.in_(old_candidate_ids))
                ).rowcount
                or 0
            )
            candidates_deleted = session.execute(
                delete(Candidate).where(Candidate.created_at < candidate_cutoff)
            ).rowcount

            snapshots_deleted = session.execute(
                delete(LearningSnapshotState).where(LearningSnapshotState.created_at < candidate_cutoff)
            ).rowcount

        counts = {
            "ai_usage_events": int(usage_deleted or 0),
            "alert_deliveries": int(deliveries_deleted or 0),
            "candidates": int(candidates_deleted or 0),
            "learning_snapshots": int(snapshots_deleted or 0),
        }
        if any(counts.values()):
            logger.info(
                "Pruned old records: %d candidates, %d deliveries, %d AI-usage events, %d learning snapshots.",
                counts["candidates"],
                counts["alert_deliveries"],
                counts["ai_usage_events"],
                counts["learning_snapshots"],
            )
        return counts
