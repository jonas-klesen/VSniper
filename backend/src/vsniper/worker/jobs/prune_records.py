from __future__ import annotations

from vsniper.core.state import get_state


def run_once() -> dict[str, int]:
    return get_state().candidates.prune_old_records()
