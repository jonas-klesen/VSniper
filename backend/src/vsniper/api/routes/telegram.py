from inspect import signature

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, status

from vsniper.core.state import get_state
from vsniper.domain.contracts import (
    TelegramUpdate,
    TelegramWebhookRegistrationPayload,
    TelegramWebhookResult,
    TelegramWebhookStatus,
)

router = APIRouter(tags=["telegram"])


@router.get("/telegram/webhook", response_model=TelegramWebhookStatus)
def get_telegram_webhook_status() -> TelegramWebhookStatus:
    return get_state().telegram.get_webhook_status()


@router.post("/telegram/webhook/register", response_model=TelegramWebhookStatus)
def register_telegram_webhook(payload: TelegramWebhookRegistrationPayload) -> TelegramWebhookStatus:
    try:
        return get_state().telegram.configure_webhook(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/telegram/webhook", response_model=TelegramWebhookResult)
def telegram_webhook(
    payload: TelegramUpdate,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> TelegramWebhookResult:
    if not get_state().telegram.is_webhook_secret_valid(x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Telegram webhook secret token")
    handler = get_state().telegram.handle_webhook
    if "schedule_background" in signature(handler).parameters:
        return handler(payload, schedule_background=lambda task: background_tasks.add_task(task))
    return handler(payload)


@router.post("/telegram/test")
def telegram_test_message() -> dict[str, str]:
    return {"preview": get_state().build_test_telegram_message()}


@router.post("/telegram/test/send")
def send_telegram_test_message() -> dict[str, object]:
    try:
        return get_state().telegram.send_test_notification()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
