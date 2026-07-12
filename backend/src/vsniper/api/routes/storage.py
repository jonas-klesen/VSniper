from fastapi import APIRouter

from vsniper.domain.contracts import CacheClearResult, StorageStats
from vsniper.services import storage_service

router = APIRouter(tags=["storage"])


@router.get("/storage/stats", response_model=StorageStats)
def storage_stats() -> StorageStats:
    return storage_service.get_storage_stats()


@router.post("/storage/cache/clear", response_model=CacheClearResult)
def clear_cache() -> CacheClearResult:
    return storage_service.clear_candidate_image_cache()
