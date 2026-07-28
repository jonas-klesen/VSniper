from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal, cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from vsniper.core.config import Settings, get_settings
from vsniper.core.database import session_scope
from vsniper.db.models import AlertDeliveryState, AppSettingsState, Candidate
from vsniper.domain.contracts import (
    DeliveryProcessingResult,
    SearchDraftApplyResult,
    TelegramCallbackMessage,
    TelegramCallbackQuery,
    TelegramMessage,
    TelegramUpdate,
    TelegramWebhookRegistrationPayload,
    TelegramWebhookResult,
    TelegramWebhookStatus,
)
from vsniper.integrations.telegram.client import TelegramClient
from vsniper.integrations.telegram.service import TelegramFormatter
from vsniper.integrations.vinted.client import VintedClient
from vsniper.services._mapping import (
    as_aware,
    candidate_to_contract,
    is_value_configured,
    timestamp_to_datetime,
)
from vsniper.services.candidate_service import CandidateService
from vsniper.services.error_service import ErrorService
from vsniper.services.taste_service import TasteService

logger = logging.getLogger(__name__)

DELIVERY_MAX_ATTEMPTS = 3
DELIVERY_RETRY_DELAYS = (timedelta(seconds=0), timedelta(seconds=30), timedelta(minutes=5))
DELIVERY_PROCESSING_TIMEOUT = timedelta(minutes=10)
ACTIVE_DELIVERY_STATUSES = ("pending", "processing", "sent")
# Statuses that block re-queuing the same candidate. Includes "failed" so a delivery
# that exhausted its retries is not re-inserted (with fresh attempts) on every scan.
DEDUP_DELIVERY_STATUSES = ACTIVE_DELIVERY_STATUSES + ("failed",)


@dataclass(frozen=True)
class TelegramRuntimeConfig:
    bot_token: str
    chat_id: str
    webhook_url: str | None
    webhook_secret: str | None


def _configured_or_none(value: str, *, placeholder: str) -> str | None:
    stripped = value.strip()
    if not is_value_configured(stripped, placeholder=placeholder):
        return None
    return stripped


def _telegram_config_from_model(model: AppSettingsState | None) -> TelegramRuntimeConfig:
    runtime = get_settings()
    bot_token = (model.telegram_bot_token if model and model.telegram_bot_token else runtime.telegram_bot_token).strip()
    chat_id = (model.telegram_chat_id if model and model.telegram_chat_id else runtime.telegram_chat_id).strip()
    webhook_url_value = model.telegram_webhook_url if model and model.telegram_webhook_url else runtime.telegram_webhook_url
    webhook_secret_value = (
        model.telegram_webhook_secret if model and model.telegram_webhook_secret else runtime.telegram_webhook_secret
    )
    return TelegramRuntimeConfig(
        bot_token=bot_token,
        chat_id=chat_id,
        webhook_url=_configured_or_none(
            webhook_url_value,
            placeholder="put-your-telegram-webhook-url-here",
        ),
        webhook_secret=_configured_or_none(
            webhook_secret_value,
            placeholder="put-your-telegram-webhook-secret-here",
        ),
    )


def _telegram_config() -> TelegramRuntimeConfig:
    with session_scope() as session:
        return _telegram_config_from_model(session.get(AppSettingsState, 1))


