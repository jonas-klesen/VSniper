"""Thin facade that wires shared resources and the four domain services together.

Routes and worker jobs go through `get_state()` and call into the services via attributes
(`get_state().searches`, `.preferences`, `.candidates`, `.telegram`). Business logic lives
in `vsniper/services/*` — this module only owns lifecycle (DB init + seed)."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

import httpx

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vsniper.core.config import get_settings
from vsniper.core.database import dispose_engine, init_database, session_scope
from vsniper.db.models import AiModelConfig as AiModelConfigRow, AiUsageEvent, AppSettingsState, Search, TasteState
from vsniper.integrations.openai.pricing import compute_cost
from vsniper.domain.contracts import (
    SettingsSnapshot,
)
from vsniper.integrations.openai.client import OpenAITasteClient
from vsniper.integrations.telegram.client import TelegramClient
from vsniper.integrations.telegram.service import TelegramFormatter
from vsniper.integrations.vinted.client import VintedClient
from vsniper.services._mapping import build_session_health, integration_configuration
from vsniper.services.candidate_service import CandidateService
from vsniper.services.search_defaults import CANONICAL_CLOTHING_ITEMS, canonical_search_values
from vsniper.services.search_service import SearchService
from vsniper.services.telegram_service import TelegramService
from vsniper.services.taste_service import TasteService

logger = logging.getLogger(__name__)
_state_lock = threading.Lock()
_state_instance: AppState | None = None


@dataclass(frozen=True)
class _DefaultModelSeed:
    """One env-derived default registry row, plus which `*_model_id` settings field it fills."""

    id: str
    provider: str
    model_name: str
    reasoning_effort: str
    local_base_url: str | None


def _extract_refresh_token_from_cookie(cookie: str) -> str:
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name.strip() == "refresh_token_web" and value:
            return value
    return ""


def _default_model_seeds() -> tuple[list[_DefaultModelSeed], str, str | None, str, str]:
    """Derive the default ai_models rows (deduped) plus the four settings model ids from env
    config, mirroring the data migration's seeding logic for a brand-new (empty) database."""
    runtime = get_settings()

    judge_provider = runtime.ai_judge_provider.strip().lower()
    judge_provider = judge_provider if judge_provider in {"openai", "cerebras", "local"} else "local"
    judge_model_name = runtime.local_judge_model if judge_provider == "local" else runtime.ai_judge_model
    judge_effort = runtime.ai_judge_reasoning_effort
    judge_base_url = runtime.local_vlm_base_url if judge_provider == "local" else None

    fallback_provider_raw = runtime.ai_judge_fallback_provider.strip().lower()
    if fallback_provider_raw == "none" and runtime.ai_judge_allow_openai_fallback:
        fallback_provider_raw = "openai"
    fallback_provider = fallback_provider_raw if fallback_provider_raw in {"openai", "cerebras"} else None
    if fallback_provider == "openai":
        fallback_model_name: str | None = runtime.ai_judge_model
    elif fallback_provider == "cerebras":
        fallback_model_name = runtime.cerebras_judge_model
    else:
        fallback_model_name = None

    learn_provider = "openai"
    learn_model_name = runtime.ai_learn_model
    learn_effort = runtime.ai_learn_reasoning_effort

    observation_provider_raw = runtime.ai_observation_provider.strip().lower()
    if observation_provider_raw == "local":
        observation_provider = "local"
        observation_model_name = judge_model_name if judge_provider == "local" else runtime.local_observation_model
        observation_effort = judge_effort
        observation_base_url = judge_base_url
    else:
        observation_provider = "openai"
        observation_model_name = learn_model_name
        observation_effort = learn_effort
        observation_base_url = None

    seeds: list[_DefaultModelSeed] = []
    seeded: dict[tuple[str, str, str, str | None], str] = {}

    def _seed(provider: str, model_name: str | None, effort: str, base_url: str | None) -> str | None:
        if not model_name:
            return None
        key = (provider, model_name, effort, base_url)
        if key in seeded:
            return seeded[key]
        model_id = f"model-{uuid4().hex[:8]}"
        seeds.append(
            _DefaultModelSeed(
                id=model_id,
                provider=provider,
                model_name=model_name,
                reasoning_effort=effort,
                local_base_url=base_url,
            )
        )
        seeded[key] = model_id
        return model_id

    judge_model_id = _seed(judge_provider, judge_model_name, judge_effort, judge_base_url)
    judge_fallback_model_id = (
        _seed(fallback_provider, fallback_model_name, judge_effort, None) if fallback_provider is not None else None
    )
    learn_model_id = _seed(learn_provider, learn_model_name, learn_effort, None)
    observation_model_id = _seed(observation_provider, observation_model_name, observation_effort, observation_base_url)

    assert judge_model_id is not None  # local_judge_model/ai_judge_model always have a default
    assert learn_model_id is not None  # ai_learn_model always has a default
    return seeds, judge_model_id, judge_fallback_model_id, learn_model_id, observation_model_id or judge_model_id


