from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from datetime import UTC, datetime, timedelta

from vsniper.core.config import get_settings
from vsniper.core.database import dispose_engine
from vsniper.core.state import get_state
from vsniper.domain.contracts import SearchRecord
from vsniper.services.maintenance_service import worker_activity
from vsniper.services.operations_service import record_heartbeat
from vsniper.worker.jobs.check_cookie_expiry import run_once as check_cookie_expiry
from vsniper.worker.jobs.process_deliveries import run_once as process_deliveries_once
from vsniper.worker.jobs.prune_records import run_once as prune_records_once
from vsniper.worker.jobs.scan_search import run_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

_logger = logging.getLogger(__name__)
_search_failure_counts: dict[str, int] = {}
_search_failure_lock = threading.Lock()
_stop = False
_cycle_count = 0

_WORKER_OWNER = f"{socket.gethostname()}:{os.getpid()}"
_CYCLE_TIMEOUT_SECONDS = 300
_WORKER_CLAIM_STALE_AFTER = timedelta(seconds=_CYCLE_TIMEOUT_SECONDS + 120)

# The continuous loop wakes on this short, fixed cadence regardless of the (much larger) scan
# interval, so it can service maintenance and dispatch staggered scans promptly.
_POLL_SECONDS = 30
# Delivery/cookie/prune run on their own fixed cadence, decoupled from the scan interval — those
# should stay responsive even when scans are spread across a 30+ minute window.
_MAINTENANCE_TICK_SECONDS = 60

_rotation_index = 0
_active_scan_threads: set[threading.Thread] = set()
_active_scan_lock = threading.Lock()


def _record_worker_error(operation: str, summary: str, exc: BaseException) -> None:
    try:
        get_state().errors.record(
            source="worker",
            operation=operation,
            summary=summary,
            exception=exc,
            details={"worker_owner": _WORKER_OWNER, "cycle_count": _cycle_count},
        )
    except Exception:
        _logger.exception("could not persist worker error event")


def _run_one(search_id: str, search_name: str, interval: int) -> None:
    state = get_state()
    if not state.searches.claim_for_run(
        search_id, timedelta(seconds=interval), stale_after=_WORKER_CLAIM_STALE_AFTER
    ):
        _logger.info("skipping '%s': claimed by another worker", search_name)
        return

    try:
        result = run_once(search_id)
        with _search_failure_lock:
            _search_failure_counts.pop(search_id, None)
        _logger.info("%s", result.summary)
    except Exception:
        with _search_failure_lock:
            _search_failure_counts[search_id] = _search_failure_counts.get(search_id, 0) + 1
            count = _search_failure_counts[search_id]
        _logger.exception("live run for '%s' failed (failure #%d this session)", search_name, count)


def cycle(interval: int) -> None:
    """Run a single full pass: scan every enabled search at once, then delivery/cookie/prune
    maintenance. Used for `--once` (a one-shot manual/test run). The continuous loop (`main`
    without `--once`) instead staggers scans across `interval` via `_dispatch_due_scans` so
    requests spread out evenly instead of firing all at once."""
    with worker_activity("scan_searches") as active:
        if not active:
            dispose_engine()
            _logger.info("worker cycle skipped: maintenance is active")
            _run_maintenance()
            return
        state = get_state()
        enabled = [s for s in state.searches.all() if s.enabled]
        if not enabled:
            _logger.info("worker idle: no enabled searches")
        else:
            max_workers = max(1, min(len(enabled), state.settings.worker_max_concurrency))
            pool = ThreadPoolExecutor(max_workers=max_workers)
            timed_out = False
            try:
                futures = {pool.submit(_run_one, s.id, s.name, interval): (s.id, s.name) for s in enabled}
                try:
                    for fut in as_completed(futures, timeout=_CYCLE_TIMEOUT_SECONDS):
                        try:
                            fut.result()
                        except Exception as exc:
                            _logger.exception("unexpected error from search worker future")
                            _record_worker_error(
                                "search_worker_future",
                                "Unexpected search worker future failure",
                                exc,
                            )
                except FutureTimeoutError:
                    timed_out = True
                    _logger.warning(
                        "worker cycle timeout after %ds — cancelling unfinished futures",
                        _CYCLE_TIMEOUT_SECONDS,
                    )
                    _record_worker_error(
                        "search_cycle_timeout",
                        "Worker search cycle timed out",
                        RuntimeError(f"Search cycle exceeded {_CYCLE_TIMEOUT_SECONDS} seconds."),
                    )
                    for fut, (search_id, search_name) in futures.items():
                        if not fut.done():
                            fut.cancel()
                            _logger.debug("search '%s' still in flight at cycle boundary", search_name)
            finally:
                pool.shutdown(wait=not timed_out, cancel_futures=timed_out)

    _run_maintenance()