class TelegramService:
    def __init__(
        self,
        settings: Settings,
        telegram_client: TelegramClient,
        telegram_formatter: TelegramFormatter,
        candidates: CandidateService,
        vinted_client: VintedClient,
        taste: TasteService,
        errors: ErrorService | None = None,
    ) -> None:
        self.settings = settings
        self.telegram_client = telegram_client
        self.telegram_formatter = telegram_formatter
        self.candidates = candidates
        self.vinted_client = vinted_client
        self.taste = taste
        self.errors = errors
        self._apply_search_drafts: Callable[[int | None], SearchDraftApplyResult] | None = None

    def set_search_draft_applier(self, applier: Callable[[int | None], SearchDraftApplyResult]) -> None:
        self._apply_search_drafts = applier

    def _apply_bot_token(self, config: TelegramRuntimeConfig) -> None:
        self.telegram_client.bot_token = config.bot_token

    def get_webhook_status(self) -> TelegramWebhookStatus:
        checked_at = datetime.now(UTC)
        config = _telegram_config()
        self._apply_bot_token(config)
        configured_url = config.webhook_url
        configured_secret = config.webhook_secret

        try:
            info = self.telegram_client.get_webhook_info()
        except Exception as exc:
            return TelegramWebhookStatus(
                last_check_ok=False,
                is_registered=False,
                matches_configured_url=False,
                configured_url=configured_url,
                effective_url=None,
                has_secret_token=bool(configured_secret),
                checked_at=checked_at,
                detail=str(exc),
            )

        effective_url = info.get("url") or None
        raw_allowed_updates = info.get("allowed_updates")
        allowed_updates = raw_allowed_updates if isinstance(raw_allowed_updates, list) else []
        matches_configured_url = bool(configured_url and effective_url and configured_url == effective_url)
        if effective_url and configured_url and matches_configured_url:
            detail = "Telegram webhook is registered and matches the configured URL."
        elif effective_url and configured_url:
            detail = "Telegram webhook is registered, but the live URL differs from the configured URL."
        elif effective_url:
            detail = "Telegram webhook is registered, but no local TELEGRAM_WEBHOOK_URL is configured."
        else:
            detail = "Telegram webhook is not registered yet."

        return TelegramWebhookStatus(
            last_check_ok=True,
            is_registered=bool(effective_url),
            matches_configured_url=matches_configured_url,
            configured_url=configured_url,
            effective_url=effective_url,
            has_secret_token=bool(configured_secret),
            pending_update_count=int(info.get("pending_update_count") or 0),
            allowed_updates=[str(item) for item in allowed_updates],
            last_error_message=info.get("last_error_message"),
            last_error_at=timestamp_to_datetime(info.get("last_error_date")),
            checked_at=checked_at,
            detail=detail,
        )

    def configure_webhook(self, payload: TelegramWebhookRegistrationPayload) -> TelegramWebhookStatus:
        with session_scope() as session:
            model = session.get(AppSettingsState, 1)
            config = _telegram_config_from_model(model)
            target_url = str(payload.url) if payload.url is not None else config.webhook_url
            if payload.url is not None and model is not None:
                model.telegram_webhook_url = str(payload.url).strip()
                config = _telegram_config_from_model(model)
        if not target_url:
            raise ValueError(
                "No Telegram webhook URL was provided. Set TELEGRAM_WEBHOOK_URL or provide a URL when registering."
            )

        self._apply_bot_token(config)
        self.telegram_client.set_webhook(
            url=target_url,
            secret_token=config.webhook_secret,
            allowed_updates=["callback_query", "message"],
            drop_pending_updates=payload.drop_pending_updates,
        )
        return self.get_webhook_status()

    def is_webhook_secret_valid(self, provided_secret: str | None) -> bool:
        expected_secret = _telegram_config().webhook_secret
        if expected_secret is None:
            # No secret configured: the endpoint cannot authenticate that the request
            # actually came from Telegram, so it falls open here. Inbound chat-id
            # authorization on every mutating path is the defense-in-depth that keeps
            # forged updates from poisoning taste learning, but configuring
            # TELEGRAM_WEBHOOK_SECRET is strongly recommended.
            logger.warning(
                "Telegram webhook accepted without a configured secret token; "
                "set TELEGRAM_WEBHOOK_SECRET to authenticate inbound requests."
            )
            return True
        if provided_secret is None:
            return False
        return hmac.compare_digest(provided_secret, expected_secret)

    def send_test_notification(self) -> dict[str, object]:
        config = _telegram_config()
        self._apply_bot_token(config)
        if not is_value_configured(config.bot_token, placeholder="put-your-telegram-bot-token-here"):
            raise ValueError("Telegram bot token is missing.")
        if not is_value_configured(config.chat_id, placeholder="put-your-telegram-chat-id-here"):
            raise ValueError("Telegram chat ID is missing.")

        response = self.telegram_client.send_message(
            chat_id=config.chat_id,
            text="vsniper test notification: Telegram delivery is configured.",
        )
        return {"ok": True, "message_id": response.get("message_id"), "chat_id": config.chat_id}

    @staticmethod
    def _get_latest_delivery(session: Session, candidate_id: str) -> AlertDeliveryState | None:
        return session.scalar(
            select(AlertDeliveryState)
            .where(AlertDeliveryState.candidate_id == candidate_id)
            .order_by(AlertDeliveryState.created_at.desc())
            .limit(1)
        )

    def queue_delivery(self, session: Session, candidate: Candidate) -> bool:
        preview_message = self.telegram_formatter.build_alert_message(candidate_to_contract(candidate))
        now = datetime.now(UTC)
        result = cast(
            CursorResult,
            session.execute(
                sqlite_insert(AlertDeliveryState)
                .values(
                    candidate_id=candidate.id,
                    channel="telegram",
                    status="pending",
                    attempt_count=0,
                    last_error=None,
                    payload_preview=preview_message,
                    created_at=now,
                    updated_at=now,
                    last_attempted_at=None,
                    sent_at=None,
                )
                .on_conflict_do_nothing(
                    index_elements=[AlertDeliveryState.candidate_id, AlertDeliveryState.channel],
                    index_where=AlertDeliveryState.status.in_(DEDUP_DELIVERY_STATUSES),
                )
            ),
        )
        return result.rowcount == 1

    def retry_delivery(self, candidate_id: str) -> bool:
        """Re-queue the latest failed delivery for a candidate so the worker picks it up next
        cycle. Resets the attempt counter and backoff timer so it is immediately eligible.
        Returns False if there is no delivery to retry or it is not in a failed state."""
        now = datetime.now(UTC)
        with session_scope() as session:
            delivery = self._get_latest_delivery(session, candidate_id)
            if delivery is None or delivery.status != "failed":
                return False
            delivery.status = "pending"
            delivery.attempt_count = 0
            delivery.last_error = None
            delivery.last_attempted_at = None
            delivery.sent_at = None
            delivery.updated_at = now
            return True

    @staticmethod
    def _retry_delay(attempt_count: int) -> timedelta:
        index = max(0, min(attempt_count, len(DELIVERY_RETRY_DELAYS) - 1))
        return DELIVERY_RETRY_DELAYS[index]

    @classmethod
    def _is_eligible(cls, delivery: AlertDeliveryState, *, now: datetime) -> bool:
        if delivery.status not in {"pending", "processing"}:
            return False
        if delivery.attempt_count >= DELIVERY_MAX_ATTEMPTS:
            return False
        if delivery.last_attempted_at is None:
            return True
        last_attempted_at = cast(datetime, as_aware(delivery.last_attempted_at))
        if delivery.status == "processing":
            return last_attempted_at + DELIVERY_PROCESSING_TIMEOUT <= now
        return last_attempted_at + cls._retry_delay(delivery.attempt_count) <= now

    @staticmethod
    def _count_pending_deliveries(session: Session) -> int:
        # Cheap COUNT for the zero-check / skip messaging — does not load rows into memory.
        return int(
            session.scalar(
                select(func.count())
                .select_from(AlertDeliveryState)
                .where(
                    AlertDeliveryState.channel == "telegram",
                    AlertDeliveryState.status.in_(("pending", "processing")),
                )
            )
            or 0
        )

    def _pending_deliveries(self, session: Session, *, limit: int | None = None) -> list[AlertDeliveryState]:
        # Bounded so a backlog can't load the whole table into memory; the Python eligibility
        # filter below may drop some (not-yet-due retries), so callers fetch a multiple of what
        # they intend to claim to leave headroom.
        query = (
            select(AlertDeliveryState)
            .where(
                AlertDeliveryState.channel == "telegram",
                AlertDeliveryState.status.in_(("pending", "processing")),
            )
            .order_by(AlertDeliveryState.created_at.asc())
        )
        if limit is not None:
            query = query.limit(limit)
        deliveries = session.scalars(query).all()
        now = datetime.now(UTC)
        return [delivery for delivery in deliveries if self._is_eligible(delivery, now=now)]

    def _claim_pending_deliveries(self, session: Session, *, limit: int) -> list[AlertDeliveryState]:
        claimed: list[AlertDeliveryState] = []
        # Fetch a multiple of the claim target so the eligibility filter has headroom.
        for delivery in self._pending_deliveries(session, limit=max(limit, 1) * 4):
            if len(claimed) >= limit:
                break

            attempted_at = datetime.now(UTC)
            result = cast(
                CursorResult,
                session.execute(
                    update(AlertDeliveryState)
                    .where(AlertDeliveryState.id == delivery.id)
                    .where(AlertDeliveryState.status == delivery.status)
                    .where(AlertDeliveryState.attempt_count == delivery.attempt_count)
                    .values(
                        status="processing",
                        attempt_count=delivery.attempt_count + 1,
                        last_attempted_at=attempted_at,
                        updated_at=attempted_at,
                    )
                ),
            )
            if result.rowcount != 1:
                continue

            session.flush()
            session.refresh(delivery)
            claimed.append(delivery)
        return claimed

    @staticmethod
    def _record_success(
        delivery: AlertDeliveryState,
        *,
        attempted_at: datetime,
        telegram_message_id: int | None = None,
        telegram_chat_id: str | None = None,
    ) -> None:
        delivery.status = "sent"
        delivery.last_error = None
        delivery.sent_at = attempted_at
        delivery.updated_at = attempted_at
        delivery.telegram_message_id = telegram_message_id
        delivery.telegram_chat_id = telegram_chat_id

    @staticmethod
    def _record_failure(
        delivery: AlertDeliveryState,
        *,
        attempted_at: datetime,
        error_message: str,
        retryable: bool,
    ) -> str:
        delivery.last_error = error_message
        delivery.sent_at = None
        delivery.updated_at = attempted_at
        if retryable and delivery.attempt_count < DELIVERY_MAX_ATTEMPTS:
            delivery.status = "pending"
            return "retry"
        delivery.status = "failed"
        return "failed"

    def _send_claimed_delivery(self, delivery_id: int, config: TelegramRuntimeConfig) -> str:
        """Send a single already-claimed delivery and write its result back in its own
        short transaction. No write lock is held across the (slow) Telegram network call,
        and the per-delivery commit bounds re-send-on-crash to at most one alert."""
        # Build the alert message in a short read transaction; the write lock is not held
        # while the network call below runs.
        with session_scope() as session:
            delivery = session.get(AlertDeliveryState, delivery_id)
            if delivery is None:
                return "failed"
            candidate = session.get(Candidate, delivery.candidate_id)
            if candidate is None:
                outcome = self._record_failure(
                    delivery,
                    attempted_at=datetime.now(UTC),
                    error_message="Candidate no longer exists for this alert delivery.",
                    retryable=False,
                )
                errors = getattr(self, "errors", None)
                if errors is not None:
                    errors.record(
                        source="telegram",
                        operation="deliver_candidate_alert",
                        summary="Telegram candidate alert failed",
                        message="Candidate no longer exists for this alert delivery.",
                        details={"delivery_id": delivery_id, "candidate_id": delivery.candidate_id},
                        related_entity_type="alert_delivery",
                        related_entity_id=delivery_id,
                        session=session,
                    )
                return outcome
            payload_preview = self.telegram_formatter.build_alert_message(candidate_to_contract(candidate))

        attempted_at = datetime.now(UTC)
        try:
            response = self.telegram_client.send_message(
                chat_id=config.chat_id,
                text=payload_preview,
                reply_markup=self.telegram_formatter.build_feedback_reply_markup(str(delivery_id)),
            )
        except Exception as exc:
            with session_scope() as session:
                delivery = session.get(AlertDeliveryState, delivery_id)
                if delivery is None:
                    return "failed"
                delivery.payload_preview = payload_preview
                outcome = self._record_failure(
                    delivery,
                    attempted_at=attempted_at,
                    error_message=str(exc),
                    retryable=getattr(exc, "retryable", False),
                )
                errors = getattr(self, "errors", None)
                if outcome == "failed" and errors is not None:
                    errors.record(
                        source="telegram",
                        operation="deliver_candidate_alert",
                        summary="Telegram candidate alert exhausted its retries",
                        exception=exc,
                        details={
                            "delivery_id": delivery_id,
                            "candidate_id": delivery.candidate_id,
                            "attempt_count": delivery.attempt_count,
                            "payload_preview": payload_preview,
                        },
                        related_entity_type="alert_delivery",
                        related_entity_id=delivery_id,
                        session=session,
                    )
                return outcome

        with session_scope() as session:
            delivery = session.get(AlertDeliveryState, delivery_id)
            if delivery is None:
                return "sent"
            delivery.payload_preview = payload_preview
            self._record_success(
                delivery,
                attempted_at=attempted_at,
                telegram_message_id=response.get("message_id"),
                telegram_chat_id=str(response.get("chat", {}).get("id")) if response.get("chat") else None,
            )
        return "sent"

    def process_pending_deliveries(self, limit: int = 25) -> DeliveryProcessingResult:
        # Phase 1: claim eligible deliveries in a short transaction and commit, so the
        # SQLite write lock is released before any (slow) Telegram sends. The committed
        # "processing" status is the cross-process lock that keeps a second worker from
        # re-sending the same alerts.
        with session_scope() as session:
            config = _telegram_config_from_model(session.get(AppSettingsState, 1))
            self._apply_bot_token(config)
            pending_count = self._count_pending_deliveries(session)
            if pending_count == 0:
                return DeliveryProcessingResult(summary="No eligible Telegram deliveries were waiting to be processed.")

            telegram_configured = is_value_configured(
                config.bot_token,
                placeholder="put-your-telegram-bot-token-here",
            ) and is_value_configured(config.chat_id, placeholder="put-your-telegram-chat-id-here")
            if not telegram_configured:
                return DeliveryProcessingResult(
                    eligible_deliveries=pending_count,
                    skipped_reason="Telegram credentials are missing.",
                    summary=(
                        f"Skipped Telegram delivery processing because credentials are missing; "
                        f"{pending_count} queued deliveries remain pending."
                    ),
                )

            eligible_deliveries = self._claim_pending_deliveries(session, limit=limit)
            claimed_ids = [delivery.id for delivery in eligible_deliveries]

        eligible_count = len(claimed_ids)
        if eligible_count == 0:
            return DeliveryProcessingResult(summary="No eligible Telegram deliveries were claimed for processing.")

        sent_deliveries = 0
        retry_scheduled_deliveries = 0
        failed_deliveries = 0
        processed_deliveries = 0

        # Phase 2: send each claimed delivery without holding the write lock; results are
        # written back per delivery in their own short transactions.
        for delivery_id in claimed_ids:
            outcome = self._send_claimed_delivery(delivery_id, config)
            processed_deliveries += 1
            if outcome == "sent":
                sent_deliveries += 1
            elif outcome == "retry":
                retry_scheduled_deliveries += 1
            else:
                failed_deliveries += 1

        return DeliveryProcessingResult(
            eligible_deliveries=eligible_count,
            processed_deliveries=processed_deliveries,
            sent_deliveries=sent_deliveries,
            retry_scheduled_deliveries=retry_scheduled_deliveries,
            failed_deliveries=failed_deliveries,
            summary=(
                f"Processed {processed_deliveries} Telegram deliveries: "
                f"{sent_deliveries} sent, {retry_scheduled_deliveries} scheduled for retry, "
                f"and {failed_deliveries} marked failed."
            ),
        )

    _REFRESH_TOKEN_WARNING_THRESHOLD = timedelta(hours=24)

    def check_refresh_token_expiry(self) -> None:
        refresh_token_expiry = self.vinted_client.get_refresh_token_expiry()
        if refresh_token_expiry is None:
            return

        now = datetime.now(UTC)
        if refresh_token_expiry - now > self._REFRESH_TOKEN_WARNING_THRESHOLD:
            return

        expiry_key = refresh_token_expiry.isoformat()
        # Phase 1 — read txn: load config, apply the bot token (in-memory), and decide whether to send.
        with session_scope() as session:
            model = session.get(AppSettingsState, 1)
            config = _telegram_config_from_model(model)
            self._apply_bot_token(config)
            if model is not None and model.refresh_token_expiry_warning_sent_for == expiry_key:
                return
            if not is_value_configured(config.bot_token, placeholder="put-your-telegram-bot-token-here"):
                return
            if not is_value_configured(config.chat_id, placeholder="put-your-telegram-chat-id-here"):
                return

        if refresh_token_expiry <= now:
            text = (
                f"⚠️ Your Vinted refresh token expired at "
                f"{refresh_token_expiry.strftime('%Y-%m-%d %H:%M UTC')}. "
                f"Update it to keep automatic token refresh working."
            )
        else:
            text = (
                f"⚠️ Your Vinted refresh token expires soon "
                f"({refresh_token_expiry.strftime('%Y-%m-%d %H:%M UTC')}). "
                f"Update it before then to keep automatic token refresh working."
            )

        # Phase 2 — send the warning with no DB lock held.
        try:
            self.telegram_client.send_message(chat_id=config.chat_id, text=text)
        except Exception as exc:
            logger.warning("Failed to send refresh token expiry warning to Telegram", exc_info=True)
            errors = getattr(self, "errors", None)
            if errors is not None:
                errors.record(
                    source="telegram",
                    operation="send_refresh_token_expiry_warning",
                    summary="Telegram refresh-token warning failed",
                    exception=exc,
                    details={"refresh_token_expiry": refresh_token_expiry},
                    related_entity_type="app_settings",
                    related_entity_id=1,
                )
            return

        # Phase 3 — write txn: record the send (idempotent against a concurrent send for the same key).
        with session_scope() as session:
            model = session.get(AppSettingsState, 1)
            if model is not None and model.refresh_token_expiry_warning_sent_for != expiry_key:
                model.refresh_token_expiry_warning_sent_for = expiry_key

    def check_cookie_expiry(self) -> None:
        self.check_refresh_token_expiry()

    def _acknowledge_callback(self, callback_query_id: str, text: str) -> None:
        if not callback_query_id.strip():
            return
        try:
            self.telegram_client.answer_callback_query(callback_query_id=callback_query_id, text=text)
        except Exception as exc:
            logger.warning("Failed to ACK Telegram callback %s", callback_query_id, exc_info=True)
            errors = getattr(self, "errors", None)
            if errors is not None:
                errors.record(
                    source="telegram",
                    operation="acknowledge_callback",
                    summary="Telegram callback acknowledgement failed",
                    exception=exc,
                    details={"callback_query_id": callback_query_id},
                )

    def _update_feedback_message(self, callback_query: TelegramCallbackQuery, result: TelegramWebhookResult) -> None:
        if result.action not in {"feedback_recorded", "feedback_unchanged"} or result.verdict is None:
            return

        message = callback_query.message
        if message is None or message.chat is None or message.text is None or not message.text.strip():
            return

        updated_text = self.telegram_formatter.build_feedback_applied_message(
            message_text=message.text,
            verdict=result.verdict,
        )
        if updated_text == message.text:
            return

        try:
            self.telegram_client.edit_message_text(
                chat_id=message.chat.id,
                message_id=message.message_id,
                text=updated_text,
                reply_markup={"inline_keyboard": []},
            )
        except Exception as exc:
            logger.warning(
                "Failed to edit Telegram message %s in chat %s",
                message.message_id,
                message.chat.id,
                exc_info=True,
            )
            errors = getattr(self, "errors", None)
            if errors is not None:
                errors.record(
                    source="telegram",
                    operation="edit_feedback_message",
                    summary="Telegram feedback message update failed",
                    exception=exc,
                    details={"message_id": message.message_id, "chat_id": str(message.chat.id)},
                    related_entity_type="telegram_message",
                    related_entity_id=message.message_id,
                )

    @staticmethod
    def _callback_ack_text(result: TelegramWebhookResult) -> str:
        if result.action == "feedback_recorded" and result.verdict is not None:
            return f"Saved {result.verdict} feedback."
        if result.action == "feedback_unchanged" and result.verdict is not None:
            return f"Already marked {result.verdict}."
        if result.action == "taste_recompute_started":
            return "Taste recompute started."
        if result.action == "taste_recompute_already_running":
            return "Taste recompute is already running."
        if result.action == "taste_drafts_applied":
            return "Draft decision saved."
        if result.action == "taste_drafts_skipped":
            return "Skipped drafted changes."
        return result.detail

    @staticmethod
    def _message_chat_id(message: TelegramMessage | TelegramCallbackMessage | None) -> int | str | None:
        if message is None or message.chat is None:
            return None
        return message.chat.id

    @staticmethod
    def _is_authorized_chat(chat_id: int | str | None, config: TelegramRuntimeConfig) -> bool:
        if chat_id is None:
            return False
        if not is_value_configured(config.chat_id, placeholder="put-your-telegram-chat-id-here"):
            return False
        return str(chat_id) == str(config.chat_id)

    def _send_telegram_message(
        self,
        *,
        chat_id: int | str,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        try:
            self.telegram_client.send_message(chat_id=str(chat_id), text=text, reply_markup=reply_markup)
        except Exception as exc:
            logger.warning("Failed to send Telegram message to chat %s", chat_id, exc_info=True)
            errors = getattr(self, "errors", None)
            if errors is not None:
                errors.record(
                    source="telegram",
                    operation="send_message",
                    summary="Telegram message failed",
                    exception=exc,
                    details={"chat_id": str(chat_id), "message_preview": text[:500]},
                )

    def _edit_or_send_callback_message(
        self,
        callback_query: TelegramCallbackQuery,
        *,
        chat_id: int | str,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        message = callback_query.message
        if message is not None:
            try:
                self.telegram_client.edit_message_text(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
                return
            except Exception:
                logger.warning("Failed to edit Telegram taste message; sending a new one instead", exc_info=True)
        self._send_telegram_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    def _handle_taste_command(self, message: TelegramMessage, config: TelegramRuntimeConfig) -> TelegramWebhookResult:
        chat_id = self._message_chat_id(message)
        if not self._is_authorized_chat(chat_id, config):
            return TelegramWebhookResult(action="unauthorized", detail="Telegram chat is not authorized for taste controls.")
        snapshot = self.taste.get_snapshot()
        self._send_telegram_message(
            chat_id=cast(int | str, chat_id),
            text=self.telegram_formatter.build_taste_status_message(snapshot),
            reply_markup=self.telegram_formatter.build_taste_status_reply_markup(),
        )
        return TelegramWebhookResult(
            action="taste_status_sent",
            detail="Sent taste status to Telegram.",
            profile_version=snapshot.taste_profile.version if snapshot.taste_profile else None,
        )

    def _run_taste_recompute_background(self, *, job_id: str, chat_id: int | str) -> None:
        config = _telegram_config()
        self._apply_bot_token(config)
        try:
            result = self.taste.run_claimed_recompute(job_id)
        except Exception as exc:
            logger.exception("Telegram-triggered taste recompute failed")
            self._send_telegram_message(
                chat_id=chat_id,
                text=self.telegram_formatter.build_taste_recompute_failed_message(str(exc)),
            )
            return

        profile = result.snapshot.taste_profile
        reply_markup = None
        if profile is not None and profile.generated_searches:
            reply_markup = self.telegram_formatter.build_taste_draft_reply_markup(profile_version=profile.version)
        self._send_telegram_message(
            chat_id=chat_id,
            text=self.telegram_formatter.build_taste_recompute_success_message(result),
            reply_markup=reply_markup,
        )

    def _require_webhook_secret(
        self, callback_query: TelegramCallbackQuery, config: TelegramRuntimeConfig, *, action_label: str
    ) -> TelegramWebhookResult | None:
        """Block expensive/mutating taste callbacks unless a webhook secret is configured.

        Without TELEGRAM_WEBHOOK_SECRET the webhook falls open (it cannot authenticate that a
        request really came from Telegram), so chat-id authorization is the only barrier — and a
        forged update from anyone who guesses the chat id could trigger an OpenAI-spending recompute
        or rewrite the user's searches. Requiring a configured secret for those actions closes that.
        """
        if config.webhook_secret is not None:
            return None
        result = TelegramWebhookResult(
            action="unauthorized",
            detail=(
                f"{action_label} requires TELEGRAM_WEBHOOK_SECRET to be configured so inbound "
                "Telegram requests can be authenticated."
            ),
        )
        self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
        return result

    def _handle_taste_callback(
        self,
        callback_query: TelegramCallbackQuery,
        *,
        config: TelegramRuntimeConfig,
        schedule_background: Callable[[Callable[[], None]], None] | None = None,
    ) -> TelegramWebhookResult:
        chat_id = self._message_chat_id(callback_query.message)
        if not self._is_authorized_chat(chat_id, config):
            result = TelegramWebhookResult(
                action="unauthorized",
                detail="Telegram chat is not authorized for taste controls.",
            )
            self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
            return result
        parsed = self.telegram_formatter.parse_taste_callback_data(callback_query.data or "")
        if parsed is None:
            result = TelegramWebhookResult(action="invalid_callback", detail="Telegram taste callback was not recognized.")
            self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
            return result

        if parsed.action == "recompute":
            guard = self._require_webhook_secret(callback_query, config, action_label="Taste recompute")
            if guard is not None:
                return guard
            claim = self.taste.claim_recompute(source="telegram")
            if not claim.claimed or claim.job_id is None:
                text = self.telegram_formatter.build_taste_recompute_already_running_message(
                    started_at=claim.running_started_at,
                )
                result = TelegramWebhookResult(
                    action="taste_recompute_already_running",
                    detail=text,
                    recompute_job_id=claim.running_job_id,
                )
                self._edit_or_send_callback_message(callback_query, chat_id=cast(int | str, chat_id), text=text)
                self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
                return result

            text = self.telegram_formatter.build_taste_recompute_started_message(started_at=claim.started_at)
            self._edit_or_send_callback_message(callback_query, chat_id=cast(int | str, chat_id), text=text)

            def task() -> None:
                self._run_taste_recompute_background(job_id=cast(str, claim.job_id), chat_id=cast(int | str, chat_id))

            if schedule_background is None:
                task()
            else:
                schedule_background(task)
            result = TelegramWebhookResult(
                action="taste_recompute_started",
                detail=text,
                recompute_job_id=claim.job_id,
            )
            self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
            return result

        if parsed.action == "skip_drafts":
            text = "Skipped drafted search changes. Run /taste whenever you want to revisit them."
            result = TelegramWebhookResult(
                action="taste_drafts_skipped",
                detail=text,
                profile_version=parsed.profile_version,
            )
            self._edit_or_send_callback_message(
                callback_query,
                chat_id=cast(int | str, chat_id),
                text=text,
                reply_markup={"inline_keyboard": []},
            )
            self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
            return result

        guard = self._require_webhook_secret(callback_query, config, action_label="Applying search drafts")
        if guard is not None:
            return guard
        if self._apply_search_drafts is None:
            result = TelegramWebhookResult(action="invalid_callback", detail="Search draft application is not configured.")
            self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
            return result
        apply_result = self._apply_search_drafts(parsed.profile_version)
        text = self.telegram_formatter.build_search_draft_apply_message(apply_result)
        result = TelegramWebhookResult(
            action="taste_drafts_applied",
            detail=text,
            profile_version=apply_result.profile_version,
            changed_searches=apply_result.applied_searches,
        )
        self._edit_or_send_callback_message(
            callback_query,
            chat_id=cast(int | str, chat_id),
            text=text,
            reply_markup={"inline_keyboard": []},
        )
        self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
        return result

    def _handle_reply_comment(
        self, message: TelegramMessage, comment: str, config: TelegramRuntimeConfig
    ) -> TelegramWebhookResult:
        if not self._is_authorized_chat(self._message_chat_id(message), config):
            return TelegramWebhookResult(
                action="unauthorized",
                detail="Telegram chat is not authorized to comment on alerts.",
            )
        message_id = message.reply_to_message.message_id if message.reply_to_message else None
        if message_id is None:
            return TelegramWebhookResult(
                action="ignored",
                detail="Reply comment did not reference an alert message.",
            )
        # Phase 1 — resolve the alert reply to its candidate + current verdict in a short txn.
        with session_scope() as session:
            delivery = session.scalar(
                select(AlertDeliveryState)
                .where(AlertDeliveryState.telegram_message_id == message_id)
                .limit(1)
            )
            if delivery is None:
                return TelegramWebhookResult(
                    action="ignored",
                    detail="Reply comment did not match any known alert delivery.",
                )
            candidate_model = session.get(Candidate, delivery.candidate_id)
            if candidate_model is None:
                return TelegramWebhookResult(
                    action="ignored",
                    detail="Candidate for this delivery no longer exists.",
                )
            candidate_id = candidate_model.id
            existing_feedback = candidate_model.feedback

        if existing_feedback in {"like", "dislike"}:
            # apply_feedback is three-phase: any VLM observation runs without a held write lock.
            _, snapshot = self.candidates.apply_feedback(
                candidate_id,
                verdict=cast(Literal["like", "dislike"], existing_feedback),
                comment=comment,
            )
            return TelegramWebhookResult(
                action="feedback_recorded",
                detail=f"Saved comment for {existing_feedback} candidate.",
                candidate_id=candidate_id,
                learning_snapshot_id=snapshot.id if snapshot is not None else None,
            )

        # No verdict yet: store a neutral note-only taste sample with durable assets.
        _, snapshot = self.candidates.apply_note(
            candidate_id,
            comment=comment,
        )
        return TelegramWebhookResult(
            action="feedback_recorded",
            detail="Saved note-only candidate feedback.",
            candidate_id=candidate_id,
            learning_snapshot_id=snapshot.id if snapshot is not None else None,
        )

    def handle_webhook(
        self,
        payload: TelegramUpdate,
        schedule_background: Callable[[Callable[[], None]], None] | None = None,
    ) -> TelegramWebhookResult:
        config = _telegram_config()
        self._apply_bot_token(config)
        if payload.message and payload.message.text:
            msg_text = payload.message.text.strip()
            command = msg_text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
            if command == "/taste":
                return self._handle_taste_command(payload.message, config)
            if payload.message.reply_to_message is not None and msg_text:
                return self._handle_reply_comment(payload.message, msg_text, config)

        callback_query = payload.callback_query
        if callback_query is None:
            return TelegramWebhookResult(
                action="ignored",
                detail="Telegram update did not contain a supported callback query.",
            )

        if not callback_query.data:
            result = TelegramWebhookResult(
                action="invalid_callback",
                detail="Telegram callback query did not include callback data.",
            )
            self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
            return result

        if callback_query.data.startswith("taste:"):
            return self._handle_taste_callback(
                callback_query,
                config=config,
                schedule_background=schedule_background,
            )

        if not self._is_authorized_chat(self._message_chat_id(callback_query.message), config):
            result = TelegramWebhookResult(
                action="unauthorized",
                detail="Telegram chat is not authorized to record alert feedback.",
            )
            self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
            return result

        parsed_callback = self.telegram_formatter.parse_feedback_callback_data(callback_query.data)
        if parsed_callback is None:
            result = TelegramWebhookResult(
                action="invalid_callback",
                detail="Telegram callback payload was not recognized.",
            )
            self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
            return result

        try:
            delivery_id = int(parsed_callback.delivery_id)
        except ValueError:
            result = TelegramWebhookResult(
                action="invalid_callback",
                detail="Telegram callback delivery ID was not a valid integer.",
                delivery_id=parsed_callback.delivery_id,
                verdict=parsed_callback.verdict,
            )
            self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
            return result

        verdict = parsed_callback.verdict

        def run() -> TelegramWebhookResult:
            # Recording feedback downloads images and runs a VLM observation; keep that work
            # out of the webhook request so Telegram gets a fast HTTP 200 and does not redeliver.
            result = self._record_feedback_for_delivery(delivery_id=delivery_id, verdict=verdict)
            self._update_feedback_message(callback_query, result)
            self._acknowledge_callback(callback_query.id, self._callback_ack_text(result))
            return result

        if schedule_background is None:
            return run()

        def background() -> None:
            run()

        schedule_background(background)
        return TelegramWebhookResult(
            action="feedback_queued",
            detail=f"Recording your {verdict} feedback…",
            delivery_id=str(delivery_id),
            verdict=verdict,
        )

    def _record_feedback_for_delivery(
        self, *, delivery_id: int, verdict: Literal["like", "dislike"]
    ) -> TelegramWebhookResult:
        # Phase 1 — resolve the delivery to its candidate in a short read transaction.
        with session_scope() as session:
            delivery = session.get(AlertDeliveryState, delivery_id)
            if delivery is None:
                return TelegramWebhookResult(
                    action="invalid_callback",
                    detail="No alert delivery matched this Telegram callback.",
                    delivery_id=str(delivery_id),
                    verdict=verdict,
                )
            candidate_id = delivery.candidate_id

        # Phase 2 — record feedback. apply_feedback is itself three-phase: the image download and
        # VLM observation run with no DB transaction held.
        try:
            candidate_record, snapshot = self.candidates.apply_feedback(
                candidate_id,
                verdict=verdict,
                skip_if_unchanged=True,
            )
        except KeyError:
            return TelegramWebhookResult(
                action="invalid_callback",
                detail="The candidate linked to this Telegram delivery no longer exists.",
                delivery_id=str(delivery_id),
                verdict=verdict,
            )

        # Phase 3 — stamp the delivery in a short write transaction.
        with session_scope() as session:
            delivery = session.get(AlertDeliveryState, delivery_id)
            if delivery is not None:
                delivery.updated_at = datetime.now(UTC)

        return TelegramWebhookResult(
            action="feedback_recorded" if snapshot is not None else "feedback_unchanged",
            detail=(
                f"Recorded Telegram {verdict} feedback and refreshed learning snapshots."
                if snapshot is not None
                else f"Candidate was already marked {verdict}; no new learning snapshot was created."
            ),
            candidate_id=candidate_record.id,
            delivery_id=str(delivery_id),
            verdict=verdict,
            learning_snapshot_id=snapshot.id if snapshot is not None else None,
        )

    def build_test_message(self, candidates: list) -> str:
        if not candidates:
            return "No candidates available yet. Run a search first."
        return self.telegram_formatter.build_alert_message(candidates[0])
