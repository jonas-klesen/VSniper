from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

_T = TypeVar("_T")
_logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BASE_DELAY = 2.0
_MAX_DELAY = 30.0


def retry_transient(fn: Callable[[], _T], *, label: str) -> _T:
    """Call fn up to _MAX_ATTEMPTS times, retrying when the raised exception has .retryable=True."""
    last_exc: BaseException
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not getattr(exc, "retryable", False) or attempt >= _MAX_ATTEMPTS - 1:
                # Retries are spent (or never applied): clear the flag so an *outer*
                # retry_transient treats this as terminal instead of re-amplifying attempts.
                if getattr(exc, "retryable", False):
                    exc.retryable = False  # type: ignore[attr-defined]
                raise
            delay = min(_BASE_DELAY * (2**attempt) + random.uniform(0, 1), _MAX_DELAY)
            _logger.warning(
                "%s transient error (attempt %d/%d), retrying in %.1fs: %s",
                label,
                attempt + 1,
                _MAX_ATTEMPTS,
                delay,
                exc,
            )
            time.sleep(delay)
    raise last_exc  # unreachable, but satisfies type checker
