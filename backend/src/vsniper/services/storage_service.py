"""Disk usage reporting and cache clearing for `storage/`.

Reports sizes for the DB, uploads, feedback-assets, and cache subtrees. Only
`cache/candidate-images/` is cleared on demand: it is fully regenerable
(re-fetched from Vinted on demand, see `search_service._evict_stale_cache`).
`cache/taste-offers/` holds images for manually-added taste offer examples
that feed the vision model during taste recompute and cannot be regenerated,
so it is reported but never cleared here.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from vsniper.core.config import get_settings
from vsniper.domain.contracts import CacheClearResult, StorageCategoryStats, StorageStats

_DB_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _dir_stats(path: Path) -> StorageCategoryStats:
    if not path.exists():
        return StorageCategoryStats(bytes=0, file_count=0)
    total_bytes = 0
    file_count = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            total_bytes += entry.stat().st_size
            file_count += 1
    return StorageCategoryStats(bytes=total_bytes, file_count=file_count)


def _db_stats(db_path: Path) -> StorageCategoryStats:
    total_bytes = 0
    file_count = 0
    if db_path.exists():
        total_bytes += db_path.stat().st_size
        file_count += 1
    for suffix in _DB_SIDECAR_SUFFIXES:
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            total_bytes += sidecar.stat().st_size
            file_count += 1
    return StorageCategoryStats(bytes=total_bytes, file_count=file_count)


def _cache_other_stats(cache_dir: Path) -> StorageCategoryStats:
    if not cache_dir.exists():
        return StorageCategoryStats(bytes=0, file_count=0)
    total_bytes = 0
    file_count = 0
    for entry in cache_dir.iterdir():
        if entry.is_file():
            total_bytes += entry.stat().st_size
            file_count += 1
    return StorageCategoryStats(bytes=total_bytes, file_count=file_count)


def get_storage_stats() -> StorageStats:
    settings = get_settings()
    db_stats = _db_stats(settings.resolve_path(settings.sqlite_path))
    uploads_stats = _dir_stats(settings.resolve_path(settings.upload_dir))
    feedback_assets_stats = _dir_stats(settings.resolve_path(settings.feedback_asset_dir))
    cache_dir = settings.resolve_path(settings.cache_dir)
    candidate_images_stats = _dir_stats(cache_dir / "candidate-images")
    taste_offers_stats = _dir_stats(cache_dir / "taste-offers")
    cache_other_stats = _cache_other_stats(cache_dir)

    total_bytes = (
        db_stats.bytes
        + uploads_stats.bytes
        + feedback_assets_stats.bytes
        + candidate_images_stats.bytes
        + taste_offers_stats.bytes
        + cache_other_stats.bytes
    )

    return StorageStats(
        total_bytes=total_bytes,
        database=db_stats,
        uploads=uploads_stats,
        feedback_assets=feedback_assets_stats,
        cache_candidate_images=candidate_images_stats,
        cache_taste_offers=taste_offers_stats,
        cache_other=cache_other_stats,
    )


def clear_candidate_image_cache() -> CacheClearResult:
    settings = get_settings()
    candidate_images_dir = settings.resolve_path(settings.cache_dir) / "candidate-images"
    freed = _dir_stats(candidate_images_dir)
    if candidate_images_dir.exists():
        shutil.rmtree(candidate_images_dir)
    candidate_images_dir.mkdir(parents=True, exist_ok=True)
    return CacheClearResult(bytes_freed=freed.bytes, files_removed=freed.file_count)
