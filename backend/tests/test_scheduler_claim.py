"""The worker must not reclaim a legitimate in-flight scan as "stale" too early."""
from __future__ import annotations

from datetime import timedelta

from vsniper.worker import scheduler

_HEARTBEAT_INTERVAL_SECONDS = 30


def test_worker_stale_cutoff_is_at_least_three_heartbeats() -> None:
    # _run() refreshes last_claimed_at every _HEARTBEAT_INTERVAL_SECONDS while active.
    # stale_after must exceed at least 3× that interval so transient DB hiccups don't
    # trigger a false reclaim of a legitimately in-flight scan.
    assert scheduler._WORKER_CLAIM_STALE_AFTER >= timedelta(seconds=3 * _HEARTBEAT_INTERVAL_SECONDS)


def test_worker_passes_explicit_stale_after_to_claim(monkeypatch) -> None:
    captured: dict = {}

    class FakeSearches:
        def claim_for_run(self, search_id, min_interval, *, stale_after=None):
            captured["min_interval"] = min_interval
            captured["stale_after"] = stale_after
            return False  # short-circuit: skip the scan

    monkeypatch.setattr(scheduler, "get_state", lambda: type("S", (), {"searches": FakeSearches()})())
    monkeypatch.setattr(scheduler.time, "sleep", lambda *_: None)

    scheduler._run_one("s1", "Search", interval=60)

    assert captured["min_interval"] == timedelta(seconds=60)
    assert captured["stale_after"] == scheduler._WORKER_CLAIM_STALE_AFTER
