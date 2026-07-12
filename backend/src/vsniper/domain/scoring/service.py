from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from vsniper.domain.contracts import (
    CandidateJudgment,
    ScoreTrace,
    TasteProfile,
)

DEFAULT_SCORE_ALERT_THRESHOLD = 95
SCORE_REVIEW_THRESHOLD = 50


def _coerce_alert_threshold(value: int | None) -> int:
    if value is None:
        return DEFAULT_SCORE_ALERT_THRESHOLD
    return max(1, min(100, int(value)))


def build_judgment_trace(
    *,
    judgment: CandidateJudgment,
    taste_profile: TasteProfile,
    model: str,
    batch_id: str,
    alert_threshold: int | None = None,
) -> ScoreTrace:
    effective_alert_threshold = _coerce_alert_threshold(alert_threshold)
    normalized = round(judgment.score / 100, 3)
    if judgment.score >= effective_alert_threshold:
        decision: Literal["alert", "review", "discard"] = "alert"
    elif judgment.score >= SCORE_REVIEW_THRESHOLD:
        decision = "review"
    else:
        decision = "discard"

    return ScoreTrace(
        final_score=normalized,
        score_10=judgment.score,
        threshold=round(effective_alert_threshold / 100, 3),
        decision=decision,
        summary=f"{judgment.score}/100; alert threshold={effective_alert_threshold}; decision={decision}. {judgment.explanation}",
        explanation=judgment.explanation,
        labels=judgment.labels,
        concerns=judgment.concerns,
        model=model,
        prompt_version=taste_profile.version,
        grid_batch_id=batch_id,
        grid_position=judgment.position,
        judged_at=datetime.now(UTC),
    )


def build_failed_judgment_trace(
    *,
    title: str,
    error: str,
    model: str | None = None,
    alert_threshold: int | None = None,
) -> ScoreTrace:
    effective_alert_threshold = _coerce_alert_threshold(alert_threshold)
    return ScoreTrace(
        final_score=0.0,
        score_10=0,
        threshold=round(effective_alert_threshold / 100, 3),
        decision="discard",
        summary=f"{title} could not be judged: {error}",
        explanation=error,
        concerns=["Judging failed"],
        model=model,
        judged_at=datetime.now(UTC),
    )
