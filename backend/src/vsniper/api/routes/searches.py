from fastapi import APIRouter, HTTPException, status

from vsniper.core.state import get_state
from vsniper.domain.contracts import (
    SearchCategoryOption,
    SearchRecord,
    SearchRunResult,
    SearchUpdate,
    VintedSizesResult,
)
from vsniper.integrations.vinted.categories import CategoryFilterError
from vsniper.integrations.vinted.client import VintedClientError, VintedConfigurationError, VintedSessionError
from vsniper.services.maintenance_service import get_maintenance_state
from vsniper.services.search_service import SearchClothingItemImmutable, SearchNotConfigured, SearchRunAlreadyClaimed

router = APIRouter(tags=["searches"])


def _reject_if_maintenance() -> None:
    mode = get_maintenance_state().get("mode", "idle")
    if mode != "idle":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"System is in maintenance mode ({mode}). Wait for maintenance to finish before running scans.",
        )


@router.get("/searches", response_model=list[SearchRecord])
def list_searches() -> list[SearchRecord]:
    return get_state().searches.all()


@router.get("/searches/category-options", response_model=dict[str, SearchCategoryOption])
def search_category_options() -> dict[str, SearchCategoryOption]:
    return get_state().searches.category_options()


@router.put("/searches/{search_id}", response_model=SearchRecord)
def update_search(search_id: str, payload: SearchUpdate) -> SearchRecord:
    try:
        return get_state().searches.update(search_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found") from exc
    except SearchClothingItemImmutable as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except CategoryFilterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/searches/{search_id}/toggle", response_model=SearchRecord)
def toggle_search(search_id: str) -> SearchRecord:
    try:
        return get_state().searches.toggle(search_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found") from exc


@router.post("/searches/sync-sizes", response_model=VintedSizesResult)
def sync_sizes() -> VintedSizesResult:
    return get_state().searches.fetch_profile_sizes()


@router.post("/searches/apply-profile-sizes", response_model=VintedSizesResult)
def apply_profile_sizes() -> VintedSizesResult:
    return get_state().searches.apply_profile_sizes_to_all()


@router.post("/searches/run-all", response_model=list[SearchRunResult])
def run_all_searches() -> list[SearchRunResult]:
    _reject_if_maintenance()
    return get_state().searches.run_all_enabled()


@router.post("/searches/{search_id}/run", response_model=SearchRunResult)
def run_search(search_id: str) -> SearchRunResult:
    try:
        return get_state().searches.run_live(search_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found") from exc
    except SearchRunAlreadyClaimed as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Search is already running. Wait for the current run to finish before starting another live run.",
        ) from exc
    except SearchNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except (VintedConfigurationError, VintedSessionError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except VintedClientError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/searches/{search_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_search(search_id: str) -> None:
    try:
        cancelled = get_state().searches.request_cancel(search_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found") from exc
    if not cancelled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Search is not currently running.")
