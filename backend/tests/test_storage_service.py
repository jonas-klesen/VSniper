from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from vsniper.core.config import get_settings
from vsniper.core.database import dispose_engine, init_database
from vsniper.services import storage_service as svc


@pytest.fixture()
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage = tmp_path / "storage"
    db_path = storage / "sqlite" / "vsniper.db"
    uploads = storage / "uploads"
    cache = storage / "cache"
    feedback_assets = storage / "feedback-assets"
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    monkeypatch.setenv("UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("CACHE_DIR", str(cache))
    monkeypatch.setenv("FEEDBACK_ASSET_DIR", str(feedback_assets))
    get_settings.cache_clear()
    dispose_engine()
    init_database()

    yield SimpleNamespace(
        storage=storage,
        db_path=db_path,
        uploads=uploads,
        cache=cache,
        feedback_assets=feedback_assets,
    )

    dispose_engine()
    get_settings.cache_clear()


def test_get_storage_stats_reports_sizes_per_category(isolated_storage) -> None:
    isolated_storage.db_path.with_name("vsniper.db-wal").write_bytes(b"12345")

    (isolated_storage.uploads / "taste").mkdir(parents=True)
    (isolated_storage.uploads / "taste" / "sample.jpg").write_bytes(b"upload-image")

    isolated_storage.feedback_assets.mkdir(parents=True, exist_ok=True)
    (isolated_storage.feedback_assets / "feedback.jpg").write_bytes(b"feedback-image")

    candidate_images = isolated_storage.cache / "candidate-images"
    candidate_images.mkdir(parents=True)
    (candidate_images / "a.jpg").write_bytes(b"candidate-a")
    (candidate_images / "b.jpg").write_bytes(b"candidate-b")

    taste_offers = isolated_storage.cache / "taste-offers"
    taste_offers.mkdir(parents=True)
    (taste_offers / "offer.jpg").write_bytes(b"taste-offer")

    (isolated_storage.cache / "stray.txt").write_bytes(b"stray")

    stats = svc.get_storage_stats()

    assert stats.database.bytes == isolated_storage.db_path.stat().st_size + 5
    assert stats.database.file_count == 2
    assert stats.uploads == svc.StorageCategoryStats(bytes=len(b"upload-image"), file_count=1)
    assert stats.feedback_assets == svc.StorageCategoryStats(bytes=len(b"feedback-image"), file_count=1)
    assert stats.cache_candidate_images == svc.StorageCategoryStats(
        bytes=len(b"candidate-a") + len(b"candidate-b"), file_count=2
    )
    assert stats.cache_taste_offers == svc.StorageCategoryStats(bytes=len(b"taste-offer"), file_count=1)
    assert stats.cache_other == svc.StorageCategoryStats(bytes=len(b"stray"), file_count=1)
    assert stats.total_bytes == (
        stats.database.bytes
        + stats.uploads.bytes
        + stats.feedback_assets.bytes
        + stats.cache_candidate_images.bytes
        + stats.cache_taste_offers.bytes
        + stats.cache_other.bytes
    )


def test_get_storage_stats_handles_missing_directories(isolated_storage) -> None:
    stats = svc.get_storage_stats()

    assert stats.uploads.bytes == 0
    assert stats.cache_candidate_images.bytes == 0
    assert stats.cache_taste_offers.bytes == 0
    assert stats.total_bytes == stats.database.bytes


def test_clear_candidate_image_cache_only_clears_candidate_images(isolated_storage) -> None:
    candidate_images = isolated_storage.cache / "candidate-images"
    candidate_images.mkdir(parents=True)
    (candidate_images / "a.jpg").write_bytes(b"candidate-a")
    (candidate_images / "b.jpg").write_bytes(b"candidate-bb")

    taste_offers = isolated_storage.cache / "taste-offers"
    taste_offers.mkdir(parents=True)
    (taste_offers / "offer.jpg").write_bytes(b"taste-offer")

    isolated_storage.uploads.mkdir(parents=True, exist_ok=True)
    (isolated_storage.uploads / "sample.jpg").write_bytes(b"upload-image")

    isolated_storage.feedback_assets.mkdir(parents=True, exist_ok=True)
    (isolated_storage.feedback_assets / "feedback.jpg").write_bytes(b"feedback-image")

    result = svc.clear_candidate_image_cache()

    assert result.bytes_freed == len(b"candidate-a") + len(b"candidate-bb")
    assert result.files_removed == 2
    assert list(candidate_images.rglob("*")) == []
    assert candidate_images.exists()
    assert (taste_offers / "offer.jpg").read_bytes() == b"taste-offer"
    assert (isolated_storage.uploads / "sample.jpg").read_bytes() == b"upload-image"
    assert (isolated_storage.feedback_assets / "feedback.jpg").read_bytes() == b"feedback-image"


def test_clear_candidate_image_cache_when_empty(isolated_storage) -> None:
    result = svc.clear_candidate_image_cache()

    assert result.bytes_freed == 0
    assert result.files_removed == 0
    assert (isolated_storage.cache / "candidate-images").exists()
