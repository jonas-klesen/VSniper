from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from vsniper.core.config import Settings
from vsniper.core.database import session_scope
from vsniper.db.models import AppSettingsState, ErrorEvent
from vsniper.domain.contracts import (
    ErrorEventPage,
    ErrorEventRecord,
    ErrorNotificationSettings,
    ErrorSource,
)
from vsniper.integrations.telegram.client import TelegramClient
from vsniper.services._mapping import as_aware, is_value_configured

logger = logging.getLogger(__name__)

ERROR_NOTIFICATION_MAX_ATTEMPTS = 3
ERROR_NOTIFICATION_RETRY_DELAYS = (
    timedelta(seconds=0),
    timedelta(seconds=30),
    timedelta(minutes=5),
)
ERROR_NOTIFICATION_PROCESSING_TIMEOUT = timedelta(minutes=10)
_MAX_MESSAGE_CHARS = 4000
_MAX_TRACEBACK_CHARS = 20_000
_MAX_DETAIL_STRING_CHARS = 8000


def _configured_telegram(settings: AppSettingsState) -> bool:
    return is_value_configured(
        settings.telegram_bot_token,
        placeholder="put-your-telegram-bot-token-here",
    ) and is_value_configured(
        settings.telegram_chat_id,
        placeholder="put-your-telegram-chat-id-here",
    )


