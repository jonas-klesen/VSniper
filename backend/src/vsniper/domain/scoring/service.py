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


def _decision_for(score: int, alert_threshold: int) -> Literal["alert", "review", "discard"]:
    if score >= alert_threshold:
        return "alert"
    if score >= SCORE_REVIEW_THRESHOLD:
        return "review"
    return "discard"


def retarget_judgment_trace(trace: ScoreTrace, *, alert_threshold: int | None) -> ScoreTrace:
    """Re-derive the decision of a stored judgment for a new alert threshold, keeping its score."""
    effective_alert_threshold = _coerce_alert_threshold(alert_threshold)
    decision = _decision_for(trace.score_10, effective_alert_threshold)
    return trace.model_copy(
        update={
            "threshold": round(effective_alert_threshold / 100, 3),
            "decision": decision,
            "summary": (
                f"{trace.score_10}/100; alert threshold={effective_alert_threshold}; decision={decision}. "
                f"{trace.explanation}"
            ),
        }
    )


def build_judgment_trace(
    *,
    judgment: CandidateJudgment,
    taste_profile: TasteProfile,
    model: str,
    alert_threshold: int | None = None,
) -> ScoreTrace:
    effective_alert_threshold = _coerce_alert_threshold(alert_threshold)
    normalized = round(judgment.score / 100, 3)
    decision = _decision_for(judgment.score, effective_alert_threshold)

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
