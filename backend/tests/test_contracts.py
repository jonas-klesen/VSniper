from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from vsniper.core.database import Base
from vsniper.db.models import AiModelConfig, AppSettingsState
from vsniper.domain.contracts import SearchUpdate, SettingsUpdate
from vsniper.services._mapping import settings_to_contract


def test_search_update_only_accepts_de_region() -> None:
    payload = {
        "name": "Search",
        "clothing_item": "hosen",
        "query": "vintage",
        "region": "fr",
        "filters": [],
    }

    with pytest.raises(ValidationError, match="Only the de Vinted region is supported"):
        SearchUpdate.model_validate(payload)


def test_settings_update_only_accepts_de_region() -> None:
    payload = {
        "vinted_region": "fr",
    }

    with pytest.raises(ValidationError, match="Only the de Vinted region is supported"):
        SettingsUpdate.model_validate(payload)


def test_settings_update_rejects_invalid_vlm_parallel_requests() -> None:
    payload = {
        "vinted_region": "de",
        "vlm_judge_parallel_requests": 0,
    }

    with pytest.raises(ValidationError, match="VLM parallel requests must be between 1 and 16"):
        SettingsUpdate.model_validate(payload)


def test_settings_update_rejects_invalid_vlm_grid_size() -> None:
    payload = {
        "vinted_region": "de",
        "vlm_grid_size": 6,
    }

    with pytest.raises(ValidationError, match="VLM grid size must be 1, 4, or 9"):
        SettingsUpdate.model_validate(payload)


def test_settings_update_accepts_single_image_vlm_grid_size() -> None:
    payload = {
        "vinted_region": "de",
        "vlm_grid_size": 1,
    }

    assert SettingsUpdate.model_validate(payload).vlm_grid_size == 1


def test_settings_update_accepts_single_parallel_request() -> None:
    payload = {
        "vinted_region": "de",
        "vlm_judge_parallel_requests": 1,
    }

    assert SettingsUpdate.model_validate(payload).vlm_judge_parallel_requests == 1


def test_settings_update_accepts_multi_image_tile_toggle() -> None:
    payload = {
        "vinted_region": "de",
        "vlm_pack_multiple_listing_images": False,
    }

    assert SettingsUpdate.model_validate(payload).vlm_pack_multiple_listing_images is False


def test_settings_update_rejects_invalid_alert_threshold() -> None:
    payload = {
        "vinted_region": "de",
        "alert_threshold": 101,
    }

    with pytest.raises(ValidationError, match="Alert threshold must be between 1 and 100"):
        SettingsUpdate.model_validate(payload)


def test_search_update_rejects_invalid_alert_threshold() -> None:
    payload = {
        "clothing_item": "hosen",
        "query": "vintage",
        "region": "de",
        "filters": [],
        "alert_threshold": 0,
    }

    with pytest.raises(ValidationError, match="Alert threshold must be between 1 and 100"):
        SearchUpdate.model_validate(payload)


def test_settings_snapshot_splits_local_judge_from_openai_learning(monkeypatch) -> None:
    monkeypatch.setattr(
        "vsniper.services._mapping.get_settings",
        lambda: SimpleNamespace(
            vinted_cookie="put-your-vinted-cookie-here",
            telegram_bot_token="put-your-telegram-bot-token-here",
            telegram_chat_id="put-your-telegram-chat-id-here",
            telegram_webhook_url="",
            telegram_webhook_secret="",
            ai_api_key="put-your-ai-key-here",
            cerebras_api_key="put-your-cerebras-api-key-here",
            openrouter_api_key="put-your-openrouter-api-key-here",
        ),
    )

    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    now = datetime.now(UTC)

    with factory() as session:
        judge_model = AiModelConfig(
            id="model-judge",
            provider="local",
            model_name="local-model",
            reasoning_effort="low",
            local_base_url="http://127.0.0.1:8080/v1",
            display_name="local-model (Local) · low",
            created_at=now,
            updated_at=now,
        )
        session.add(judge_model)
        model = AppSettingsState(
            id=1,
            vinted_region="de",
            vinted_cookie="",
            vinted_refresh_token="",
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_webhook_url="",
            telegram_webhook_secret="",
            telegram_configured=False,
            # No learn_model_id set, so learning (and therefore overall ai_configured) stays
            # unconfigured even though the local judge model is fully set up.
            judge_model_id="model-judge",
            judge_fallback_model_id=None,
            learn_model_id=None,
            observation_model_id=None,
            vlm_grid_size=1,
            vlm_pack_multiple_listing_images=True,
            vlm_judge_parallel_requests=1,
            ai_judge_image_max_px=512,
            alert_threshold=95,
            session_health={
                "region": "de",
                "status": "missing",
                "last_validated_at": None,
                "detail": "missing",
            },
        )
        session.add(model)
        session.flush()

        snapshot = settings_to_contract(model, session)

    assert snapshot.judge_configured is True
    assert snapshot.learning_configured is False
    assert snapshot.ai_configured is False
    assert snapshot.alert_threshold == 95
