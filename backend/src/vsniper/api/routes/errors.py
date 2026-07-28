from fastapi import APIRouter, HTTPException, Query, status

from vsniper.core.state import get_state
from vsniper.domain.contracts import (
    ErrorEventPage,
    ErrorNotificationSettings,
    ErrorNotificationSettingsUpdate,
    ErrorSource,
)

router = APIRouter(tags=["errors"])


@router.get("/errors", response_model=ErrorEventPage)
def list_errors(
    source: ErrorSource | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ErrorEventPage:
    return get_state().errors.page(source=source, limit=limit, offset=offset)


@router.put("/errors/telegram-notifications", response_model=ErrorNotificationSettings)
def update_error_notification_settings(
    payload: ErrorNotificationSettingsUpdate,
) -> ErrorNotificationSettings:
    try:
        return get_state().errors.update_notification_settings(enabled=payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
