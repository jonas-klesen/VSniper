from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _discover_runtime_root() -> Path:
    cwd = Path.cwd().resolve()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "docker-compose.yml").exists():
            return candidate

    fallback = Path(__file__).resolve().parents[3]
    return fallback


class Settings(BaseSettings):
    app_env: str = Field(default="development", alias="APP_ENV")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    sqlite_path: Path = Field(default=Path("storage/sqlite/vsniper.db"), alias="SQLITE_PATH")
    vinted_region: str = Field(default="de", alias="VINTED_REGION")
    vinted_cookie: str = Field(default="put-your-vinted-cookie-here", alias="VINTED_COOKIE")
    vinted_browser_webdriver_url: str = Field(
        default="http://browser:4444/wd/hub",
        alias="VINTED_BROWSER_WEBDRIVER_URL",
    )
    vinted_browser_proxy_url: str = Field(default="", alias="VINTED_BROWSER_PROXY_URL")
    vinted_browser_timeout_seconds: int = Field(default=30, alias="VINTED_BROWSER_TIMEOUT_SECONDS")
    vinted_browser_profile_dir: Path = Field(
        default=Path("/app/browser-profile"),
        alias="VINTED_BROWSER_PROFILE_DIR",
    )
    telegram_bot_token: str = Field(default="put-your-telegram-bot-token-here", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="put-your-telegram-chat-id-here", alias="TELEGRAM_CHAT_ID")
    telegram_webhook_url: str = Field(
        default="put-your-telegram-webhook-url-here",
        alias="TELEGRAM_WEBHOOK_URL",
    )
    telegram_webhook_secret: str = Field(
        default="put-your-telegram-webhook-secret-here",
        alias="TELEGRAM_WEBHOOK_SECRET",
    )
    ai_judge_provider: str = Field(default="local", alias="AI_JUDGE_PROVIDER")
    ai_judge_model: str = Field(default="gpt-5.4-mini", alias="AI_JUDGE_MODEL")
    local_judge_model: str = Field(default="gemma4-12b-quality", alias="LOCAL_JUDGE_MODEL")
    ai_judge_allow_openai_fallback: bool = Field(default=False, alias="AI_JUDGE_ALLOW_OPENAI_FALLBACK")
    ai_judge_fallback_provider: str = Field(default="none", alias="AI_JUDGE_FALLBACK_PROVIDER")
    cerebras_api_key: str = Field(default="put-your-cerebras-api-key-here", alias="CEREBRAS_API_KEY")
    cerebras_api_base_url: str = Field(default="https://api.cerebras.ai/v1", alias="CEREBRAS_API_BASE_URL")
    cerebras_judge_model: str = Field(default="gemma-4-31b", alias="CEREBRAS_JUDGE_MODEL")
    openrouter_api_key: str = Field(default="put-your-openrouter-api-key-here", alias="OPENROUTER_API_KEY")
    openrouter_api_base_url: str = Field(default="https://openrouter.ai/api/v1", alias="OPENROUTER_API_BASE_URL")
    ai_judge_reasoning_effort: str = Field(default="low", alias="AI_JUDGE_REASONING_EFFORT")
    ai_judge_image_detail: str = Field(default="low", alias="AI_JUDGE_IMAGE_DETAIL")
    local_vlm_base_url: str = Field(default="http://127.0.0.1:8080/v1", alias="LOCAL_VLM_BASE_URL")
    ai_learn_model: str = Field(default="gpt-5.5", alias="AI_LEARN_MODEL")
    ai_learn_reasoning_effort: str = Field(default="medium", alias="AI_LEARN_REASONING_EFFORT")
    ai_learn_image_detail: str = Field(default="low", alias="AI_LEARN_IMAGE_DETAIL")
    ai_observation_provider: str = Field(default="local", alias="AI_OBSERVATION_PROVIDER")
    local_observation_model: str = Field(default="gemma4-12b-quality", alias="LOCAL_OBSERVATION_MODEL")
    ai_learn_observation_batch_size: int = Field(default=15, alias="AI_LEARN_OBSERVATION_BATCH_SIZE")
    vlm_grid_size: int = Field(default=1, alias="VLM_GRID_SIZE")
    vlm_pack_multiple_listing_images: bool = Field(default=True, alias="VLM_PACK_MULTIPLE_LISTING_IMAGES")
    vlm_judge_parallel_requests: int = Field(default=1, alias="VLM_JUDGE_PARALLEL_REQUESTS")
    ai_api_key: str = Field(default="put-your-ai-key-here", alias="AI_API_KEY")
    upload_dir: Path = Field(default=Path("storage/uploads"), alias="UPLOAD_DIR")
    cache_dir: Path = Field(default=Path("storage/cache"), alias="CACHE_DIR")
    feedback_asset_dir: Path = Field(default=Path("storage/feedback-assets"), alias="FEEDBACK_ASSET_DIR")
    worker_max_concurrency: int = Field(default=4, alias="WORKER_MAX_CONCURRENCY")
    # Fallback/seed value only — the live worker reads AppSettingsState.scan_interval_seconds
    # (editable on the Settings page) once the DB row exists.
    scan_interval_seconds: int = Field(default=1800, alias="SCAN_INTERVAL_SECONDS")
    # Retention: the worker prunes rows older than these (generous) windows so the SQLite file
    # and the stats/queue scans stay bounded. Pending/processing deliveries are never pruned.
    candidate_retention_days: int = Field(default=365, alias="CANDIDATE_RETENTION_DAYS")
    delivery_retention_days: int = Field(default=365, alias="DELIVERY_RETENTION_DAYS")
    ai_usage_retention_days: int = Field(default=365, alias="AI_USAGE_RETENTION_DAYS")
    error_retention_days: int = Field(default=365, alias="ERROR_RETENTION_DAYS")
    # How often (in worker cycles) to run the prune job; ~hourly at a 60s cycle interval.
    prune_every_cycles: int = Field(default=60, alias="PRUNE_EVERY_CYCLES")
    # Comma-separated list of browser origins allowed to call the API. Defaults to the local
    # Vite dev server; set explicitly (e.g. the LAN host) in deployments. "*" disables the lock.
    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173", alias="CORS_ALLOWED_ORIGINS"
    )

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    def resolve_path(self, value: Path) -> Path:
        if value.is_absolute():
            return value
        return _discover_runtime_root() / value

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.resolve_path(self.sqlite_path)}"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.resolve_path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    settings.resolve_path(settings.cache_dir).mkdir(parents=True, exist_ok=True)
    settings.resolve_path(settings.feedback_asset_dir).mkdir(parents=True, exist_ok=True)
    settings.resolve_path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    return settings