def _run_maintenance() -> None:
    global _cycle_count
    _cycle_count += 1
    cycle_started = datetime.now(UTC)
    record_heartbeat(owner=_WORKER_OWNER, cycle_count=_cycle_count, phase="process_deliveries", cycle_started_at=cycle_started)

    with worker_activity("process_deliveries") as active:
        if not active:
            dispose_engine()
            _logger.info("delivery processing skipped: maintenance is active")
            return
        try:
            delivery_result = process_deliveries_once()
            if delivery_result.eligible_deliveries or delivery_result.skipped_reason:
                _logger.info("%s", delivery_result.summary)
        except Exception as exc:
            _logger.exception("Telegram delivery processing failed")
            _record_worker_error(
                "process_deliveries",
                "Telegram delivery processing failed",
                exc,
            )

    # Error notifications are a separate durable outbox. Failures here are only logged and
    # stored on their originating event, never recorded as new error events.
    try:
        notification_result = get_state().errors.process_pending_notifications()
        if notification_result["claimed"]:
            _logger.info("Processed error notifications: %s", notification_result)
    except Exception:
        _logger.warning("error notification processing failed", exc_info=True)

    record_heartbeat(owner=_WORKER_OWNER, cycle_count=_cycle_count, phase="check_cookie_expiry")

    with worker_activity("check_cookie_expiry") as active:
        if not active:
            dispose_engine()
            _logger.info("cookie expiry check skipped: maintenance is active")
            return
        try:
            check_cookie_expiry()
        except Exception as exc:
            _logger.warning("cookie expiry check failed", exc_info=True)
            _record_worker_error("check_cookie_expiry", "Cookie expiry check failed", exc)

    state = get_state()
    prune_every = max(1, state.settings.prune_every_cycles)
    if _cycle_count % prune_every == 0:
        record_heartbeat(owner=_WORKER_OWNER, cycle_count=_cycle_count, phase="prune_records")
        with worker_activity("prune_records") as active:
            if not active:
                dispose_engine()
                _logger.info("record pruning skipped: maintenance is active")
                return
            try:
                prune_records_once()
            except Exception as exc:
                _logger.warning("record pruning failed", exc_info=True)
                _record_worker_error("prune_records", "Record pruning failed", exc)
            try:
                pruned_errors = get_state().errors.prune_old_events()
                if pruned_errors:
                    _logger.info("Pruned %d old error events", pruned_errors)
            except Exception as exc:
                _logger.warning("error-event pruning failed", exc_info=True)
                _record_worker_error("prune_error_events", "Error-event pruning failed", exc)

    cycle_finished = datetime.now(UTC)
    record_heartbeat(
        owner=_WORKER_OWNER,
        cycle_count=_cycle_count,
        phase="idle",
        cycle_finished_at=cycle_finished,
    )


def _enabled_searches() -> list[SearchRecord]:
    state = get_state()
    return [s for s in state.searches.all() if s.enabled]


def _active_scan_count() -> int:
    with _active_scan_lock:
        return len(_active_scan_threads)


