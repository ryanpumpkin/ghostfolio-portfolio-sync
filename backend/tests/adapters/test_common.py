"""Tests for retry, health tracking, and session cache utilities."""

from __future__ import annotations

import pytest

from app.adapters._common import (
    HealthTracker,
    PermanentError,
    RetryPolicy,
    SessionCache,
    TransientError,
    retry_async,
)
from app.models.domain import SourceHealthStatus


def test_retry_policy_delay_bounded() -> None:
    pol = RetryPolicy(initial_delay=0.1, multiplier=2.0, max_delay=1.0, jitter=0.0)
    assert pol.compute_delay(0) == pytest.approx(0.1)
    assert pol.compute_delay(1) == pytest.approx(0.2)
    # Capped at max_delay.
    assert pol.compute_delay(10) == pytest.approx(1.0)


def test_retry_policy_jitter_non_negative() -> None:
    pol = RetryPolicy(initial_delay=0.1, multiplier=1.0, max_delay=1.0, jitter=2.0)
    for _ in range(20):
        assert pol.compute_delay(0) >= 0.0


@pytest.mark.asyncio
async def test_retry_async_succeeds_after_transient() -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("nope")
        return "ok"

    async def no_sleep(_d: float) -> None:
        return None

    result = await retry_async(
        flaky, policy=RetryPolicy(max_attempts=5, jitter=0.0), sleep=no_sleep
    )
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_async_gives_up_after_max_attempts() -> None:
    async def always_fail() -> None:
        raise TransientError("boom")

    async def no_sleep(_d: float) -> None:
        return None

    with pytest.raises(TransientError):
        await retry_async(
            always_fail,
            policy=RetryPolicy(max_attempts=3, jitter=0.0),
            sleep=no_sleep,
        )


@pytest.mark.asyncio
async def test_retry_async_permanent_propagates_immediately() -> None:
    calls = {"n": 0}

    async def boom() -> None:
        calls["n"] += 1
        raise PermanentError("no")

    with pytest.raises(PermanentError):
        await retry_async(boom, policy=RetryPolicy(max_attempts=5))
    assert calls["n"] == 1


def test_health_tracker_transitions() -> None:
    tracker = HealthTracker(source="x", degraded_threshold=1, down_threshold=3)
    assert tracker.snapshot().status is SourceHealthStatus.OK

    tracker.record_failure("network")
    assert tracker.snapshot().status is SourceHealthStatus.DEGRADED
    assert tracker.snapshot().message == "network"

    tracker.record_failure("network")
    tracker.record_failure("network")
    assert tracker.snapshot().status is SourceHealthStatus.DOWN

    tracker.record_success()
    snap = tracker.snapshot()
    assert snap.status is SourceHealthStatus.OK
    assert snap.last_success_at is not None
    assert snap.message is None


def test_session_cache_get_or_create() -> None:
    cache: SessionCache[str, int] = SessionCache()
    calls = {"n": 0}

    def make() -> int:
        calls["n"] += 1
        return 42

    assert cache.get_or_create("k", make) == 42
    assert cache.get_or_create("k", make) == 42
    assert calls["n"] == 1
    assert cache.get("k") == 42

    cache.set("other", 7)
    assert cache.pop("other") == 7
    assert cache.pop("missing") is None

    cache.clear()
    assert cache.get("k") is None
