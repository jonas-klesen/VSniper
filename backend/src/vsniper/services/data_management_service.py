"""Full-state export/import as a ZIP archive.

Export captures a transactionally-consistent snapshot of the SQLite DB (via the
sqlite3 online backup API, which doesn't block the worker) plus the `uploads/`
and `feedback-assets/` trees and, optionally, the `cache/` tree. Import validates the archive's manifest
and per-file hashes, pauses worker phases through DB-backed maintenance state,
backs up the current `storage/`, then overwrites the DB file and file trees in
place, runs Alembic to head, and reloads `AppState`."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from posixpath import normpath
from typing import Iterator

from alembic.config import Config
from alembic.script import ScriptDirectory

from vsniper.core.config import get_settings
from vsniper.core.database import dispose_engine, init_database
from vsniper.domain.contracts import BackupImportResult, BackupManifest, BackupManifestFileEntry
from vsniper.services.maintenance_service import (
    MaintenanceBusy,
    begin_import_maintenance,
    finish_import_maintenance,
    wait_for_workers_to_drain,
)

logger = logging.getLogger(__name__)

BACKUP_FORMAT_VERSION = 1
DB_ARCHIVE_PATH = "db/vsniper.db"
MANIFEST_PATH = "manifest.json"

# Filenames/dirs that must never enter a backup (runtime locks, WAL sidecars,
# legacy dirs superseded by `taste/`).
_EXCLUDED_DIR_NAMES = {".pre-import-backup-tmp"}
_EXCLUDED_FILE_SUFFIXES = ("-wal", "-shm", "-journal")
_EXCLUDED_FILE_NAMES = {".migration.lock"}
_RESTORABLE_PREFIXES = ("uploads/", "cache/", "feedback-assets/")


class BackupError(Exception):
    """Raised for user-facing backup/restore failures (bad archive, version
    mismatch, integrity check failure). Surfaces as HTTP 400 by the route."""


class BackupImportVersionMismatch(BackupError):
    """The imported DB's Alembic head is newer than this codebase supports
    (would require a downgrade)."""


class BackupImportBusy(BackupError):
    """Import could not safely pause workers before replacing state."""


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _storage_root() -> Path:
    settings = get_settings()
    db_path = settings.resolve_path(settings.sqlite_path)
    # Configured layout is storage/sqlite/vsniper.db. Deriving from the DB path
    # keeps tests and alternate deployments isolated when absolute paths are set.
    return db_path.parent.parent


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _should_exclude(rel_path: Path) -> bool:
    name = rel_path.name
    if name in _EXCLUDED_FILE_NAMES:
        return True
    if any(name.endswith(suffix) for suffix in _EXCLUDED_FILE_SUFFIXES):
        return True
    parts = rel_path.parts
    if any(part in _EXCLUDED_DIR_NAMES for part in parts):
        return True
    return False


def _walk_tree(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root)
            if not _should_exclude(rel):
                yield path


def _current_alembic_head() -> str:
    """The Alembic revision id this codebase considers `head`."""
    return _script_directory().get_current_head() or ""


def _db_alembic_version(db_path: Path) -> str:
    """Read the `alembic_version` table from an arbitrary SQLite file."""
    if not db_path.exists():
        return ""
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute("SELECT version_num FROM alembic_version LIMIT 1")
        row = cursor.fetchone()
        return row[0] if row else ""
    except sqlite3.DatabaseError:
        return ""
    finally:
        conn.close()


def _snapshot_db(db_path: Path, dest_path: Path) -> None:
    """Online backup of the live WAL-mode DB into a self-contained file."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db_path))
    dest = sqlite3.connect(str(dest_path))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()


def _collect_file_entries(storage_root: Path, tree_subdir: str) -> list[tuple[Path, str]]:
    """Return [(absolute_path, archive_rel_path)] for one storage tree.

    `tree_subdir` is `uploads`, `feedback-assets`, or `cache`; the archive stores files under
    `{tree_subdir}/...` matching their relative location under storage."""
    tree_root = storage_root / tree_subdir
    out: list[tuple[Path, str]] = []
    for abs_path in _walk_tree(tree_root):
        rel = abs_path.relative_to(tree_root)
        archive_rel = f"{tree_subdir}/{rel.as_posix()}"
        out.append((abs_path, archive_rel))
    return out


