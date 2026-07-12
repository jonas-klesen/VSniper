"""Module-level CRUD for the AI model registry (`ai_models` table).

Not part of `AppState`/the four domain services on purpose — this is plain CRUD over a
standalone table with no cross-service wiring, following the same pattern as
`operations_service.py`."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from vsniper.core.database import session_scope
from vsniper.db.models import AiModelConfig as AiModelConfigRow, AppSettingsState
from vsniper.domain.contracts import AiModelConfig, AiModelCreate, AiModelUpdate


class AiModelInUse(Exception):
    """Raised when attempting to delete an AI model that is still referenced by settings."""


_PROVIDER_LABELS = {"openai": "OpenAI", "cerebras": "Cerebras", "local": "Local", "openrouter": "OpenRouter"}


def _display_name(provider: str, model_name: str, reasoning_effort: str) -> str:
    label = _PROVIDER_LABELS.get(provider, provider.capitalize())
    return f"{model_name} ({label}) · {reasoning_effort}"


def _to_contract(row: AiModelConfigRow) -> AiModelConfig:
    return AiModelConfig.model_validate(
        {
            "id": row.id,
            "provider": row.provider,
            "model_name": row.model_name,
            "reasoning_effort": row.reasoning_effort,
            "local_base_url": row.local_base_url,
            "display_name": row.display_name,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


def list_models() -> list[AiModelConfig]:
    with session_scope() as session:
        rows = session.scalars(select(AiModelConfigRow).order_by(AiModelConfigRow.created_at.asc())).all()
        return [_to_contract(row) for row in rows]


def create_model(payload: AiModelCreate) -> AiModelConfig:
    now = datetime.now(UTC)
    with session_scope() as session:
        row = AiModelConfigRow(
            id=f"model-{uuid4().hex[:8]}",
            provider=payload.provider,
            model_name=payload.model_name.strip(),
            reasoning_effort=payload.reasoning_effort.strip(),
            local_base_url=payload.local_base_url.strip() if payload.local_base_url else None,
            display_name=_display_name(payload.provider, payload.model_name.strip(), payload.reasoning_effort.strip()),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return _to_contract(row)


def update_model(model_id: str, payload: AiModelUpdate) -> AiModelConfig:
    with session_scope() as session:
        row = session.get(AiModelConfigRow, model_id)
        if row is None:
            raise KeyError(model_id)
        if payload.model_name is not None:
            row.model_name = payload.model_name.strip()
        if payload.reasoning_effort is not None:
            row.reasoning_effort = payload.reasoning_effort.strip()
        if payload.local_base_url is not None:
            row.local_base_url = payload.local_base_url.strip() or None
        if row.provider == "local" and not row.local_base_url:
            raise ValueError("local_base_url is required when provider is 'local'.")
        row.display_name = _display_name(row.provider, row.model_name, row.reasoning_effort)
        row.updated_at = datetime.now(UTC)
        session.flush()
        return _to_contract(row)


def delete_model(model_id: str) -> None:
    with session_scope() as session:
        row = session.get(AiModelConfigRow, model_id)
        if row is None:
            raise KeyError(model_id)
        settings_row = session.get(AppSettingsState, 1)
        if settings_row is not None and model_id in {
            settings_row.judge_model_id,
            settings_row.judge_fallback_model_id,
            settings_row.learn_model_id,
            settings_row.observation_model_id,
        }:
            raise AiModelInUse(model_id)
        session.delete(row)
