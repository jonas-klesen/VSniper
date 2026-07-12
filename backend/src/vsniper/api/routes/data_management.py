"""Full-state export/import endpoints.

`GET /api/data/export` streams a ZIP containing a transactionally-consistent
SQLite snapshot plus the uploads tree (and optionally the cache tree).
`POST /api/data/import` accepts such a ZIP, validates it, and overwrites the
live DB and file trees, then reloads AppState."""
import logging
import tempfile
from datetime import timezone
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from vsniper.domain.contracts import BackupImportResult
from vsniper.services.data_management_service import (
    BackupError,
    BackupImportBusy,
    BackupImportVersionMismatch,
    apply_import_file,
    build_export_file,
)

router = APIRouter(tags=["data"])
logger = logging.getLogger(__name__)

# 1 GB cap — archives can be large when cache images are included. Matches the
# rough upper bound of a few thousand cached candidate images plus uploads.
MAX_BACKUP_UPLOAD_BYTES = 1024 * 1024 * 1024


@router.get("/data/export")
def export_data(include_cache: bool = Query(default=False)) -> StreamingResponse:
    try:
        zip_path, manifest = build_export_file(include_cache=include_cache)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("data export failed")
        raise
    timestamp = manifest.created_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"vsniper-backup-{timestamp}.zip"

    def iter_file(path: Path) -> Iterator[bytes]:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                yield chunk

    return StreamingResponse(
        iter_file(zip_path),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Backup-Alembic-Head": manifest.alembic_head,
            "X-Backup-Include-Cache": "true" if manifest.include_cache else "false",
        },
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@router.post(
    "/data/import",
    response_model=BackupImportResult,
    status_code=status.HTTP_200_OK,
)
async def import_data(file: UploadFile = File(...)) -> BackupImportResult:
    tmp_path: Path | None = None
    try:
        total = 0
        with tempfile.NamedTemporaryFile(prefix="vsniper-import-", suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BACKUP_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Uploaded backup exceeds the 1 GB limit.")
                tmp.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="Uploaded backup is empty.")
        return apply_import_file(tmp_path)
    except BackupImportBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BackupImportVersionMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("data import failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        await file.close()