def build_export_file(include_cache: bool) -> tuple[Path, BackupManifest]:
    """Build a full backup ZIP in a temporary file and return its path.

    The route streams this file and deletes it in a background task. A temp file
    avoids holding potentially large cache-heavy backups in process memory.
    """
    settings = get_settings()
    storage_root = _storage_root()
    db_path = settings.resolve_path(settings.sqlite_path)
    if not db_path.exists():
        raise BackupError(f"SQLite database does not exist at {db_path}.")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        snapshot_path = tmp / "vsniper.db"
        logger.info("export snapshotting db via online backup -> %s", snapshot_path)
        _snapshot_db(db_path, snapshot_path)
        db_sha, db_size = _sha256_file(snapshot_path)

        file_entries: list[tuple[Path, str]] = []
        file_entries.extend(_collect_file_entries(storage_root, "uploads"))
        file_entries.extend(_collect_file_entries(storage_root, "feedback-assets"))
        if include_cache:
            file_entries.extend(_collect_file_entries(storage_root, "cache"))

        manifest_files: list[BackupManifestFileEntry] = []
        zip_file = tempfile.NamedTemporaryFile(prefix="vsniper-backup-", suffix=".zip", delete=False)
        zip_path = Path(zip_file.name)
        zip_file.close()

        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for abs_path, archive_rel in file_entries:
                data = abs_path.read_bytes()
                zf.writestr(archive_rel, data)
                manifest_files.append(
                    BackupManifestFileEntry(
                        path=archive_rel,
                        sha256=_sha256_bytes(data),
                        size=len(data),
                    )
                )
            zf.write(snapshot_path, DB_ARCHIVE_PATH)

        manifest = BackupManifest(
            format_version=BACKUP_FORMAT_VERSION,
            created_at=datetime.now(UTC),
            alembic_head=_current_alembic_head(),
            include_cache=include_cache,
            db_sha256=db_sha,
            db_size=db_size,
            files=manifest_files,
        )
        with zipfile.ZipFile(zip_path, mode="a", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.writestr(MANIFEST_PATH, manifest.model_dump_json(indent=2))

        return zip_path, manifest


def build_export(include_cache: bool) -> tuple[bytes, BackupManifest]:
    """Test/convenience wrapper returning the ZIP as bytes."""
    zip_path, manifest = build_export_file(include_cache=include_cache)
    try:
        return zip_path.read_bytes(), manifest
    finally:
        zip_path.unlink(missing_ok=True)


def _read_zip_manifest(zf: zipfile.ZipFile) -> BackupManifest:
    if MANIFEST_PATH not in zf.namelist():
        raise BackupError(f"Backup ZIP is missing {MANIFEST_PATH} at its root.")
    try:
        return BackupManifest.model_validate_json(zf.read(MANIFEST_PATH))
    except Exception as exc:
        raise BackupError(f"Backup manifest is invalid: {exc}") from exc


def _safe_archive_path(path: str) -> str:
    normalized = normpath(path.replace("\\", "/"))
    if normalized in {"", "."} or normalized.startswith("../") or normalized.startswith("/"):
        raise BackupError(f"Backup contains unsafe path: {path}")
    return normalized


def _validate_manifest_paths(manifest: BackupManifest) -> None:
    seen: set[str] = set()
    for entry in manifest.files:
        normalized = _safe_archive_path(entry.path)
        if normalized != entry.path:
            raise BackupError(f"Backup manifest path is not normalized: {entry.path}")
        if not normalized.startswith(_RESTORABLE_PREFIXES):
            raise BackupError(f"Backup manifest contains unsupported file path: {entry.path}")
        if normalized in seen:
            raise BackupError(f"Backup manifest contains duplicate file path: {entry.path}")
        seen.add(normalized)


def _read_archive_member(zf: zipfile.ZipFile, path: str) -> bytes:
    _safe_archive_path(path)
    try:
        return zf.read(path)
    except KeyError as exc:
        raise BackupError(f"Backup ZIP is missing {path}.") from exc


def _replace_tree(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)


def _write_tree_file(root: Path, rel_path: str, data: bytes) -> None:
    normalized = _safe_archive_path(rel_path)
    target = (root / normalized).resolve()
    root_resolved = root.resolve()
    if not (target == root_resolved or root_resolved in target.parents):
        raise BackupError(f"Backup path escapes restore root: {rel_path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _backup_current_storage(storage_root: Path, db_path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = storage_root / f".pre-import-backup-{timestamp}"
    suffix = 1
    while backup_dir.exists():
        backup_dir = storage_root / f".pre-import-backup-{timestamp}-{suffix}"
        suffix += 1
    backup_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        (backup_dir / "sqlite").mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_path, backup_dir / "sqlite" / "vsniper.db")
    for sub in ("uploads", "cache", "feedback-assets"):
        src = storage_root / sub
        if src.exists():
            shutil.copytree(src, backup_dir / sub)
    return backup_dir


def _revision_is_newer_than_code(imported_head: str, current_head: str) -> bool:
    if not imported_head or not current_head or imported_head == current_head:
        return False
    script_dir = _script_directory()
    # walk_revisions() yields newest -> oldest. If the imported revision appears
    # before this codebase's head, the backup came from a newer app version.
    ordered = [r.revision for r in script_dir.walk_revisions()]
    if imported_head not in ordered or current_head not in ordered:
        return True
    return ordered.index(imported_head) < ordered.index(current_head)


def apply_import(zip_bytes: bytes) -> BackupImportResult:
    """Bytes convenience wrapper used by tests."""
    try:
        zf = zipfile.ZipFile(BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise BackupError(f"Uploaded file is not a valid ZIP archive: {exc}") from exc
    with zf:
        return _apply_import_zip(zf)


def apply_import_file(zip_path: Path) -> BackupImportResult:
    """Validate and apply a full-state backup ZIP from disk."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise BackupError(f"Uploaded file is not a valid ZIP archive: {exc}") from exc
    with zf:
        return _apply_import_zip(zf)


def _apply_import_zip(zf: zipfile.ZipFile) -> BackupImportResult:
    """Validate and apply a full-state backup ZIP, overwriting the live DB and
    file trees. Returns a summary of what was restored."""
    settings = get_settings()
    storage_root = _storage_root()
    db_path = settings.resolve_path(settings.sqlite_path)
    uploads_root = settings.resolve_path(settings.upload_dir)
    cache_root = settings.resolve_path(settings.cache_dir)
    feedback_asset_root = settings.resolve_path(settings.feedback_asset_dir)

    if DB_ARCHIVE_PATH not in zf.namelist():
        raise BackupError(f"Backup ZIP is missing {DB_ARCHIVE_PATH}.")
    manifest = _read_zip_manifest(zf)
    if manifest.format_version != BACKUP_FORMAT_VERSION:
        raise BackupError(
            f"Unsupported backup format version {manifest.format_version} (expected {BACKUP_FORMAT_VERSION})."
        )
    _validate_manifest_paths(manifest)

    # Read and hash-verify every payload before touching live state, so a
    # corrupt archive never leaves us half-overwritten.
    db_bytes = zf.read(DB_ARCHIVE_PATH)
    if _sha256_bytes(db_bytes) != manifest.db_sha256:
        raise BackupError("DB file hash mismatch — archive is corrupt or was tampered with.")
    if len(db_bytes) != manifest.db_size:
        raise BackupError("DB file size mismatch — archive is corrupt or was tampered with.")

    file_payloads: list[tuple[str, bytes]] = []
    skipped: list[str] = []
    for entry in manifest.files:
        data = _read_archive_member(zf, entry.path)
        if _sha256_bytes(data) != entry.sha256:
            raise BackupError(f"File hash mismatch for {entry.path} — archive is corrupt or was tampered with.")
        if len(data) != entry.size:
            raise BackupError(f"File size mismatch for {entry.path} — archive is corrupt or was tampered with.")
        file_payloads.append((entry.path, data))

    # Sanity-check the imported DB's alembic head against the codebase.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        tmp_db.write(db_bytes)
        tmp_db_path = Path(tmp_db.name)
    try:
        imported_head = _db_alembic_version(tmp_db_path)
        current_head = _current_alembic_head()
        if _revision_is_newer_than_code(imported_head, current_head):
            raise BackupImportVersionMismatch(
                f"Imported DB is at Alembic revision {imported_head or 'unknown'}, but this "
                f"codebase tops out at {current_head or 'unknown'}. Downgrade is not supported."
            )
    finally:
        tmp_db_path.unlink(missing_ok=True)

    operation_id: str | None = None
    db_replaced = False
    try:
        try:
            operation_id = begin_import_maintenance()
            wait_for_workers_to_drain()
        except MaintenanceBusy as exc:
            raise BackupImportBusy(str(exc)) from exc

        backup_dir = _backup_current_storage(storage_root, db_path)
        logger.info("pre-import backup written to %s", backup_dir)

        # Tear down this process' SQLAlchemy pool so nothing holds the old DB inode open when we
        # swap it out. Other worker processes have drained before this point and will skip new work
        # while maintenance is active.
        dispose_engine()

        # Overwrite the DB file atomically.
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Clear WAL sidecars so the new file isn't paired with stale WAL state.
        for suffix in _EXCLUDED_FILE_SUFFIXES:
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        tmp_replace = db_path.with_suffix(db_path.suffix + ".import-tmp")
        tmp_replace.write_bytes(db_bytes)
        os.replace(tmp_replace, db_path)
        db_replaced = True

        # Restore file trees. Clear targets first so deleted-on-source files
        # don't linger. Cache is always cleared; if the archive didn't include
        # it, it will rebuild lazily from future scans.
        _replace_tree(uploads_root)
        _replace_tree(feedback_asset_root)
        _replace_tree(cache_root)
        restored_files = 0
        for archive_rel, data in file_payloads:
            if archive_rel.startswith("uploads/"):
                rel = archive_rel[len("uploads/"):]
                _write_tree_file(uploads_root, rel, data)
            elif archive_rel.startswith("feedback-assets/"):
                rel = archive_rel[len("feedback-assets/"):]
                _write_tree_file(feedback_asset_root, rel, data)
            elif archive_rel.startswith("cache/"):
                rel = archive_rel[len("cache/"):]
                _write_tree_file(cache_root, rel, data)
            else:
                skipped.append(f"{archive_rel}: unknown tree")
                continue
            restored_files += 1

        # Bring the (possibly older) imported schema up to head. Idempotent if
        # already at head; raises for unsupported downgrade (caught above).
        init_database()
        finish_import_maintenance()
    except Exception:
        if operation_id is not None and not db_replaced:
            try:
                finish_import_maintenance(operation_id)
            except Exception:
                logger.exception("failed to release import maintenance mode after import failure")
        raise

    # Reload AppState so the running API/worker see the new DB. Done outside the
    # `with zf` block so the archive is closed first.
    from vsniper.core.state import get_state

    reloaded = True
    try:
        get_state().reload()
    except Exception:
        logger.exception("AppState reload after import failed; restart the API/worker to pick up the new state")
        reloaded = False

    final_head = _db_alembic_version(db_path)
    logger.info(
        "import applied restored_files=%d skipped=%d alembic_head=%s reloaded=%s",
        restored_files,
        len(skipped),
        final_head,
        reloaded,
    )
    return BackupImportResult(
        restored_db=True,
        restored_files=restored_files,
        skipped_files=skipped,
        alembic_head=final_head,
        reloaded=reloaded,
    )


def _script_directory():
    backend_root = Path(__file__).resolve().parents[3]
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return ScriptDirectory.from_config(cfg)
