from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vsniper.core.database import session_scope
from vsniper.db.models import (
    AlertDeliveryState,
    Candidate,
    Search,
    SearchRun,
    TasteState,
    WorkerHeartbeat,
)
from vsniper.domain.contracts import (
    DeliveryQueueSummary,
    OperationsSnapshot,
    SearchClaimStatus,
    SearchRunPage,
    SearchRunRecord,
    WorkerActivityRecord,
    WorkerHeartbeatRecord,
)
from vsniper.services._mapping import as_aware, candidate_to_contract
from vsniper.services.maintenance_service import (
    active_workers,
    get_maintenance_state,
)

HEARTBEAT_ROW_ID = 1
CLAIM_STALE_THRESHOLD = timedelta(minutes=10)

_logger = logging.getLogger(__name__)


def _search_run_to_record(row: SearchRun) -> SearchRunRecord:
    return SearchRunRecord(
        id=row.id,
        search_id=row.search_id,
        mode=row.mode,  # type: ignore[arg-type]
        trigger=row.trigger or "worker",
        status=row.status or "running",
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        fetched_count=row.fetched_count,
        judged_count=row.judged_count,
        new_judged_count=row.new_judged_count,
        alert_count=row.alert_count,
        queued_delivery_count=row.queued_delivery_count,
        failures_by_reason=row.failures_by_reason,
        judge_provider=row.judge_provider,
        judge_model=row.judge_model,
        fallback_used=row.fallback_used,
        vinted_status=row.vinted_status,
        vinted_detail=row.vinted_detail,
        cost_usd=row.cost_usd,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        error=row.error,
    )


def _get_heartbeat() -> WorkerHeartbeatRecord | None:
    with session_scope() as session:
        row = session.get(WorkerHeartbeat, HEARTBEAT_ROW_ID)
        if row is None:
            return None
        return WorkerHeartbeatRecord(
            owner=row.owner or "",
            cycle_count=row.cycle_count,
            phase=row.phase or "",
            last_heartbeat_at=row.last_heartbeat_at,
            last_cycle_started_at=row.last_cycle_started_at,
            last_cycle_finished_at=row.last_cycle_finished_at,
        )


def _get_search_claims() -> list[SearchClaimStatus]:
    now = datetime.now(UTC)
    with session_scope() as session:
        rows = session.scalars(
            select(Search).where(Search.run_status.in_(("running", "failed")))
        ).all()
        claims = []
        for row in rows:
            claimed_at = as_aware(row.last_claimed_at)
            age = (now - claimed_at).total_seconds() if claimed_at else None
            is_stale = age is not None and age > CLAIM_STALE_THRESHOLD.total_seconds()
            claims.append(
                SearchClaimStatus(
                    search_id=row.id,
                    search_name=row.name,
                    clothing_item=row.clothing_item,  # type: ignore[arg-type]
                    run_status=row.run_status,
                    last_claimed_at=claimed_at,
                    last_run_at=as_aware(row.last_run_at),
                    claim_age_seconds=age,
                    is_stale=is_stale,
                )
            )
        return claims


def _get_delivery_summary() -> DeliveryQueueSummary:
    with session_scope() as session:
        counts = dict(
            session.execute(
                select(AlertDeliveryState.status, func.count())
                .group_by(AlertDeliveryState.status)
            ).all()
        )
        failure_rows = session.execute(
            select(Candidate)
            .join(AlertDeliveryState, AlertDeliveryState.candidate_id == Candidate.id)
            .where(AlertDeliveryState.status == "failed")
            .order_by(AlertDeliveryState.updated_at.desc())
            .limit(10)
        ).scalars().all()
        return DeliveryQueueSummary(
            pending=counts.get("pending", 0),
            processing=counts.get("processing", 0),
            sent=counts.get("sent", 0),
            failed=counts.get("failed", 0),
            latest_failures=[candidate_to_contract(c) for c in failure_rows],
        )


def _get_recompute_status() -> str:
    with session_scope() as session:
        state = session.get(TasteState, 1)
        if state is None:
            return "idle"
        return state.recompute_status or "idle"


def get_operations_snapshot() -> OperationsSnapshot:
    maint = get_maintenance_state()
    heartbeat = _get_heartbeat()
    tasks = [
        WorkerActivityRecord(
            id=t["id"],
            operation=t["operation"],
            started_at=t["started_at"],
            heartbeat_at=t["heartbeat_at"],
        )
        for t in active_workers()
    ]
    recompute = _get_recompute_status()
    delivery = _get_delivery_summary()
    claims = _get_search_claims()

    with session_scope() as session:
        run_rows = session.scalars(
            select(SearchRun).order_by(SearchRun.started_at.desc()).limit(50)
        ).all()
        recent_runs = [_search_run_to_record(r) for r in run_rows]

    return OperationsSnapshot(
        maintenance_mode=maint["mode"],  # type: ignore[arg-type]
        maintenance_reason=maint["reason"],
        maintenance_started_at=maint["started_at"],
        worker_heartbeat=heartbeat,
        active_worker_tasks=tasks,
        recompute_status=recompute,
        delivery_summary=delivery,
        search_claims=claims,
        recent_runs=recent_runs,
    )


