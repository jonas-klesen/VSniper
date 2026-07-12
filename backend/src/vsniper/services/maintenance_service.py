from __future__ import annotations

import logging
import os
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from vsniper.core.database import session_scope
from vsniper.db.models import MaintenanceState, WorkerActivityState

logger = logging.getLogger(__name__)

MAINTENANCE_ROW_ID = 1
WORKER_ACTIVITY_STALE_AFTER = timedelta(hours=1)
IMPORT_DRAIN_TIMEOUT_SECONDS = 60
IMPORT_DRAIN_POLL_SECONDS = 0.25


class MaintenanceBusy(RuntimeError):
    pass


class MaintenanceImportActive(MaintenanceBusy):
    """Raised when manual pause is requested but import maintenance is active."""


class MaintenanceNotPaused(RuntimeError):
    """Raised when resume is requested but mode is not manual."""


class WorkerDrainTimeout(MaintenanceBusy):
    pass


def _owner_label() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _ensure_maintenance_row(session, now: datetime) -> None:
    session.execute(
        sqlite_insert(MaintenanceState)
        .values(id=MAINTENANCE_ROW_ID, mode="idle", owner="", reason="", updated_at=now)
        .on_conflict_do_nothing()
    )


def _purge_stale_worker_activity(session, now: datetime) -> None:
    cutoff = now - WORKER_ACTIVITY_STALE_AFTER
    session.execute(delete(WorkerActivityState).where(WorkerActivityState.heartbeat_at < cutoff))


def begin_manual_pause(*, reason: str = "") -> None:
    now = datetime.now(UTC)
    with session_scope() as session:
        _ensure_maintenance_row(session, now)
        state = session.get(MaintenanceState, MAINTENANCE_ROW_ID)
        if state is not None and state.mode == "import":
            raise MaintenanceImportActive(
                "Import maintenance is active. Wait for the import to finish before pausing."
            )
        if state is not None and state.mode == "manual":
            return
        session.execute(
            update(MaintenanceState)
            .where(MaintenanceState.id == MAINTENANCE_ROW_ID)
            .where(MaintenanceState.mode != "import")
            .values(
                mode="manual",
                operation_id=None,
                owner=_owner_label(),
                reason=reason or "Manual pause",
                started_at=now,
                updated_at=now,
            )
        )


def end_manual_pause() -> None:
    now = datetime.now(UTC)
    with session_scope() as session:
        _ensure_maintenance_row(session, now)
        session.execute(
            update(MaintenanceState)
            .where(MaintenanceState.id == MAINTENANCE_ROW_ID)
            .where(MaintenanceState.mode == "manual")
            .values(mode="idle", operation_id=None, owner="", reason="", started_at=None, updated_at=now)
        )


def begin_import_maintenance(*, reason: str = "Full-state import") -> str:
    operation_id = f"import-{uuid4().hex[:12]}"
    now = datetime.now(UTC)
    with session_scope() as session:
        _ensure_maintenance_row(session, now)
        result = session.execute(
            update(MaintenanceState)
            .where(MaintenanceState.id == MAINTENANCE_ROW_ID)
            .where(MaintenanceState.mode == "idle")
            .values(
                mode="import",
                operation_id=operation_id,
                owner=_owner_label(),
                reason=reason,
                started_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            state = session.get(MaintenanceState, MAINTENANCE_ROW_ID)
            current_mode = state.mode if state else "unknown"
            if current_mode == "manual":
                session.execute(
                    update(MaintenanceState)
                    .where(MaintenanceState.id == MAINTENANCE_ROW_ID)
                    .where(MaintenanceState.mode == "manual")
                    .values(
                        mode="import",
                        operation_id=operation_id,
                        owner=_owner_label(),
                        reason=reason,
                        started_at=now,
                        updated_at=now,
                    )
                )
                return operation_id
            raise MaintenanceBusy(
                f"Maintenance is already active ({current_mode}). Try again after it finishes."
            )
    return operation_id


def finish_import_maintenance(operation_id: str | None = None) -> None:
    now = datetime.now(UTC)
    with session_scope() as session:
        _ensure_maintenance_row(session, now)
        stmt = (
            update(MaintenanceState)
            .where(MaintenanceState.id == MAINTENANCE_ROW_ID)
            .values(mode="idle", operation_id=None, owner="", reason="", started_at=None, updated_at=now)
        )
        if operation_id is not None:
            stmt = stmt.where(MaintenanceState.operation_id == operation_id)
        session.execute(stmt)


def get_maintenance_state() -> dict:
    now = datetime.now(UTC)
    with session_scope() as session:
        _ensure_maintenance_row(session, now)
        state = session.get(MaintenanceState, MAINTENANCE_ROW_ID)
        if state is None:
            return {"mode": "idle", "reason": "", "started_at": None, "owner": ""}
        return {
            "mode": state.mode or "idle",
            "reason": state.reason or "",
            "started_at": state.started_at,
            "owner": state.owner or "",
        }


def active_worker_count() -> int:
    now = datetime.now(UTC)
    with session_scope() as session:
        _purge_stale_worker_activity(session, now)
        return int(session.scalar(select(func.count()).select_from(WorkerActivityState)) or 0)


def active_workers() -> list[dict]:
    now = datetime.now(UTC)
    with session_scope() as session:
        _purge_stale_worker_activity(session, now)
        rows = session.execute(select(WorkerActivityState)).scalars().all()
        return [
            {
                "id": row.id,
                "operation": row.operation,
                "started_at": row.started_at,
                "heartbeat_at": row.heartbeat_at,
            }
            for row in rows
        ]


def wait_for_workers_to_drain(
    *,
    timeout_seconds: float = IMPORT_DRAIN_TIMEOUT_SECONDS,
    poll_seconds: float = IMPORT_DRAIN_POLL_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        count = active_worker_count()
        if count == 0:
            return
        if time.monotonic() >= deadline:
            raise WorkerDrainTimeout(
                f"Timed out waiting for {count} active worker task{'s' if count != 1 else ''} to finish."
            )
        time.sleep(poll_seconds)


@contextmanager
def worker_activity(operation: str):
    activity_id = f"{_owner_label()}:{threading.get_ident()}:{uuid4().hex[:8]}"
    registered = False
    active = False
    now = datetime.now(UTC)
    try:
        with session_scope() as session:
            _ensure_maintenance_row(session, now)
            state = session.get(MaintenanceState, MAINTENANCE_ROW_ID)
            active = state is not None and state.mode == "idle"
            if active:
                session.add(
                    WorkerActivityState(
                        id=activity_id,
                        operation=operation,
                        started_at=now,
                        heartbeat_at=now,
                    )
                )
                registered = True
    except SQLAlchemyError:
        logger.warning("worker maintenance check failed; skipping %s", operation, exc_info=True)
        active = False

    try:
        yield active
    finally:
        if registered:
            try:
                with session_scope() as session:
                    session.execute(delete(WorkerActivityState).where(WorkerActivityState.id == activity_id))
            except SQLAlchemyError:
                logger.warning("failed to clear worker activity %s", activity_id, exc_info=True)