class ErrorService:
    """Persists terminal operational failures and delivers their optional Telegram notices."""

    def __init__(self, settings: Settings, telegram_client: TelegramClient) -> None:
        self.settings = settings
        self.telegram_client = telegram_client

    @staticmethod
    def _settings_row(session: Session) -> AppSettingsState:
        row = session.get(AppSettingsState, 1)
        if row is None:
            raise RuntimeError("Application settings are not initialized.")
        return row

    def _secret_values(self, session: Session) -> list[str]:
        row = self._settings_row(session)
        values = [
            row.vinted_cookie,
            row.vinted_refresh_token,
            row.telegram_bot_token,
            row.telegram_webhook_secret,
            self.settings.vinted_cookie,
            self.settings.telegram_bot_token,
            self.settings.telegram_webhook_secret,
            self.settings.ai_api_key,
            self.settings.cerebras_api_key,
            self.settings.openrouter_api_key,
        ]
        return sorted(
            {
                value.strip()
                for value in values
                if value
                and len(value.strip()) >= 6
                and not value.strip().startswith("put-your-")
            },
            key=len,
            reverse=True,
        )

    @staticmethod
    def _redact_text(value: str, secrets: list[str], *, limit: int) -> str:
        redacted = value
        for secret in secrets:
            redacted = redacted.replace(secret, "<redacted>")
        if len(redacted) > limit:
            return redacted[: limit - 1].rstrip() + "…"
        return redacted

    def _sanitize(self, value: Any, secrets: list[str]) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str):
            return self._redact_text(value, secrets, limit=_MAX_DETAIL_STRING_CHARS)
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in list(value.items())[:100]:
                normalized_key = str(key)[:128]
                if any(token in normalized_key.lower() for token in ("authorization", "cookie", "secret", "token")):
                    sanitized[normalized_key] = "<redacted>"
                else:
                    sanitized[normalized_key] = self._sanitize(item, secrets)
            return sanitized
        if isinstance(value, (list, tuple, set)):
            return [self._sanitize(item, secrets) for item in list(value)[:100]]
        return self._redact_text(str(value), secrets, limit=_MAX_DETAIL_STRING_CHARS)

    def record(
        self,
        *,
        source: ErrorSource,
        operation: str,
        summary: str,
        message: str | None = None,
        exception: BaseException | None = None,
        details: dict[str, Any] | None = None,
        related_entity_type: str | None = None,
        related_entity_id: str | int | None = None,
        session: Session | None = None,
    ) -> int | None:
        """Best-effort recording that never replaces the original failure."""

        try:
            if session is not None:
                return self._record_in_session(
                    session,
                    source=source,
                    operation=operation,
                    summary=summary,
                    message=message,
                    exception=exception,
                    details=details,
                    related_entity_type=related_entity_type,
                    related_entity_id=related_entity_id,
                )
            with session_scope() as owned_session:
                return self._record_in_session(
                    owned_session,
                    source=source,
                    operation=operation,
                    summary=summary,
                    message=message,
                    exception=exception,
                    details=details,
                    related_entity_type=related_entity_type,
                    related_entity_id=related_entity_id,
                )
        except Exception:
            logger.exception("Could not persist operational error event")
            return None

    def _record_in_session(
        self,
        session: Session,
        *,
        source: ErrorSource,
        operation: str,
        summary: str,
        message: str | None,
        exception: BaseException | None,
        details: dict[str, Any] | None,
        related_entity_type: str | None,
        related_entity_id: str | int | None,
    ) -> int:
        settings = self._settings_row(session)
        secrets = self._secret_values(session)
        effective_message = message if message is not None else str(exception or summary)
        safe_details = self._sanitize(details or {}, secrets)
        if exception is not None:
            retry_count = getattr(exception, "attempt_count", None)
            retry_label = getattr(exception, "retry_label", None)
            status_code = getattr(exception, "status_code", None)
            if retry_count is not None:
                safe_details["retry_attempt_count"] = retry_count
            if retry_label:
                safe_details["retry_label"] = str(retry_label)
            if status_code is not None:
                safe_details["status_code"] = status_code
            formatted = "".join(
                traceback.format_exception(type(exception), exception, exception.__traceback__)
            )
            safe_details["traceback"] = self._redact_text(
                formatted, secrets, limit=_MAX_TRACEBACK_CHARS
            )

        now = datetime.now(UTC)
        event = ErrorEvent(
            occurred_at=now,
            source=source,
            operation=operation[:128],
            summary=self._redact_text(summary, secrets, limit=255),
            message=self._redact_text(effective_message, secrets, limit=_MAX_MESSAGE_CHARS),
            exception_type=type(exception).__name__[:255] if exception is not None else None,
            details=safe_details,
            related_entity_type=related_entity_type[:64] if related_entity_type else None,
            related_entity_id=str(related_entity_id)[:128] if related_entity_id is not None else None,
            telegram_notification_status=(
                "pending" if settings.error_telegram_notifications_enabled else "not_requested"
            ),
            telegram_notification_attempt_count=0,
            updated_at=now,
        )
        session.add(event)
        session.flush()
        return event.id

    @staticmethod
    def _to_record(event: ErrorEvent) -> ErrorEventRecord:
        return ErrorEventRecord(
            id=event.id,
            occurred_at=event.occurred_at,
            source=event.source,  # type: ignore[arg-type]
            operation=event.operation,
            summary=event.summary,
            message=event.message,
            exception_type=event.exception_type,
            details=event.details or {},
            related_entity_type=event.related_entity_type,
            related_entity_id=event.related_entity_id,
            telegram_notification_status=event.telegram_notification_status,  # type: ignore[arg-type]
            telegram_notification_attempt_count=event.telegram_notification_attempt_count,
            telegram_notification_last_attempted_at=event.telegram_notification_last_attempted_at,
            telegram_notification_last_error=event.telegram_notification_last_error,
            telegram_notification_sent_at=event.telegram_notification_sent_at,
        )

    def page(
        self,
        *,
        source: ErrorSource | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ErrorEventPage:
        with session_scope() as session:
            stmt = select(ErrorEvent)
            count_stmt = select(func.count()).select_from(ErrorEvent)
            if source is not None:
                stmt = stmt.where(ErrorEvent.source == source)
                count_stmt = count_stmt.where(ErrorEvent.source == source)
            total = int(session.scalar(count_stmt) or 0)
            rows = session.scalars(
                stmt.order_by(ErrorEvent.occurred_at.desc(), ErrorEvent.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            settings = self._settings_row(session)
            return ErrorEventPage(
                items=[self._to_record(row) for row in rows],
                total=total,
                telegram_notifications_enabled=settings.error_telegram_notifications_enabled,
                telegram_configured=_configured_telegram(settings),
            )

    def update_notification_settings(self, *, enabled: bool) -> ErrorNotificationSettings:
        now = datetime.now(UTC)
        with session_scope() as session:
            settings = self._settings_row(session)
            configured = _configured_telegram(settings)
            if enabled and not configured:
                raise ValueError("Configure a Telegram bot token and chat ID before enabling error notifications.")
            settings.error_telegram_notifications_enabled = enabled
            if not enabled:
                session.execute(
                    update(ErrorEvent)
                    .where(ErrorEvent.telegram_notification_status == "pending")
                    .values(
                        telegram_notification_status="not_requested",
                        telegram_notification_last_error=None,
                        updated_at=now,
                    )
                )
            return ErrorNotificationSettings(enabled=enabled, telegram_configured=configured)

    @staticmethod
    def _retry_delay(attempt_count: int) -> timedelta:
        index = max(0, min(attempt_count, len(ERROR_NOTIFICATION_RETRY_DELAYS) - 1))
        return ERROR_NOTIFICATION_RETRY_DELAYS[index]

    @classmethod
    def _eligible(cls, event: ErrorEvent, *, now: datetime) -> bool:
        if event.telegram_notification_status not in {"pending", "processing"}:
            return False
        if event.telegram_notification_attempt_count >= ERROR_NOTIFICATION_MAX_ATTEMPTS:
            return False
        attempted = as_aware(event.telegram_notification_last_attempted_at)
        if attempted is None:
            return True
        if event.telegram_notification_status == "processing":
            return attempted + ERROR_NOTIFICATION_PROCESSING_TIMEOUT <= now
        return attempted + cls._retry_delay(event.telegram_notification_attempt_count) <= now

    def _claim_pending(self, session: Session, *, limit: int) -> list[int]:
        now = datetime.now(UTC)
        candidates = session.scalars(
            select(ErrorEvent)
            .where(ErrorEvent.telegram_notification_status.in_(("pending", "processing")))
            .order_by(ErrorEvent.occurred_at.asc())
            .limit(max(limit, 1) * 4)
        ).all()
        claimed: list[int] = []
        for event in candidates:
            if len(claimed) >= limit or not self._eligible(event, now=now):
                continue
            attempted_at = datetime.now(UTC)
            result = cast(
                CursorResult,
                session.execute(
                    update(ErrorEvent)
                    .where(ErrorEvent.id == event.id)
                    .where(ErrorEvent.telegram_notification_status == event.telegram_notification_status)
                    .where(
                        ErrorEvent.telegram_notification_attempt_count
                        == event.telegram_notification_attempt_count
                    )
                    .values(
                        telegram_notification_status="processing",
                        telegram_notification_attempt_count=event.telegram_notification_attempt_count + 1,
                        telegram_notification_last_attempted_at=attempted_at,
                        updated_at=attempted_at,
                    )
                ),
            )
            if result.rowcount == 1:
                claimed.append(event.id)
        return claimed

    @staticmethod
    def _notification_text(event: ErrorEvent) -> str:
        related = ""
        if event.related_entity_type and event.related_entity_id:
            related = f"\nRelated: {event.related_entity_type} {event.related_entity_id}"
        message = event.message
        if len(message) > 1200:
            message = message[:1199].rstrip() + "…"
        return (
            f"❌ vsniper error #{event.id}\n"
            f"{event.source} · {event.operation}\n"
            f"{event.summary}\n\n{message}{related}\n"
            f"Occurred: {event.occurred_at.isoformat()}"
        )

    def _send_claimed(self, event_id: int, *, bot_token: str, chat_id: str) -> Literal["sent", "retry", "failed"]:
        with session_scope() as session:
            event = session.get(ErrorEvent, event_id)
            if event is None:
                return "failed"
            text = self._notification_text(event)

        self.telegram_client.bot_token = bot_token
        attempted_at = datetime.now(UTC)
        try:
            self.telegram_client.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            with session_scope() as session:
                event = session.get(ErrorEvent, event_id)
                if event is None:
                    return "failed"
                retryable = getattr(exc, "retryable", False)
                event.telegram_notification_last_error = self._redact_text(
                    str(exc), self._secret_values(session), limit=_MAX_MESSAGE_CHARS
                )
                event.updated_at = attempted_at
                if retryable and event.telegram_notification_attempt_count < ERROR_NOTIFICATION_MAX_ATTEMPTS:
                    event.telegram_notification_status = "pending"
                    return "retry"
                event.telegram_notification_status = "failed"
                return "failed"

        with session_scope() as session:
            event = session.get(ErrorEvent, event_id)
            if event is not None:
                event.telegram_notification_status = "sent"
                event.telegram_notification_last_error = None
                event.telegram_notification_sent_at = attempted_at
                event.updated_at = attempted_at
        return "sent"

    def process_pending_notifications(self, *, limit: int = 25) -> dict[str, int]:
        """Send error alerts without recording failures from this method as new events."""

        with session_scope() as session:
            settings = self._settings_row(session)
            if not settings.error_telegram_notifications_enabled:
                return {"claimed": 0, "sent": 0, "retry": 0, "failed": 0}
            bot_token = settings.telegram_bot_token
            chat_id = settings.telegram_chat_id
            claimed = self._claim_pending(session, limit=limit)

        counts = {"claimed": len(claimed), "sent": 0, "retry": 0, "failed": 0}
        for event_id in claimed:
            outcome = self._send_claimed(event_id, bot_token=bot_token, chat_id=chat_id)
            counts[outcome] += 1
        return counts

    def prune_old_events(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=self.settings.error_retention_days)
        with session_scope() as session:
            result = session.execute(
                delete(ErrorEvent).where(
                    ErrorEvent.occurred_at < cutoff,
                    ErrorEvent.telegram_notification_status.in_(("not_requested", "sent", "failed")),
                )
            )
            return int(result.rowcount or 0)
