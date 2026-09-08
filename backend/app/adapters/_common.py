"""Common adapter utilities: retry/backoff with jitter, health tracking, cache.

These are intentionally dependency-free (no `tenacity`) so we can run with
the existing `pyproject.toml` lock and keep the surface easy to type-check.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Generic, TypeVar

from app.models.domain import SourceHealth, SourceHealthStatus

T = TypeVar("T")


class TransientError(Exception):
    """Raised by adapter calls to signal the operation can be retried."""


class PermanentError(Exception):
    """Raised by adapter calls to signal the operation must not be retried."""


@dataclass(slots=True)
class RetryPolicy:
    """Exponential backoff with full jitter."""

    max_attempts: int = 4
    initial_delay: float = 0.1
    multiplier: float = 2.0
    max_delay: float = 5.0
    jitter: float = 0.5  # fraction of the computed delay applied as +/- noise

    def compute_delay(self, attempt: int) -> float:
        """Return the delay before the (attempt+1)-th try (0-indexed)."""
        base = min(self.initial_delay * (self.multiplier**attempt), self.max_delay)
        noise = base * self.jitter * (2.0 * random.random() - 1.0)
        return max(0.0, base + noise)


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run `func` with exponential-backoff retries on `TransientError`."""
    pol = policy or RetryPolicy()
    last_exc: BaseException | None = None
    for attempt in range(pol.max_attempts):
        try:
            return await func()
        except PermanentError:
            raise
        except TransientError as exc:
            last_exc = exc
            if attempt == pol.max_attempts - 1:
                break
            await sleep(pol.compute_delay(attempt))
    assert last_exc is not None  # noqa: S101 - retry exhausted
    raise last_exc


@dataclass(slots=True)
class HealthTracker:
    """Per-source health record updated by adapters after each call."""

    source: str
    status: SourceHealthStatus = SourceHealthStatus.OK
    message: str | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    degraded_threshold: int = 1
    down_threshold: int = 3

    def record_success(self) -> None:
        self.status = SourceHealthStatus.OK
        self.message = None
        self.last_success_at = datetime.now(UTC)
        self.consecutive_failures = 0

    def record_failure(self, message: str) -> None:
        self.consecutive_failures += 1
        self.message = message
        if self.consecutive_failures >= self.down_threshold:
            self.status = SourceHealthStatus.DOWN
        elif self.consecutive_failures >= self.degraded_threshold:
            self.status = SourceHealthStatus.DEGRADED

    def snapshot(self) -> SourceHealth:
        return SourceHealth(
            source=self.source,
            status=self.status,
            message=self.message,
            last_success_at=self.last_success_at,
        )


K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


@dataclass(slots=True)
class SessionCache(Generic[K, V]):
    """Tiny per-key session cache keyed by (user_id, connection_id) or similar.

    Values are produced lazily by a factory passed to `get_or_create`. This
    keeps adapter instances around for the life of a request scope without
    leaking SDK clients between users.
    """

    _store: dict[K, V] = field(default_factory=dict)

    def get(self, key: K) -> V | None:
        return self._store.get(key)

    def set(self, key: K, value: V) -> None:
        self._store[key] = value

    def get_or_create(self, key: K, factory: Callable[[], V]) -> V:
        existing = self._store.get(key)
        if existing is not None:
            return existing
        created = factory()
        self._store[key] = created
        return created

    def pop(self, key: K) -> V | None:
        return self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()
