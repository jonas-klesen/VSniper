from __future__ import annotations

import sqlite3
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from vsniper.core.config import get_settings
from vsniper.core.database import dispose_engine, init_database
from vsniper.domain.contracts import BackupManifest, BackupManifestFileEntry
from vsniper.services import data_management_service as svc
from vsniper.services.data_management_service import BackupError, BackupImportBusy, BackupImportVersionMismatch
from vsniper.services.maintenance_service import WorkerDrainTimeout


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

    monkeypatch.setattr(
        "vsniper.core.state.get_state",
        lambda: SimpleNamespace(reload=lambda: None),
    )
    yield SimpleNamespace(
        storage=storage,
        db_path=db_path,
        uploads=uploads,
        cache=cache,
        feedback_assets=feedback_assets,
    )

    dispose_engine()
    get_settings.cache_clear()


def _set_probe(db_path: Path, value: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS backup_probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("DELETE FROM backup_probe")
        conn.execute("INSERT INTO backup_probe (id, value) VALUES (1, ?)", (value,))


def _get_probe(db_path: Path) -> str:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT value FROM backup_probe WHERE id = 1").fetchone()
    return row[0]


def _get_maintenance_mode(db_path: Path) -> str:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT mode FROM maintenance_state WHERE id = 1").fetchone()
    return row[0]


def _zip_names(zip_bytes: bytes) -> set[str]:
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        return set(zf.namelist())


def test_export_includes_uploads_and_optionally_cache(isolated_storage) -> None:
    _set_probe(isolated_storage.db_path, "original")
    (isolated_storage.uploads / "taste").mkdir(parents=True)
    (isolated_storage.uploads / "taste" / "sample.jpg").write_bytes(b"upload-image")
    isolated_storage.feedback_assets.mkdir(parents=True, exist_ok=True)
    (isolated_storage.feedback_assets / "feedback.jpg").write_bytes(b"feedback-image")
    (isolated_storage.cache / "candidate-images").mkdir(parents=True)
    (isolated_storage.cache / "candidate-images" / "cached.jpg").write_bytes(b"cache-image")
    isolated_storage.db_path.with_name("vsniper.db-wal").write_bytes(b"sidecar")
    (isolated_storage.storage / ".migration.lock").write_text("lock")

    without_cache, manifest = svc.build_export(include_cache=False)
    with_cache, cache_manifest = svc.build_export(include_cache=True)

    assert manifest.include_cache is False
    names = _zip_names(without_cache)
    assert "manifest.json" in names
    assert "db/vsniper.db" in names
    assert "uploads/taste/sample.jpg" in names
    assert "feedback-assets/feedback.jpg" in names
    assert "cache/candidate-images/cached.jpg" not in names
    assert not any(name.endswith(("-wal", "-shm")) or name == ".migration.lock" for name in names)

    assert cache_manifest.include_cache is True
    assert "cache/candidate-images/cached.jpg" in _zip_names(with_cache)


def test_import_restores_db_uploads_and_cache_and_removes_stale_files(isolated_storage) -> None:
    _set_probe(isolated_storage.db_path, "original")
    (isolated_storage.uploads / "taste").mkdir(parents=True)
    (isolated_storage.uploads / "taste" / "sample.jpg").write_bytes(b"upload-image")
    isolated_storage.feedback_assets.mkdir(parents=True, exist_ok=True)
    (isolated_storage.feedback_assets / "feedback.jpg").write_bytes(b"feedback-image")
    (isolated_storage.cache / "candidate-images").mkdir(parents=True)
    (isolated_storage.cache / "candidate-images" / "cached.jpg").write_bytes(b"cache-image")
    backup, _ = svc.build_export(include_cache=True)

    _set_probe(isolated_storage.db_path, "changed")
    (isolated_storage.uploads / "taste" / "sample.jpg").write_bytes(b"changed-upload")
    (isolated_storage.uploads / "stale.txt").write_text("stale")
    (isolated_storage.feedback_assets / "feedback.jpg").write_bytes(b"changed-feedback")
    (isolated_storage.feedback_assets / "stale.txt").write_text("stale")
    (isolated_storage.cache / "candidate-images" / "cached.jpg").write_bytes(b"changed-cache")
    (isolated_storage.cache / "stale.txt").write_text("stale")

    result = svc.apply_import(backup)

    assert result.restored_db is True
    assert result.restored_files == 3
    assert result.skipped_files == []
    assert _get_probe(isolated_storage.db_path) == "original"
    assert (isolated_storage.uploads / "taste" / "sample.jpg").read_bytes() == b"upload-image"
    assert not (isolated_storage.uploads / "stale.txt").exists()
    assert (isolated_storage.feedback_assets / "feedback.jpg").read_bytes() == b"feedback-image"
    assert not (isolated_storage.feedback_assets / "stale.txt").exists()
    assert (isolated_storage.cache / "candidate-images" / "cached.jpg").read_bytes() == b"cache-image"
    assert not (isolated_storage.cache / "stale.txt").exists()
    assert list(isolated_storage.storage.glob(".pre-import-backup-*"))


def test_import_without_cache_clears_current_cache(isolated_storage) -> None:
    _set_probe(isolated_storage.db_path, "original")
    (isolated_storage.uploads / "taste").mkdir(parents=True)
    (isolated_storage.uploads / "taste" / "sample.jpg").write_bytes(b"upload-image")
    isolated_storage.feedback_assets.mkdir(parents=True, exist_ok=True)
    (isolated_storage.feedback_assets / "feedback.jpg").write_bytes(b"feedback-image")
    (isolated_storage.cache / "candidate-images").mkdir(parents=True)
    (isolated_storage.cache / "candidate-images" / "cached.jpg").write_bytes(b"cache-image")
    backup, _ = svc.build_export(include_cache=False)

    (isolated_storage.cache / "stale.txt").write_text("stale")
    svc.apply_import(backup)

    assert list(isolated_storage.cache.rglob("*")) == []
    assert (isolated_storage.feedback_assets / "feedback.jpg").read_bytes() == b"feedback-image"


def test_import_rejects_hash_mismatch_before_mutating(isolated_storage) -> None:
    _set_probe(isolated_storage.db_path, "original")
    backup, _ = svc.build_export(include_cache=False)
    _set_probe(isolated_storage.db_path, "changed")

    broken = BytesIO()
    with zipfile.ZipFile(BytesIO(backup)) as source, zipfile.ZipFile(broken, "w", compression=zipfile.ZIP_DEFLATED) as target:
        manifest = BackupManifest.model_validate_json(source.read("manifest.json"))
        manifest.db_sha256 = "0" * 64
        for info in source.infolist():
            if info.filename == "manifest.json":
                target.writestr("manifest.json", manifest.model_dump_json())
            else:
                target.writestr(info, source.read(info.filename))

    with pytest.raises(BackupError, match="DB file hash mismatch"):
        svc.apply_import(broken.getvalue())

    assert _get_probe(isolated_storage.db_path) == "changed"


def test_import_rejects_unsafe_manifest_paths(isolated_storage) -> None:
    _set_probe(isolated_storage.db_path, "original")
    backup, manifest = svc.build_export(include_cache=False)
    evil = b"evil"
    manifest.files.append(
        BackupManifestFileEntry(path="../evil.txt", sha256=svc._sha256_bytes(evil), size=len(evil))
    )

    broken = BytesIO()
    with zipfile.ZipFile(BytesIO(backup)) as source, zipfile.ZipFile(broken, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            if info.filename == "manifest.json":
                target.writestr("manifest.json", manifest.model_dump_json())
            else:
                target.writestr(info, source.read(info.filename))
        target.writestr("../evil.txt", evil)

    with pytest.raises(BackupError, match="unsafe path"):
        svc.apply_import(broken.getvalue())


def test_import_rejects_newer_schema_backup(isolated_storage, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_probe(isolated_storage.db_path, "original")
    backup, _ = svc.build_export(include_cache=False)
    monkeypatch.setattr(svc, "_current_alembic_head", lambda: "20260609b001")

    with pytest.raises(BackupImportVersionMismatch):
        svc.apply_import(backup)


def test_import_releases_maintenance_mode_when_worker_drain_times_out(
    isolated_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_probe(isolated_storage.db_path, "original")
    backup, _ = svc.build_export(include_cache=False)
    _set_probe(isolated_storage.db_path, "changed")
    monkeypatch.setattr(
        svc,
        "wait_for_workers_to_drain",
        lambda: (_ for _ in ()).throw(WorkerDrainTimeout("active worker")),
    )

    with pytest.raises(BackupImportBusy, match="active worker"):
        svc.apply_import(backup)

    assert _get_probe(isolated_storage.db_path) == "changed"
    assert _get_maintenance_mode(isolated_storage.db_path) == "idle"
