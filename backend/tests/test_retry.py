from __future__ import annotations

import pytest

import vsniper.integrations._retry as retry_mod
from vsniper.integrations._retry import retry_transient


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(retry_mod.time, "sleep", lambda _seconds: None)


def _retryable_error(message: str) -> RuntimeError:
    exc = RuntimeError(message)
    exc.retryable = True  # type: ignore[attr-defined]
    return exc


def test_exhausted_retry_clears_retryable_flag() -> None:
    # Once the inner retries are spent the error must be marked terminal, so an outer
    # retry_transient does not re-amplify the attempt count (the nested-retry blowup).
    def always_fails() -> None:
        raise _retryable_error("transient")

    with pytest.raises(RuntimeError) as excinfo:
        retry_transient(always_fails, label="inner")

    assert getattr(excinfo.value, "retryable", False) is False


def test_nested_retry_does_not_amplify_attempts() -> None:
    calls = 0

    def always_fails() -> None:
        nonlocal calls
        calls += 1
        raise _retryable_error("transient")

    def inner() -> None:
        retry_transient(always_fails, label="inner")

    with pytest.raises(RuntimeError):
        retry_transient(inner, label="outer")

    # Inner exhausts its 3 attempts and clears the flag, so the outer sees a terminal error
    # and does not retry — 3 total calls, not 9.
    assert calls == 3


def test_non_retryable_error_is_not_retried() -> None:
    calls = 0

    def fails() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("permanent")

    with pytest.raises(RuntimeError):
        retry_transient(fails, label="label")

    assert calls == 1


def test_retryable_error_eventually_succeeds() -> None:
    calls = 0

    def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise _retryable_error("transient")
        return "ok"

    assert retry_transient(flaky, label="label") == "ok"
    assert calls == 2
