from __future__ import annotations

from vsniper.core.state import get_state


def run_once() -> None:
    get_state().telegram.check_refresh_token_expiry()
