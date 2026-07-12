from fastapi import APIRouter, HTTPException, Query, status

from vsniper.domain.contracts import (
    MaintenancePauseRequest,
    OperationsSnapshot,
    SearchRunPage,
)
from vsniper.services import operations_service
from vsniper.services.maintenance_service import (
    MaintenanceImportActive,
    begin_manual_pause,
    end_manual_pause,
)

router = APIRouter(tags=["operations"])


@router.get("/operations/status", response_model=OperationsSnapshot)
def operations_status() -> OperationsSnapshot:
    return operations_service.get_operations_snapshot()


@router.post("/operations/maintenance/pause", status_code=status.HTTP_204_NO_CONTENT)
def maintenance_pause(body: MaintenancePauseRequest | None = None) -> None:
    reason = body.reason if body else ""
    try:
        begin_manual_pause(reason=reason)
    except MaintenanceImportActive as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/operations/maintenance/resume", status_code=status.HTTP_204_NO_CONTENT)
def maintenance_resume() -> None:
    end_manual_pause()


@router.get("/operations/search-runs", response_model=SearchRunPage)
def list_search_runs(
    search_id: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> SearchRunPage:
    return operations_service.get_search_runs(
        search_id=search_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