def _default_settings(now: datetime) -> SettingsSnapshot:
    runtime = get_settings()
    vinted_configured, telegram_configured, _ = integration_configuration()
    refresh_token = _extract_refresh_token_from_cookie(runtime.vinted_cookie)
    return SettingsSnapshot(
        vinted_region=runtime.vinted_region,
        vinted_cookie=runtime.vinted_cookie,
        vinted_refresh_token=refresh_token,
        telegram_bot_token=runtime.telegram_bot_token,
        telegram_chat_id=runtime.telegram_chat_id,
        telegram_webhook_url=runtime.telegram_webhook_url,
        telegram_webhook_secret=runtime.telegram_webhook_secret,
        vinted_configured=vinted_configured,
        telegram_configured=telegram_configured,
        ai_configured=False,
        judge_configured=False,
        learning_configured=False,
        vlm_grid_size=runtime.vlm_grid_size,
        vlm_pack_multiple_listing_images=runtime.vlm_pack_multiple_listing_images,
        vlm_judge_parallel_requests=runtime.vlm_judge_parallel_requests,
        ai_judge_image_max_px=512,
        alert_threshold=95,
        scan_interval_seconds=runtime.scan_interval_seconds,
        session_health=build_session_health(region=runtime.vinted_region),
    )


class AppState:
    """Holds shared resources (Settings, integration clients) and exposes the four services
    that contain all business logic."""

    def __init__(self) -> None:
        self._initialize()

    def _initialize(self) -> None:
        started = perf_counter()
        logger.info("AppState initialization started")
        self.settings = get_settings()
        logger.info("AppState settings loaded")

        self._vinted_http_client = httpx.Client(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "vsniper/0.1 (+https://local.vsniper)"},
        )
        self._telegram_http_client = httpx.Client(timeout=10)

        self.vinted_client = VintedClient(
            client=self._vinted_http_client,
            on_tokens_refreshed=self._persist_refreshed_tokens,
        )
        self.taste_client = OpenAITasteClient(self.settings, on_usage=self._record_ai_usage)
        self.telegram_client = TelegramClient(client=self._telegram_http_client)
        self.telegram_formatter = TelegramFormatter()
        logger.info("AppState integration clients initialized")

        init_database()
        logger.info("AppState database initialized")
        self._seed_if_empty()
        logger.info("AppState defaults seeded")

        with session_scope() as session:
            row = session.get(AppSettingsState, 1)
            if row is not None:
                self.vinted_client.set_cookie(row.vinted_cookie or "")
                self.vinted_client.set_refresh_token(row.vinted_refresh_token or "")
        logger.info("AppState persisted settings loaded")

        self.taste = TasteService(self.settings, self.taste_client, self.vinted_client)
        self.preferences = self.taste
        self.candidates = CandidateService(self.settings, self.taste, self.taste_client)
        self.telegram = TelegramService(
            self.settings, self.telegram_client, self.telegram_formatter, self.candidates, self.vinted_client, self.taste
        )
        self.searches = SearchService(
            self.settings, self.vinted_client, self.taste_client, self.taste, self.telegram
        )
        self.telegram.set_search_draft_applier(self.searches.apply_generated_search_drafts)
        logger.info("AppState services initialized in %.1fs", perf_counter() - started)

    def reload(self) -> None:
        """Tear down and re-initialize shared resources against the on-disk DB.

        Called after a full-state import overwrites `vsniper.db` so the running
        API process picks up the new schema, settings, and integration credentials
        without a process restart. The worker sees the new state on its next
        `get_state()` access (it shares the singleton)."""
        logger.info("AppState reload started")
        try:
            self.close()
        except Exception:
            logger.exception("AppState close during reload failed; continuing with re-init")
        dispose_engine()
        self._initialize()
        logger.info("AppState reload finished")

    @staticmethod
    def _seed_if_empty() -> None:
        now = datetime.now(UTC)
        logger.info("AppState seed_if_empty started")
        with session_scope() as session:
            settings_row_existed = session.get(AppSettingsState, 1) is not None
            settings = _default_settings(now)
            judge_model_id: str | None = None
            judge_fallback_model_id: str | None = None
            learn_model_id: str | None = None
            observation_model_id: str | None = None
            if not settings_row_existed:
                seeds, judge_model_id, judge_fallback_model_id, learn_model_id, observation_model_id = (
                    _default_model_seeds()
                )
                for seed in seeds:
                    session.add(
                        AiModelConfigRow(
                            id=seed.id,
                            provider=seed.provider,
                            model_name=seed.model_name,
                            reasoning_effort=seed.reasoning_effort,
                            local_base_url=seed.local_base_url,
                            display_name=f"{seed.model_name} ({seed.provider.capitalize()}) · {seed.reasoning_effort}",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                session.flush()
            session.execute(
                sqlite_insert(AppSettingsState)
                .values(
                    id=1,
                    vinted_region=settings.vinted_region,
                    vinted_cookie=settings.vinted_cookie or "",
                    vinted_refresh_token=settings.vinted_refresh_token or "",
                    telegram_bot_token=settings.telegram_bot_token or "",
                    telegram_chat_id=settings.telegram_chat_id or "",
                    telegram_webhook_url=settings.telegram_webhook_url or "",
                    telegram_webhook_secret=settings.telegram_webhook_secret or "",
                    telegram_configured=settings.telegram_configured,
                    judge_model_id=judge_model_id,
                    judge_fallback_model_id=judge_fallback_model_id,
                    learn_model_id=learn_model_id,
                    observation_model_id=observation_model_id,
                    vlm_grid_size=settings.vlm_grid_size,
                    vlm_pack_multiple_listing_images=settings.vlm_pack_multiple_listing_images,
                    vlm_judge_parallel_requests=settings.vlm_judge_parallel_requests,
                    ai_judge_image_max_px=settings.ai_judge_image_max_px,
                    alert_threshold=settings.alert_threshold,
                    scan_interval_seconds=settings.scan_interval_seconds,
                    session_health=settings.session_health.model_dump(mode="json"),
                )
                .on_conflict_do_nothing()
            )
            session.execute(
                sqlite_insert(TasteState)
                .values(id=1, manual_note="", taste_profile={}, reference_observations=[])
                .on_conflict_do_nothing()
            )
            for clothing_item in CANONICAL_CLOTHING_ITEMS:
                session.execute(
                    sqlite_insert(Search)
                    .values(**canonical_search_values(clothing_item, now=now))
                    .on_conflict_do_nothing()
                )
        logger.info("AppState seed_if_empty finished")

    def _persist_refreshed_tokens(self, cookie: str, refresh_token: str) -> None:
        with session_scope() as session:
            row = session.get(AppSettingsState, 1)
            if row is not None:
                row.vinted_cookie = cookie
                row.vinted_refresh_token = refresh_token

    def _record_ai_usage(
        self, operation: str, model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> None:
        with session_scope() as session:
            session.add(AiUsageEvent(
                called_at=datetime.now(UTC),
                operation=operation,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                cost_usd=compute_cost(model, input_tokens, output_tokens, cached_input_tokens),
            ))

    def build_test_telegram_message(self) -> str:
        """Convenience for the /api/telegram/test-preview route — needs candidate list + formatter."""
        return self.telegram.build_test_message(self.candidates.page(limit=1).items)

    def close(self) -> None:
        self.taste_client.close()
        self._vinted_http_client.close()
        self._telegram_http_client.close()


def get_state() -> AppState:
    global _state_instance
    if _state_instance is None:
        with _state_lock:
            if _state_instance is None:
                _state_instance = AppState()
    return _state_instance