def get_search_runs(
    *,
    search_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> SearchRunPage:
    with session_scope() as session:
        stmt = select(SearchRun)
        count_stmt = select(func.count()).select_from(SearchRun)
        if search_id:
            stmt = stmt.where(SearchRun.search_id == search_id)
            count_stmt = count_stmt.where(SearchRun.search_id == search_id)
        if status:
            stmt = stmt.where(SearchRun.status == status)
            count_stmt = count_stmt.where(SearchRun.status == status)
        total = int(session.scalar(count_stmt) or 0)
        rows = session.scalars(
            stmt.order_by(SearchRun.started_at.desc()).offset(offset).limit(limit)
        ).all()
        return SearchRunPage(
            items=[_search_run_to_record(r) for r in rows],
            total=total,
        )


def record_heartbeat(
    *,
    owner: str,
    cycle_count: int,
    phase: str,
    cycle_started_at: datetime | None = None,
    cycle_finished_at: datetime | None = None,
) -> None:
    now = datetime.now(UTC)
    with session_scope() as session:
        session.execute(
            sqlite_insert(WorkerHeartbeat)
            .values(
                id=HEARTBEAT_ROW_ID,
                owner=owner,
                cycle_count=cycle_count,
                phase=phase,
                last_heartbeat_at=now,
                last_cycle_started_at=cycle_started_at,
                last_cycle_finished_at=cycle_finished_at,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[WorkerHeartbeat.id],
                set_={
                    "owner": owner,
                    "cycle_count": cycle_count,
                    "phase": phase,
                    "last_heartbeat_at": now,
                    "last_cycle_finished_at": cycle_finished_at,
                    "updated_at": now,
                    **({"last_cycle_started_at": cycle_started_at} if cycle_started_at else {}),
                },
            )
        )


def close_orphaned_running_runs(search_id: str) -> list[int]:
    """Marks any SearchRun row for search_id still status="running" as failed.

    Called when claim_for_run successfully (re)claims a search: a SearchRun left "running" at
    that point belongs to a prior attempt whose process was killed before it could call
    finish_search_run, so it would otherwise show up forever as "running" in run history.
    """
    now = datetime.now(UTC)
    with session_scope() as session:
        run_ids = list(
            session.scalars(
                select(SearchRun.id)
                .where(SearchRun.search_id == search_id)
                .where(SearchRun.status == "running")
            )
        )
        session.execute(
            update(SearchRun)
            .where(SearchRun.search_id == search_id)
            .where(SearchRun.status == "running")
            .values(
                status="failed",
                finished_at=now,
                error="Orphaned: process was killed or crashed before this run could finish.",
            )
        )
        return run_ids


def create_search_run(
    *,
    search_id: str,
    mode: str,
    trigger: str = "worker",
) -> int:
    now = datetime.now(UTC)
    with session_scope() as session:
        run = SearchRun(
            search_id=search_id,
            mode=mode,
            trigger=trigger,
            status="running",
            started_at=now,
        )
        session.add(run)
        session.flush()
        return run.id


def finish_search_run(
    *,
    run_id: int,
    status: str = "completed",
    fetched_count: int = 0,
    judged_count: int = 0,
    new_judged_count: int = 0,
    alert_count: int = 0,
    queued_delivery_count: int = 0,
    failures_by_reason: dict[str, int] | None = None,
    judge_provider: str | None = None,
    judge_model: str | None = None,
    fallback_used: bool = False,
    vinted_status: str | None = None,
    vinted_detail: str | None = None,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error: str | None = None,
) -> None:
    now = datetime.now(UTC)
    with session_scope() as session:
        run = session.get(SearchRun, run_id)
        if run is None:
            return
        started = as_aware(run.started_at)
        duration_ms = int((now - started).total_seconds() * 1000) if started else None
        run.status = status
        run.finished_at = now
        run.duration_ms = duration_ms
        run.fetched_count = fetched_count
        run.judged_count = judged_count
        run.new_judged_count = new_judged_count
        run.alert_count = alert_count
        run.queued_delivery_count = queued_delivery_count
        run.failures_by_reason = failures_by_reason
        run.judge_provider = judge_provider
        run.judge_model = judge_model
        run.fallback_used = fallback_used
        run.vinted_status = vinted_status
        run.vinted_detail = vinted_detail
        run.cost_usd = cost_usd
        run.input_tokens = input_tokens
        run.output_tokens = output_tokens
        run.error = error
