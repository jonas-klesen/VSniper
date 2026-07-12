from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from vsniper.core.database import session_scope
from vsniper.core.state import get_state
from vsniper.domain.contracts import (
    BlockedBrandsSnapshot,
    BlockedBrandsUpdate,
    ModelTestRequest,
    ModelTestResult,
    SessionHealth,
    SettingsSnapshot,
    SettingsUpdate,
    VintedBrandOption,
)
from vsniper.integrations.openai.client import OpenAIIntegrationError
from vsniper.integrations.vinted.client import VintedClientError
from vsniper.services._mapping import resolve_ai_model

router = APIRouter()


class ValidateCookieRequest(BaseModel):
    cookie: str


@router.get("/settings", response_model=SettingsSnapshot)
def get_settings() -> SettingsSnapshot:
    return get_state().searches.get_app_settings()


@router.put("/settings", response_model=SettingsSnapshot)
def update_settings(payload: SettingsUpdate) -> SettingsSnapshot:
    return get_state().searches.update_app_settings(payload)


@router.get("/settings/blocked-brands", response_model=BlockedBrandsSnapshot)
def get_blocked_brands() -> BlockedBrandsSnapshot:
    return BlockedBrandsSnapshot(brands=get_state().searches.get_blocked_brands())


@router.put("/settings/blocked-brands", response_model=BlockedBrandsSnapshot)
def update_blocked_brands(payload: BlockedBrandsUpdate) -> BlockedBrandsSnapshot:
    return BlockedBrandsSnapshot(brands=get_state().searches.update_blocked_brands(payload.brands))


@router.get("/vinted/brands", response_model=list[VintedBrandOption])
def search_vinted_brands(query: str = "") -> list[VintedBrandOption]:
    try:
        options = get_state().vinted_client.search_brands(query)
    except VintedClientError as exc:
        status_code = 503 if exc.retryable else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return [VintedBrandOption.model_validate(option) for option in options]


@router.post("/settings/validate-cookie", response_model=SessionHealth)
def validate_cookie(payload: ValidateCookieRequest) -> SessionHealth:
    return get_state().vinted_client.validate_cookie(payload.cookie)


@router.post("/settings/test-model", response_model=ModelTestResult)
def test_model(payload: ModelTestRequest) -> ModelTestResult:
    state = get_state()
    with session_scope() as session:
        resolved = resolve_ai_model(session, payload.model_id)

    if resolved is None:
        raise HTTPException(status_code=404, detail="AI model not found")
    if resolved.provider == "local" and not (resolved.local_base_url and resolved.local_base_url.strip()):
        raise HTTPException(status_code=400, detail="This local model has no base URL configured.")

    base_url = resolved.local_base_url.strip() if resolved.local_base_url else None
    model = resolved.model_name

    try:
        answer = state.taste_client.test_model(
            provider=resolved.provider,
            model=model,
            reasoning_effort=resolved.reasoning_effort,
            prompt=payload.prompt,
            local_base_url=base_url,
        )
    except OpenAIIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ModelTestResult(provider=resolved.provider, base_url=base_url, model=model, answer=answer)
