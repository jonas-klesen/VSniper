"""ai models registry

Revision ID: 20260630a005
Revises: 20260630a004
Create Date: 2026-06-30 00:00:00.000000

"""
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "20260630a005"
down_revision: Union[str, Sequence[str], None] = "20260630a004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _new_model_id() -> str:
    return f"model-{uuid4().hex[:8]}"


def upgrade() -> None:
    op.create_table(
        "ai_models",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("reasoning_effort", sa.String(length=32), nullable=False),
        sa.Column("local_base_url", sa.String(length=512), nullable=True),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("judge_model_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("judge_fallback_model_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("learn_model_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("observation_model_id", sa.String(length=64), nullable=True))

    connection = op.get_bind()
    row = connection.execute(
        sa.text(
            "SELECT ai_judge_provider, ai_judge_model, local_judge_model, "
            "ai_judge_fallback_provider, ai_judge_allow_openai_fallback, cerebras_judge_model, "
            "ai_judge_reasoning_effort, local_vlm_base_url, ai_learn_model, ai_learn_reasoning_effort, "
            "ai_observation_provider, local_observation_model "
            "FROM app_settings WHERE id = 1"
        )
    ).mappings().first()

    if row is not None:
        provider_map = {"openai": "openai", "cerebras": "cerebras", "local": "local"}

        def _provider(value: str | None) -> str:
            normalized = (value or "").strip().lower()
            return provider_map.get(normalized, "local")

        judge_provider = _provider(row["ai_judge_provider"])
        judge_model_name = row["local_judge_model"] if judge_provider == "local" else row["ai_judge_model"]
        judge_effort = row["ai_judge_reasoning_effort"] or "low"
        judge_base_url = row["local_vlm_base_url"] if judge_provider == "local" else None

        fallback_provider_raw = (row["ai_judge_fallback_provider"] or "none").strip().lower()
        if fallback_provider_raw == "none" and row["ai_judge_allow_openai_fallback"]:
            fallback_provider_raw = "openai"
        fallback_provider = fallback_provider_raw if fallback_provider_raw in {"openai", "cerebras"} else None
        if fallback_provider == "openai":
            fallback_model_name: str | None = row["ai_judge_model"]
        elif fallback_provider == "cerebras":
            fallback_model_name = row["cerebras_judge_model"]
        else:
            fallback_model_name = None
        fallback_effort = judge_effort
        fallback_base_url = None

        learn_provider = "openai"
        learn_model_name = row["ai_learn_model"]
        learn_effort = row["ai_learn_reasoning_effort"] or "medium"
        learn_base_url = None

        observation_provider_raw = (row["ai_observation_provider"] or "local").strip().lower()
        if observation_provider_raw == "local":
            observation_provider = "local"
            observation_model_name = judge_model_name if judge_provider == "local" else row["local_observation_model"]
            observation_effort = judge_effort
            observation_base_url = judge_base_url
        else:
            observation_provider = "openai"
            observation_model_name = learn_model_name
            observation_effort = learn_effort
            observation_base_url = None

        # Dedupe identical (provider, model_name, effort, base_url) tuples into one row.
        seeded: dict[tuple[str, str, str, str | None], str] = {}

        def _seed(provider: str, model_name: str | None, effort: str, base_url: str | None) -> str | None:
            if not model_name:
                return None
            key = (provider, model_name, effort, base_url)
            if key in seeded:
                return seeded[key]
            model_id = _new_model_id()
            provider_label = {"openai": "OpenAI", "cerebras": "Cerebras", "local": "Local"}.get(provider, provider.capitalize())
            display_name = f"{model_name} ({provider_label}) · {effort}"
            connection.execute(
                sa.text(
                    "INSERT INTO ai_models "
                    "(id, provider, model_name, reasoning_effort, local_base_url, display_name, created_at, updated_at) "
                    "VALUES (:id, :provider, :model_name, :effort, :base_url, :display_name, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": model_id,
                    "provider": provider,
                    "model_name": model_name,
                    "effort": effort,
                    "base_url": base_url,
                    "display_name": display_name,
                },
            )
            seeded[key] = model_id
            return model_id

        judge_model_id = _seed(judge_provider, judge_model_name, judge_effort, judge_base_url)
        judge_fallback_model_id = (
            _seed(fallback_provider, fallback_model_name, fallback_effort, fallback_base_url)
            if fallback_provider is not None
            else None
        )
        learn_model_id = _seed(learn_provider, learn_model_name, learn_effort, learn_base_url)
        observation_model_id = _seed(observation_provider, observation_model_name, observation_effort, observation_base_url)

        connection.execute(
            sa.text(
                "UPDATE app_settings SET judge_model_id = :judge, judge_fallback_model_id = :fallback, "
                "learn_model_id = :learn, observation_model_id = :observation WHERE id = 1"
            ),
            {
                "judge": judge_model_id,
                "fallback": judge_fallback_model_id,
                "learn": learn_model_id,
                "observation": observation_model_id,
            },
        )

    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.drop_column("ai_judge_provider")
        batch_op.drop_column("ai_judge_model")
        batch_op.drop_column("local_judge_model")
        batch_op.drop_column("ai_judge_allow_openai_fallback")
        batch_op.drop_column("ai_judge_fallback_provider")
        batch_op.drop_column("cerebras_judge_model")
        batch_op.drop_column("ai_judge_reasoning_effort")
        batch_op.drop_column("local_vlm_base_url")
        batch_op.drop_column("ai_learn_model")
        batch_op.drop_column("ai_learn_reasoning_effort")
        batch_op.drop_column("ai_observation_provider")
        batch_op.drop_column("local_observation_model")


def downgrade() -> None:
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ai_judge_provider", sa.String(length=32), nullable=False, server_default="local"))
        batch_op.add_column(sa.Column("ai_judge_model", sa.String(length=128), nullable=False, server_default="gpt-5.4-mini"))
        batch_op.add_column(sa.Column("local_judge_model", sa.String(length=128), nullable=False, server_default="gemma4-12b-quality"))
        batch_op.add_column(sa.Column("ai_judge_allow_openai_fallback", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("ai_judge_fallback_provider", sa.String(length=32), nullable=False, server_default="none"))
        batch_op.add_column(sa.Column("cerebras_judge_model", sa.String(length=128), nullable=False, server_default="gemma-4-31b"))
        batch_op.add_column(sa.Column("ai_judge_reasoning_effort", sa.String(length=32), nullable=False, server_default="low"))
        batch_op.add_column(sa.Column("local_vlm_base_url", sa.String(length=512), nullable=False, server_default="http://127.0.0.1:8080/v1"))
        batch_op.add_column(sa.Column("ai_learn_model", sa.String(length=128), nullable=False, server_default="gpt-5.5"))
        batch_op.add_column(sa.Column("ai_learn_reasoning_effort", sa.String(length=32), nullable=False, server_default="medium"))
        batch_op.add_column(sa.Column("ai_observation_provider", sa.String(length=32), nullable=False, server_default="local"))
        batch_op.add_column(sa.Column("local_observation_model", sa.String(length=128), nullable=False, server_default="gemma4-12b-quality"))
        batch_op.drop_column("judge_model_id")
        batch_op.drop_column("judge_fallback_model_id")
        batch_op.drop_column("learn_model_id")
        batch_op.drop_column("observation_model_id")

    op.drop_table("ai_models")