def _next_search(enabled: list[SearchRecord]) -> SearchRecord:
    global _rotation_index
    idx = _rotation_index % len(enabled)
    _rotation_index += 1
    return enabled[idx]


def _launch_scan(search: SearchRecord, interval: int) -> None:
    """Runs one search's scan on its own thread, registering worker activity for the scan's
    full duration (not just the dispatch instant) so maintenance operations can see it's live."""

    def _target() -> None:
        try:
            with worker_activity("scan_searches") as active:
                if not active:
                    dispose_engine()
                    _logger.info("skipping '%s': maintenance is active", search.name)
                    return
                _run_one(search.id, search.name, interval)
        finally:
            with _active_scan_lock:
                _active_scan_threads.discard(threading.current_thread())

    thread = threading.Thread(target=_target, name=f"scan-{search.id}")
    with _active_scan_lock:
        _active_scan_threads.add(thread)
    thread.start()


def _resolve_interval(override: int | None) -> int:
    """Seconds between worker cycles: an explicit --interval always wins; otherwise this reads
    the scan interval configured on the Settings page each cycle, so edits there take effect
    without a worker restart."""
    if override is not None:
        return override
    try:
        return get_state().searches.get_scan_interval_seconds()
    except Exception as exc:
        _logger.warning("could not read configured scan interval; falling back to default", exc_info=True)
        _record_worker_error("resolve_scan_interval", "Could not read configured scan interval", exc)
        return get_settings().scan_interval_seconds


def _run_continuous_loop(interval_override: int | None) -> None:
    """Spreads search scans evenly across the (configurable) scan interval instead of firing
    them all at once, so N searches over an interval-second window each get their own turn
    roughly interval/N seconds apart — this reduces the chance of several concurrent requests
    tripping Vinted's abuse detection. Delivery/cookie/prune maintenance runs on its own fixed,
    short cadence (`_MAINTENANCE_TICK_SECONDS`), independent of the scan interval."""
    last_maintenance_at: float | None = None
    next_scan_dispatch_at: float | None = None

    while not _stop:
        interval = _resolve_interval(interval_override)
        now = time.monotonic()

        if last_maintenance_at is None or now - last_maintenance_at >= _MAINTENANCE_TICK_SECONDS:
            try:
                _run_maintenance()
            except Exception as exc:
                _logger.exception("maintenance pass failed; continuing")
                _record_worker_error("maintenance_pass", "Worker maintenance pass failed", exc)
            last_maintenance_at = time.monotonic()

        try:
            enabled = _enabled_searches()
        except Exception as exc:
            _logger.exception("could not list enabled searches")
            _record_worker_error("list_enabled_searches", "Worker could not list enabled searches", exc)
            enabled = []

        if not enabled:
            next_scan_dispatch_at = None
        else:
            if next_scan_dispatch_at is None:
                next_scan_dispatch_at = now
            max_workers = max(1, get_state().settings.worker_max_concurrency)
            now = time.monotonic()
            while now >= next_scan_dispatch_at and enabled:
                if _active_scan_count() >= max_workers:
                    break  # at capacity; retry this search next tick without advancing rotation
                search = _next_search(enabled)
                _launch_scan(search, interval)
                next_scan_dispatch_at += interval / len(enabled)
                now = time.monotonic()

        if _stop:
            break
        time.sleep(_POLL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="vsniper worker scheduler")
    parser.add_argument("--once", action="store_true", help="Run a single worker cycle and exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Seconds between worker cycles (overrides the scan interval configured in Settings)",
    )
    args = parser.parse_args()

    if args.once:
        cycle(_resolve_interval(args.interval))
        return

    def _handle_stop(signum: int, frame: object) -> None:
        global _stop
        _logger.info("received signal %d, will stop after current cycle", signum)
        _stop = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    _run_continuous_loop(args.interval)


if __name__ == "__main__":
    main()
