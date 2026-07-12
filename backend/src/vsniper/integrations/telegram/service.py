from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from vsniper.domain.contracts import (
    CandidateRecord,
    SearchDraftApplyResult,
    TasteRecomputeResult,
    TasteSnapshot,
    TelegramFeedbackCallback,
    TelegramTasteCallback,
)


CALLBACK_PREFIX = "feedback"
TASTE_CALLBACK_PREFIX = "taste"


_DESCRIPTION_MAX_CHARS = 300


class TelegramFormatter:
    def build_alert_message(self, candidate: CandidateRecord) -> str:
        description = (candidate.description or "").strip()
        if len(description) > _DESCRIPTION_MAX_CHARS:
            boundary = description.rfind(" ", 0, _DESCRIPTION_MAX_CHARS)
            description = description[: boundary if boundary > 50 else _DESCRIPTION_MAX_CHARS].rstrip() + "…"

        parts = [
            f"🧥 {candidate.title}",
            f"Brand: {candidate.brand}  |  Size: {candidate.size}  |  Price: €{candidate.price_eur:.2f}",
        ]
        if description:
            parts.append(f"\n{description}")
        parts += [
            f"\nDecision: {candidate.score_trace.decision} ({candidate.score_trace.score_10}/100)",
            f"Why: {candidate.score_trace.explanation or candidate.score_trace.summary}",
            f"🔗 {candidate.url}",
        ]
        return "\n".join(parts)

    def build_feedback_reply_markup(self, delivery_id: str) -> dict[str, list[list[dict[str, str]]]]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "👍 Like",
                        "callback_data": self.build_feedback_callback_data(delivery_id=delivery_id, verdict="like"),
                    },
                    {
                        "text": "👎 Dislike",
                        "callback_data": self.build_feedback_callback_data(delivery_id=delivery_id, verdict="dislike"),
                    },
                ]
            ]
        }

    @staticmethod
    def build_feedback_applied_message(*, message_text: str, verdict: str) -> str:
        base_text = message_text.split("\n\nFeedback:", maxsplit=1)[0].rstrip()
        badge = "👍 Like" if verdict == "like" else "👎 Dislike"
        return f"{base_text}\n\nFeedback: {badge}\n↩ Reply to add a note (feeds recompute)"

    @staticmethod
    def build_feedback_callback_data(*, delivery_id: str, verdict: str) -> str:
        return f"{CALLBACK_PREFIX}:{delivery_id}:{verdict}"

    @staticmethod
    def parse_feedback_callback_data(data: str) -> TelegramFeedbackCallback | None:
        prefix, separator, payload = data.partition(":")
        if prefix != CALLBACK_PREFIX or not separator or not payload:
            return None

        delivery_id, separator, verdict = payload.partition(":")
        if not separator or not delivery_id or verdict not in {"like", "dislike"}:
            return None

        typed_verdict: Literal["like", "dislike"] = "like" if verdict == "like" else "dislike"
        return TelegramFeedbackCallback(delivery_id=delivery_id, verdict=typed_verdict)

    @staticmethod
    def build_taste_callback_data(*, action: str, profile_version: int | None = None) -> str:
        if profile_version is None:
            return f"{TASTE_CALLBACK_PREFIX}:{action}"
        return f"{TASTE_CALLBACK_PREFIX}:{action}:{profile_version}"

    @staticmethod
    def parse_taste_callback_data(data: str) -> TelegramTasteCallback | None:
        parts = data.split(":")
        if len(parts) < 2 or parts[0] != TASTE_CALLBACK_PREFIX:
            return None
        action = parts[1]
        if action not in {"recompute", "apply_drafts", "skip_drafts"}:
            return None
        profile_version: int | None = None
        if len(parts) == 3 and parts[2]:
            try:
                profile_version = int(parts[2])
            except ValueError:
                return None
        elif len(parts) > 3:
            return None
        typed_action: Literal["recompute", "apply_drafts", "skip_drafts"]
        typed_action = "recompute" if action == "recompute" else "apply_drafts" if action == "apply_drafts" else "skip_drafts"
        return TelegramTasteCallback(action=typed_action, profile_version=profile_version)

    def build_taste_status_reply_markup(self) -> dict[str, list[list[dict[str, str]]]]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "🧠 Recompute taste",
                        "callback_data": self.build_taste_callback_data(action="recompute"),
                    }
                ]
            ]
        }

    def build_taste_draft_reply_markup(self, *, profile_version: int) -> dict[str, list[list[dict[str, str]]]]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Apply drafted searches",
                        "callback_data": self.build_taste_callback_data(
                            action="apply_drafts",
                            profile_version=profile_version,
                        ),
                    },
                    {
                        "text": "Not now",
                        "callback_data": self.build_taste_callback_data(
                            action="skip_drafts",
                            profile_version=profile_version,
                        ),
                    },
                ]
            ]
        }

    @staticmethod
    def _format_dt(value: datetime | None) -> str:
        if value is None:
            return "never"
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")

    def build_taste_status_message(self, snapshot: TasteSnapshot) -> str:
        profile = snapshot.taste_profile
        dirty = snapshot.dirty_counts
        recompute = snapshot.recompute_state
        parts = [
            "🧠 Taste status",
            f"Samples: {len(snapshot.samples)} total · {dirty.new_or_changed_samples} changed",
            (
                "Changed evidence: "
                f"{dirty.new_or_changed_positive_samples} positive · "
                f"{dirty.new_or_changed_negative_samples} negative · "
                f"manual note {'changed' if dirty.manual_note_changed else 'unchanged'}"
            ),
            f"Last recompute: {self._format_dt(snapshot.last_recomputed_at)}",
        ]
        if profile is None:
            parts.append("Active profile: none yet")
        else:
            model_bits = [f"v{profile.version}"]
            if profile.model:
                model_bits.append(profile.model)
            parts.append(f"Active profile: {' · '.join(model_bits)}")
            parts.append(f"Generated drafts: {len(profile.generated_searches)}")
        if recompute.status == "running":
            parts.append(f"Recompute: running since {self._format_dt(recompute.started_at)}")
        elif recompute.status == "failed" and recompute.error:
            parts.append(f"Last recompute failed: {recompute.error[:160]}")
        elif recompute.status == "succeeded":
            parts.append(
                f"Last cost: ${recompute.last_cost_usd:.4f} "
                f"({recompute.last_input_tokens} in / {recompute.last_output_tokens} out tokens)"
            )
        return "\n".join(parts)

    @staticmethod
    def build_taste_recompute_started_message(*, started_at: datetime | None = None) -> str:
        return (
            f"🧠 Taste recompute started at {TelegramFormatter._format_dt(started_at)}. "
            "I’ll report back here when it finishes."
        )

    @staticmethod
    def build_taste_recompute_already_running_message(*, started_at: datetime | None = None) -> str:
        return (
            f"🧠 Taste recompute is already running since {TelegramFormatter._format_dt(started_at)}. "
            "I’ll avoid starting a duplicate run."
        )

    def build_taste_recompute_success_message(self, result: TasteRecomputeResult) -> str:
        profile = result.snapshot.taste_profile
        profile_text = f"v{profile.version}" if profile else "unknown version"
        draft_count = len(profile.generated_searches) if profile else 0
        return "\n".join(
            [
                "✅ Taste recompute finished",
                f"Profile: {profile_text}",
                f"Cost: ${result.cost_usd:.4f}",
                f"Tokens: {result.input_tokens} in / {result.output_tokens} out",
                f"Observation cache: {result.observation_cache.cached_observations} cached / {result.observation_cache.fresh_observations} fresh",
                f"Generated search drafts: {draft_count}",
                "Apply the drafted search changes?" if draft_count else "No generated drafts are available to apply.",
            ]
        )

    @staticmethod
    def build_taste_recompute_failed_message(error: str) -> str:
        return f"❌ Taste recompute failed: {error[:800]}"

    @staticmethod
    def build_search_draft_apply_message(result: SearchDraftApplyResult) -> str:
        if result.stale:
            return f"⚠️ {result.summary} Run /taste again to review the current profile."
        return "\n".join(
            [
                "✅ Drafted search changes applied",
                result.summary,
                f"Changed: {result.applied_searches} · unchanged: {result.unchanged_searches} · skipped: {result.skipped_searches}",
            ]
        )
