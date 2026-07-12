from __future__ import annotations

import json
import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Callable

import httpx
from fastapi.testclient import TestClient

from vsniper.api.main import app
from vsniper.db.models import AlertDeliveryState, AppSettingsState
from vsniper.domain.contracts import (
    SearchDraftApplyResult,
    TasteDirtyCounts,
    TasteManualNote,
    TasteObservationCacheStats,
    TasteRecomputeResult,
    TasteRecomputeState,
    TasteSnapshot,
    TelegramUpdate,
)
from vsniper.integrations.telegram.service import TelegramFormatter
from vsniper.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from vsniper.integrations.vinted.client import VintedClient
from vsniper.services.telegram_service import TelegramRuntimeConfig, TelegramService


def _jwt_with_expiry(expiry: datetime) -> str:
    payload = json.dumps({"exp": int(expiry.timestamp())}, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"header.{encoded}.signature"


def test_telegram_client_sends_message_successfully() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith('/sendMessage')
        payload = json.loads(request.read().decode('utf-8'))
        assert payload['text'] == 'hello from test'
        assert payload['reply_markup']['inline_keyboard'][0][0]['callback_data'] == 'feedback:1:like'
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    client = TelegramClient(
        bot_token='bot-token',
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.send_message(
        chat_id='chat-123',
        text='hello from test',
        reply_markup={
            'inline_keyboard': [[{'text': '👍 Like', 'callback_data': 'feedback:1:like'}]],
        },
    )

    assert result == {"message_id": 42}


def test_telegram_client_answers_callback_queries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith('/answerCallbackQuery')
        payload = json.loads(request.read().decode('utf-8'))
        assert payload == {'callback_query_id': 'callback-1', 'text': 'Saved like feedback.'}
        return httpx.Response(200, json={"ok": True, "result": True})

    client = TelegramClient(
        bot_token='bot-token',
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.answer_callback_query(callback_query_id='callback-1', text='Saved like feedback.')

    assert result == {'result': True}


def test_telegram_client_gets_and_sets_webhook_info() -> None:
    requests: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        payload = json.loads(body.decode('utf-8')) if body else None
        requests.append((request.method, request.url.path, payload))
        if request.url.path.endswith('/getWebhookInfo'):
            return httpx.Response(200, json={"ok": True, "result": {"url": "https://example.com/api/telegram/webhook", "pending_update_count": 0}})
        if request.url.path.endswith('/setWebhook'):
            return httpx.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f'unexpected path {request.url.path}')

    client = TelegramClient(
        bot_token='bot-token',
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    info = client.get_webhook_info()
    result = client.set_webhook(
        url='https://example.com/api/telegram/webhook',
        secret_token='secret-token',
        allowed_updates=['callback_query'],
        drop_pending_updates=True,
    )

    assert info['url'] == 'https://example.com/api/telegram/webhook'
    assert result == {'result': True}
    assert requests[0][0] == 'GET'
    assert requests[0][1].endswith('/getWebhookInfo')
    assert requests[1][0] == 'POST'
    assert requests[1][2] == {
        'url': 'https://example.com/api/telegram/webhook',
        'allowed_updates': ['callback_query'],
        'drop_pending_updates': True,
        'secret_token': 'secret-token',
    }


def test_telegram_client_edits_messages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith('/editMessageText')
        payload = json.loads(request.read().decode('utf-8'))
        assert payload == {
            'chat_id': 523,
            'message_id': 77,
            'text': 'alert body\n\nFeedback: 👍 Like',
            'disable_web_page_preview': False,
            'reply_markup': {'inline_keyboard': []},
        }
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    client = TelegramClient(
        bot_token='bot-token',
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.edit_message_text(
        chat_id=523,
        message_id=77,
        text='alert body\n\nFeedback: 👍 Like',
        reply_markup={'inline_keyboard': []},
    )

    assert result == {'message_id': 77}


def _build_telegram_service_without_init() -> TelegramService:
    """Construct a TelegramService bypassing __init__ so tests can inject fakes."""
    return TelegramService.__new__(TelegramService)


def test_pending_deliveries_respects_limit(tmp_path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from vsniper.core.database import Base
    from vsniper.db.models import Candidate, Search

    engine = create_engine(f"sqlite:///{tmp_path / 'pending.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    with factory() as session:
        session.add(Search(id="s1", name="S", clothing_item="hosen", query="q", region="de", enabled=True))
        for i in range(10):
            session.add(Candidate(
                id=f"c{i}", clothing_item="hosen", search_id="s1", title="t", brand="b",
                price_eur=1.0, size="M", url="http://x", decision="alert", final_score=8.0,
            ))
            session.add(AlertDeliveryState(
                candidate_id=f"c{i}", channel="telegram", status="pending",
                attempt_count=0, last_attempted_at=None,
            ))
        session.commit()

    service = _build_telegram_service_without_init()
    with factory() as session:
        assert len(service._pending_deliveries(session, limit=4)) == 4
        assert len(service._pending_deliveries(session)) == 10
        assert service._count_pending_deliveries(session) == 10


def test_vinted_refresh_token_expiry_uses_refresh_token_not_access_cookie() -> None:
    now = datetime.now(UTC)
    access_token = _jwt_with_expiry(now + timedelta(hours=1))
    refresh_token = _jwt_with_expiry(now + timedelta(days=30))
    client = VintedClient()
    client.set_cookie(f"access_token_web={access_token}; refresh_token_web={refresh_token}")

    assert client.get_cookie_expiry() is not None
    assert client.get_cookie_expiry() < now + timedelta(hours=2)
    assert client.get_refresh_token_expiry() is not None
    assert client.get_refresh_token_expiry() > now + timedelta(days=29)


def test_refresh_token_expiry_warning_sends_once_and_persists_marker(monkeypatch) -> None:
    expiry = datetime.now(UTC) + timedelta(hours=2)
    settings_row = SimpleNamespace(
        telegram_bot_token='bot-token',
        telegram_chat_id='chat-123',
        telegram_webhook_url='',
        telegram_webhook_secret='',
        session_health={},
        refresh_token_expiry_warning_sent_for=None,
    )
    sent_messages: list[dict[str, str]] = []

    service = _build_telegram_service_without_init()
    service.vinted_client = SimpleNamespace(get_refresh_token_expiry=lambda: expiry)
    service.telegram_client = SimpleNamespace(
        bot_token='',
        send_message=lambda **kwargs: sent_messages.append(kwargs),
    )

    class FakeSession:
        def get(self, model, key):
            if model is AppSettingsState and key == 1:
                return settings_row
            return None

    @contextmanager
    def fake_session_scope():
        yield FakeSession()

    monkeypatch.setattr('vsniper.services.telegram_service.session_scope', fake_session_scope)

    service.check_refresh_token_expiry()
    service.check_refresh_token_expiry()

    assert len(sent_messages) == 1
    assert sent_messages[0]['chat_id'] == 'chat-123'
    assert 'refresh token expires soon' in sent_messages[0]['text']
    assert '24 hours' not in sent_messages[0]['text']
    assert '12 hours' not in sent_messages[0]['text']
    assert '6 hours' not in sent_messages[0]['text']
    assert settings_row.refresh_token_expiry_warning_sent_for == expiry.isoformat()
    assert settings_row.session_health == {}


def test_refresh_token_expiry_warning_ignores_far_future_refresh_token(monkeypatch) -> None:
    settings_row = SimpleNamespace(
        telegram_bot_token='bot-token',
        telegram_chat_id='chat-123',
        telegram_webhook_url='',
        telegram_webhook_secret='',
        session_health={},
    )
    sent_messages: list[dict[str, str]] = []

    service = _build_telegram_service_without_init()
    service.vinted_client = SimpleNamespace(get_refresh_token_expiry=lambda: datetime.now(UTC) + timedelta(days=30))
    service.telegram_client = SimpleNamespace(
        bot_token='',
        send_message=lambda **kwargs: sent_messages.append(kwargs),
    )

    class FakeSession:
        def get(self, model, key):
            if model is AppSettingsState and key == 1:
                return settings_row
            return None

    @contextmanager
    def fake_session_scope():
        yield FakeSession()

    monkeypatch.setattr('vsniper.services.telegram_service.session_scope', fake_session_scope)

    service.check_refresh_token_expiry()

    assert sent_messages == []
    assert settings_row.session_health == {}


def test_is_eligible_handles_naive_last_attempted_at() -> None:
    # SQLite returns DateTime(timezone=True) columns as naive datetimes; comparing
    # them against datetime.now(UTC) must not raise a TypeError (regression: a single
    # failed delivery would otherwise crash the worker loop on every pass).
    now = datetime.now(UTC)
    delivery = AlertDeliveryState(
        id=1,
        candidate_id='cand-1',
        channel='telegram',
        status='pending',
        attempt_count=1,
        last_error='rate limited',
        payload_preview='queued body',
        created_at=now.replace(tzinfo=None),
        updated_at=now.replace(tzinfo=None),
        last_attempted_at=(now - timedelta(days=1)).replace(tzinfo=None),
        sent_at=None,
    )

    assert TelegramService._is_eligible(delivery, now=now) is True

    delivery.last_attempted_at = now.replace(tzinfo=None)
    assert TelegramService._is_eligible(delivery, now=now) is False


def test_process_pending_deliveries_retries_retryable_errors(monkeypatch) -> None:
    service = _build_telegram_service_without_init()
    service.settings = SimpleNamespace(telegram_chat_id='chat-123')
    service.telegram_formatter = SimpleNamespace(
        build_alert_message=lambda candidate: 'alert body',
        build_feedback_reply_markup=lambda delivery_id: {'inline_keyboard': [[{'callback_data': f'feedback:{delivery_id}:like'}]]},
    )

    def fail_send_message(*, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        raise TelegramDeliveryError('rate limited', retryable=True, status_code=429)

    service.telegram_client = SimpleNamespace(send_message=fail_send_message)

    candidate = SimpleNamespace(
        id='cand-1',
        feedback='unknown',
        alert_deliveries=[],
    )
    delivery = AlertDeliveryState(
        id=1,
        candidate_id='cand-1',
        channel='telegram',
        status='pending',
        attempt_count=0,
        last_error=None,
        payload_preview='queued body',
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        last_attempted_at=None,
        sent_at=None,
    )

    class FakeSession:
        def get(self, model, key):
            if model is AppSettingsState:
                return SimpleNamespace(
                    telegram_bot_token='bot-token',
                    telegram_chat_id='chat-123',
                    telegram_webhook_url='',
                    telegram_webhook_secret='',
                )
            if model is AlertDeliveryState and key == delivery.id:
                return delivery
            if key == 'cand-1':
                return candidate
            return None

    @contextmanager
    def fake_session_scope():
        yield FakeSession()

    monkeypatch.setattr('vsniper.services.telegram_service.session_scope', fake_session_scope)
    monkeypatch.setattr('vsniper.services.telegram_service.candidate_to_contract', lambda candidate: SimpleNamespace(id='cand-1'))
    monkeypatch.setattr(TelegramService, '_count_pending_deliveries', lambda self, session: 1)

    def fake_claim(self, session, *, limit: int):
        delivery.status = 'processing'
        delivery.attempt_count += 1
        delivery.last_attempted_at = datetime.now(UTC)
        return [delivery]

    monkeypatch.setattr(TelegramService, '_claim_pending_deliveries', fake_claim)

    result = service.process_pending_deliveries(limit=10)

    assert result.eligible_deliveries == 1
    assert result.processed_deliveries == 1
    assert result.retry_scheduled_deliveries == 1
    assert result.failed_deliveries == 0
    assert delivery.status == 'pending'
    assert delivery.attempt_count == 1
    assert delivery.last_error == 'rate limited'


def test_process_pending_deliveries_marks_successful_sends(monkeypatch) -> None:
    service = _build_telegram_service_without_init()
    service.settings = SimpleNamespace(telegram_chat_id='chat-123')
    service.telegram_formatter = SimpleNamespace(
        build_alert_message=lambda candidate: 'alert body',
        build_feedback_reply_markup=lambda delivery_id: {
            'inline_keyboard': [[
                {'text': '👍 Like', 'callback_data': f'feedback:{delivery_id}:like'},
                {'text': '👎 Dislike', 'callback_data': f'feedback:{delivery_id}:dislike'},
            ]],
        },
    )
    send_calls: list[dict] = []

    def send_message(**kwargs):
        send_calls.append(kwargs)
        return {"message_id": 99}

    service.telegram_client = SimpleNamespace(send_message=send_message)

    candidate = SimpleNamespace(
        id='cand-2',
        feedback='unknown',
        alert_deliveries=[],
    )
    delivery = AlertDeliveryState(
        id=2,
        candidate_id='cand-2',
        channel='telegram',
        status='pending',
        attempt_count=0,
        last_error=None,
        payload_preview='queued body',
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        last_attempted_at=None,
        sent_at=None,
    )

    class FakeSession:
        def get(self, model, key):
            if model is AppSettingsState:
                return SimpleNamespace(
                    telegram_bot_token='bot-token',
                    telegram_chat_id='chat-123',
                    telegram_webhook_url='',
                    telegram_webhook_secret='',
                )
            if model is AlertDeliveryState and key == delivery.id:
                return delivery
            if key == 'cand-2':
                return candidate
            return None

    @contextmanager
    def fake_session_scope():
        yield FakeSession()

    monkeypatch.setattr('vsniper.services.telegram_service.session_scope', fake_session_scope)
    monkeypatch.setattr('vsniper.services.telegram_service.candidate_to_contract', lambda candidate: SimpleNamespace(id='cand-2'))
    monkeypatch.setattr(TelegramService, '_count_pending_deliveries', lambda self, session: 1)

    def fake_claim(self, session, *, limit: int):
        delivery.status = 'processing'
        delivery.attempt_count += 1
        delivery.last_attempted_at = datetime.now(UTC)
        return [delivery]

    monkeypatch.setattr(TelegramService, '_claim_pending_deliveries', fake_claim)

    result = service.process_pending_deliveries(limit=10)

    assert result.sent_deliveries == 1
    assert result.retry_scheduled_deliveries == 0
    assert result.failed_deliveries == 0
    assert delivery.status == 'sent'
    assert delivery.attempt_count == 1
    assert delivery.sent_at is not None
    assert send_calls[0]['reply_markup']['inline_keyboard'][0][0]['callback_data'] == 'feedback:2:like'
    assert send_calls[0]['reply_markup']['inline_keyboard'][0][1]['callback_data'] == 'feedback:2:dislike'


def test_process_pending_deliveries_writes_back_each_delivery_independently(monkeypatch) -> None:
    # The first send succeeds and the second fails non-retryably within the same cycle;
    # each delivery's outcome must be recorded independently (per-delivery write-back),
    # not lost in an all-or-nothing transaction.
    service = _build_telegram_service_without_init()
    service.settings = SimpleNamespace(telegram_chat_id='chat-123')
    service.telegram_formatter = SimpleNamespace(
        build_alert_message=lambda candidate: 'alert body',
        build_feedback_reply_markup=lambda delivery_id: {'inline_keyboard': []},
    )

    def send_message(**kwargs):
        if 'feedback' in kwargs['text']:  # pragma: no cover - defensive
            raise AssertionError('unexpected text')
        if kwargs['chat_id'] == 'chat-123' and send_message.calls == 1:
            send_message.calls += 1
            raise TelegramDeliveryError('hard failure', retryable=False, status_code=400)
        send_message.calls += 1
        return {"message_id": 99}

    send_message.calls = 0
    service.telegram_client = SimpleNamespace(send_message=send_message)

    candidates = {
        'cand-a': SimpleNamespace(id='cand-a', feedback='unknown', alert_deliveries=[]),
        'cand-b': SimpleNamespace(id='cand-b', feedback='unknown', alert_deliveries=[]),
    }
    now = datetime.now(UTC)
    deliveries = {
        10: AlertDeliveryState(
            id=10, candidate_id='cand-a', channel='telegram', status='pending',
            attempt_count=0, last_error=None, payload_preview='queued', created_at=now,
            updated_at=now, last_attempted_at=None, sent_at=None,
        ),
        11: AlertDeliveryState(
            id=11, candidate_id='cand-b', channel='telegram', status='pending',
            attempt_count=0, last_error=None, payload_preview='queued', created_at=now,
            updated_at=now, last_attempted_at=None, sent_at=None,
        ),
    }

    class FakeSession:
        def get(self, model, key):
            if model is AppSettingsState:
                return SimpleNamespace(
                    telegram_bot_token='bot-token',
                    telegram_chat_id='chat-123',
                    telegram_webhook_url='',
                    telegram_webhook_secret='',
                )
            if model is AlertDeliveryState:
                return deliveries.get(key)
            return candidates.get(key)

    @contextmanager
    def fake_session_scope():
        yield FakeSession()

    monkeypatch.setattr('vsniper.services.telegram_service.session_scope', fake_session_scope)
    monkeypatch.setattr(
        'vsniper.services.telegram_service.candidate_to_contract',
        lambda candidate: SimpleNamespace(id=candidate.id),
    )
    monkeypatch.setattr(TelegramService, '_count_pending_deliveries', lambda self, session: 2)

    def fake_claim(self, session, *, limit: int):
        claimed = []
        for delivery in deliveries.values():
            delivery.status = 'processing'
            delivery.attempt_count += 1
            delivery.last_attempted_at = datetime.now(UTC)
            claimed.append(delivery)
        return claimed

    monkeypatch.setattr(TelegramService, '_claim_pending_deliveries', fake_claim)

    result = service.process_pending_deliveries(limit=10)

    assert result.processed_deliveries == 2
    assert result.sent_deliveries == 1
    assert result.failed_deliveries == 1
    assert result.retry_scheduled_deliveries == 0
    assert deliveries[10].status == 'sent'
    assert deliveries[10].sent_at is not None
    assert deliveries[11].status == 'failed'
    assert deliveries[11].last_error == 'hard failure'


def test_handle_telegram_webhook_records_feedback_from_delivery_callback(monkeypatch) -> None:
    service = _build_telegram_service_without_init()
    acknowledgements: list[dict[str, str]] = []
    edits: list[dict[str, object]] = []
    service.telegram_client = SimpleNamespace(
        answer_callback_query=lambda **kwargs: acknowledgements.append(kwargs),
        edit_message_text=lambda **kwargs: edits.append(kwargs),
    )
    service.telegram_formatter = TelegramFormatter()

    candidate = SimpleNamespace(id='cand-3', feedback='unknown')
    delivery = AlertDeliveryState(
        id=3,
        candidate_id='cand-3',
        channel='telegram',
        status='sent',
        attempt_count=1,
        last_error=None,
        payload_preview='queued body',
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        last_attempted_at=datetime.now(UTC),
        sent_at=datetime.now(UTC),
    )

    class FakeSession:
        def get(self, model, key):
            if key == 3:
                return delivery
            if key == 'cand-3':
                return candidate
            return None

        def flush(self):
            return None

        def refresh(self, model):
            return None

    @contextmanager
    def fake_session_scope():
        yield FakeSession()

    def fake_apply_feedback(candidate_id, *, verdict: str, comment: str = '', skip_if_unchanged: bool = False):
        candidate.feedback = verdict
        return SimpleNamespace(id='cand-3'), SimpleNamespace(id='snapshot-1')

    service.candidates = SimpleNamespace(apply_feedback=fake_apply_feedback)

    monkeypatch.setattr('vsniper.services.telegram_service.session_scope', fake_session_scope)
    monkeypatch.setattr(
        'vsniper.services.telegram_service._telegram_config',
        lambda: TelegramRuntimeConfig('bot-token', '523', None, None),
    )

    result = service.handle_webhook(
        TelegramUpdate.model_validate(
            {
                'update_id': 1001,
                'callback_query': {
                    'id': 'callback-telegram-1',
                    'data': 'feedback:3:like',
                    'message': {
                        'message_id': 77,
                        'text': 'queued body',
                        'chat': {'id': 523},
                    },
                },
            }
        )
    )

    assert result.action == 'feedback_recorded'
    assert result.candidate_id == 'cand-3'
    assert result.delivery_id == '3'
    assert result.verdict == 'like'
    assert result.learning_snapshot_id == 'snapshot-1'
    assert edits == [
        {
            'chat_id': 523,
            'message_id': 77,
            'text': 'queued body\n\nFeedback: 👍 Like\n↩ Reply to add a note (feeds recompute)',
            'reply_markup': {'inline_keyboard': []},
        },
    ]
    assert acknowledgements == [
        {'callback_query_id': 'callback-telegram-1', 'text': 'Saved like feedback.'},
    ]


def test_handle_telegram_webhook_rejects_feedback_from_unauthorized_chat(monkeypatch) -> None:
    service = _build_telegram_service_without_init()
    acknowledgements: list[dict[str, str]] = []
    edits: list[dict[str, object]] = []
    service.telegram_client = SimpleNamespace(
        answer_callback_query=lambda **kwargs: acknowledgements.append(kwargs),
        edit_message_text=lambda **kwargs: edits.append(kwargs),
    )
    service.telegram_formatter = TelegramFormatter()

    record_calls: list[dict[str, object]] = []

    def fake_record_feedback_in_session(session, **kwargs):
        record_calls.append(kwargs)
        return SimpleNamespace(id='cand-3'), SimpleNamespace(id='snapshot-1')

    service.candidates = SimpleNamespace(record_feedback_in_session=fake_record_feedback_in_session)

    @contextmanager
    def fake_session_scope():
        raise AssertionError('session_scope must not be opened for an unauthorized chat')
        yield

    monkeypatch.setattr('vsniper.services.telegram_service.session_scope', fake_session_scope)
    monkeypatch.setattr(
        'vsniper.services.telegram_service._telegram_config',
        lambda: TelegramRuntimeConfig('bot-token', '523', None, None),
    )

    result = service.handle_webhook(
        TelegramUpdate.model_validate(
            {
                'update_id': 1002,
                'callback_query': {
                    'id': 'callback-forged-1',
                    'data': 'feedback:3:like',
                    'message': {
                        'message_id': 77,
                        'text': 'queued body',
                        'chat': {'id': 999},
                    },
                },
            }
        )
    )

    assert result.action == 'unauthorized'
    assert record_calls == []
    assert edits == []
    assert acknowledgements == [
        {
            'callback_query_id': 'callback-forged-1',
            'text': 'Telegram chat is not authorized to record alert feedback.',
        },
    ]


def test_taste_callback_data_round_trips() -> None:
    formatter = TelegramFormatter()

    recompute = formatter.parse_taste_callback_data('taste:recompute')
    apply = formatter.parse_taste_callback_data('taste:apply_drafts:7')
    skip = formatter.parse_taste_callback_data('taste:skip_drafts:7')

    assert recompute is not None and recompute.action == 'recompute'
    assert apply is not None and apply.action == 'apply_drafts' and apply.profile_version == 7
    assert skip is not None and skip.action == 'skip_drafts' and skip.profile_version == 7
    assert formatter.parse_taste_callback_data('taste:apply_drafts:not-int') is None


def test_taste_command_sends_status_for_authorized_chat(monkeypatch) -> None:
    service = _build_telegram_service_without_init()
    sent_messages: list[dict] = []
    service.telegram_client = SimpleNamespace(bot_token='', send_message=lambda **kwargs: sent_messages.append(kwargs))
    service.telegram_formatter = TelegramFormatter()
    service.taste = SimpleNamespace(
        get_snapshot=lambda: TasteSnapshot(
            samples=[],
            manual_note=TasteManualNote(),
            dirty_counts=TasteDirtyCounts(
                new_or_changed_samples=3,
                new_or_changed_positive_samples=2,
                new_or_changed_negative_samples=1,
            ),
            recompute_state=TasteRecomputeState(status='idle'),
        )
    )

    monkeypatch.setattr(
        'vsniper.services.telegram_service._telegram_config',
        lambda: TelegramRuntimeConfig('bot-token', '523', None, None),
    )

    result = service.handle_webhook(
        TelegramUpdate.model_validate(
            {'update_id': 1, 'message': {'message_id': 10, 'text': '/taste', 'chat': {'id': 523}}}
        )
    )

    assert result.action == 'taste_status_sent'
    assert sent_messages[0]['chat_id'] == '523'
    assert '3 changed' in sent_messages[0]['text']
    assert sent_messages[0]['reply_markup']['inline_keyboard'][0][0]['callback_data'] == 'taste:recompute'


def test_taste_recompute_callback_schedules_background_job(monkeypatch) -> None:
    service = _build_telegram_service_without_init()
    edits: list[dict] = []
    acknowledgements: list[dict] = []
    scheduled: list[Callable[[], None]] = []
    started_at = datetime.now(UTC)
    service.telegram_client = SimpleNamespace(
        bot_token='',
        edit_message_text=lambda **kwargs: edits.append(kwargs),
        send_message=lambda **kwargs: None,
        answer_callback_query=lambda **kwargs: acknowledgements.append(kwargs),
    )
    service.telegram_formatter = TelegramFormatter()
    service.taste = SimpleNamespace(
        claim_recompute=lambda source: SimpleNamespace(
            claimed=True,
            job_id='taste-job-1',
            started_at=started_at,
            running_job_id=None,
            running_started_at=None,
        )
    )

    monkeypatch.setattr(
        'vsniper.services.telegram_service._telegram_config',
        lambda: TelegramRuntimeConfig('bot-token', '523', None, 'hook-secret'),
    )

    result = service.handle_webhook(
        TelegramUpdate.model_validate(
            {
                'update_id': 1,
                'callback_query': {
                    'id': 'cb-taste-1',
                    'data': 'taste:recompute',
                    'message': {'message_id': 77, 'text': 'taste status', 'chat': {'id': 523}},
                },
            }
        ),
        schedule_background=lambda task: scheduled.append(task),
    )

    assert result.action == 'taste_recompute_started'
    assert result.recompute_job_id == 'taste-job-1'
    assert len(scheduled) == 1
    assert 'started' in edits[0]['text']
    assert acknowledgements == [{'callback_query_id': 'cb-taste-1', 'text': 'Taste recompute started.'}]


def test_taste_recompute_callback_detects_already_running(monkeypatch) -> None:
    service = _build_telegram_service_without_init()
    edits: list[dict] = []
    acknowledgements: list[dict] = []
    running_since = datetime.now(UTC) - timedelta(minutes=5)
    service.telegram_client = SimpleNamespace(
        bot_token='',
        edit_message_text=lambda **kwargs: edits.append(kwargs),
        send_message=lambda **kwargs: None,
        answer_callback_query=lambda **kwargs: acknowledgements.append(kwargs),
    )
    service.telegram_formatter = TelegramFormatter()
    service.taste = SimpleNamespace(
        claim_recompute=lambda source: SimpleNamespace(
            claimed=False,
            job_id=None,
            started_at=None,
            running_job_id='taste-job-running',
            running_started_at=running_since,
        )
    )

    monkeypatch.setattr(
        'vsniper.services.telegram_service._telegram_config',
        lambda: TelegramRuntimeConfig('bot-token', '523', None, 'hook-secret'),
    )

    result = service.handle_webhook(
        TelegramUpdate.model_validate(
            {
                'update_id': 1,
                'callback_query': {
                    'id': 'cb-taste-2',
                    'data': 'taste:recompute',
                    'message': {'message_id': 77, 'text': 'taste status', 'chat': {'id': 523}},
                },
            }
        ),
        schedule_background=lambda task: (_ for _ in ()).throw(AssertionError('should not schedule')),
    )

    assert result.action == 'taste_recompute_already_running'
    assert result.recompute_job_id == 'taste-job-running'
    assert 'already running' in edits[0]['text']
    assert acknowledgements == [{'callback_query_id': 'cb-taste-2', 'text': 'Taste recompute is already running.'}]


def test_taste_apply_drafts_callback_confirms_result(monkeypatch) -> None:
    service = _build_telegram_service_without_init()
    edits: list[dict] = []
    acknowledgements: list[dict] = []
    service.telegram_client = SimpleNamespace(
        bot_token='',
        edit_message_text=lambda **kwargs: edits.append(kwargs),
        send_message=lambda **kwargs: None,
        answer_callback_query=lambda **kwargs: acknowledgements.append(kwargs),
    )
    service.telegram_formatter = TelegramFormatter()
    service._apply_search_drafts = lambda version: SearchDraftApplyResult(
        profile_version=version,
        requested_profile_version=version,
        applied_searches=6,
        summary='Applied generated drafts from taste profile v4.',
    )

    monkeypatch.setattr(
        'vsniper.services.telegram_service._telegram_config',
        lambda: TelegramRuntimeConfig('bot-token', '523', None, 'hook-secret'),
    )

    result = service.handle_webhook(
        TelegramUpdate.model_validate(
            {
                'update_id': 1,
                'callback_query': {
                    'id': 'cb-taste-3',
                    'data': 'taste:apply_drafts:4',
                    'message': {'message_id': 77, 'text': 'recompute done', 'chat': {'id': 523}},
                },
            }
        )
    )

    assert result.action == 'taste_drafts_applied'
    assert result.changed_searches == 6
    assert 'Changed: 6' in edits[0]['text']
    assert edits[0]['reply_markup'] == {'inline_keyboard': []}
    assert acknowledgements == [{'callback_query_id': 'cb-taste-3', 'text': 'Draft decision saved.'}]


def test_taste_recompute_callback_blocked_without_webhook_secret(monkeypatch) -> None:
    service = _build_telegram_service_without_init()
    acknowledgements: list[dict] = []
    service.telegram_client = SimpleNamespace(
        bot_token='',
        edit_message_text=lambda **kwargs: None,
        send_message=lambda **kwargs: None,
        answer_callback_query=lambda **kwargs: acknowledgements.append(kwargs),
    )
    service.telegram_formatter = TelegramFormatter()

    def _must_not_claim(source):
        raise AssertionError('recompute must not be claimed without a configured webhook secret')

    service.taste = SimpleNamespace(claim_recompute=_must_not_claim)

    monkeypatch.setattr(
        'vsniper.services.telegram_service._telegram_config',
        lambda: TelegramRuntimeConfig('bot-token', '523', None, None),
    )

    result = service.handle_webhook(
        TelegramUpdate.model_validate(
            {
                'update_id': 1,
                'callback_query': {
                    'id': 'cb-taste-nosecret',
                    'data': 'taste:recompute',
                    'message': {'message_id': 77, 'text': 'taste status', 'chat': {'id': 523}},
                },
            }
        ),
        schedule_background=lambda task: (_ for _ in ()).throw(AssertionError('should not schedule')),
    )

    assert result.action == 'unauthorized'
    assert 'TELEGRAM_WEBHOOK_SECRET' in result.detail
    assert acknowledgements[0]['callback_query_id'] == 'cb-taste-nosecret'


def test_taste_recompute_completion_message_includes_cost_and_cache_stats() -> None:
    message = TelegramFormatter().build_taste_recompute_success_message(
        TasteRecomputeResult(
            snapshot=TasteSnapshot(recompute_state=TasteRecomputeState(status='succeeded')),
            cost_usd=0.1234,
            input_tokens=100,
            output_tokens=20,
            observation_cache=TasteObservationCacheStats(cached_observations=4, fresh_observations=1),
        )
    )

    assert 'Cost: $0.1234' in message
    assert 'Tokens: 100 in / 20 out' in message
    assert 'Observation cache: 4 cached / 1 fresh' in message


def test_telegram_webhook_route_smoke(monkeypatch) -> None:
    fake_telegram = SimpleNamespace(
        is_webhook_secret_valid=lambda provided: provided == 'secret-token',
        handle_webhook=lambda payload: {
            'ok': True,
            'action': 'feedback_recorded',
            'detail': 'Recorded Telegram like feedback and refreshed learning snapshots.',
            'candidate_id': 'cand-smoke',
            'delivery_id': '123',
            'verdict': 'like',
            'learning_snapshot_id': 'snapshot-smoke',
        },
    )
    fake_state = SimpleNamespace(telegram=fake_telegram)

    monkeypatch.setattr('vsniper.api.routes.telegram.get_state', lambda: fake_state)

    client = TestClient(app)
    response = client.post(
        '/api/telegram/webhook',
        headers={'X-Telegram-Bot-Api-Secret-Token': 'secret-token'},
        json={
            'update_id': 1,
            'callback_query': {
                'id': 'callback-route-1',
                'data': 'feedback:123:like',
                'message': {'message_id': 7, 'text': 'alert body', 'chat': {'id': 1}},
            },
        },
    )

    assert response.status_code == 200
    assert response.json()['action'] == 'feedback_recorded'
