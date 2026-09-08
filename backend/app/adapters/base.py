"""Source adapter Protocol shared by all broker / exchange adapters.

Mirrors detailed-design §4.3. Concrete adapters live under
`app/adapters/<source>/` and inject a thin SDK wrapper so the real network
client is replaceable in tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Protocol, runtime_checkable

from app.models.domain import (
    CashBalance,
    Position,
    Quote,
    SourceHealth,
    Transaction,
)


@runtime_checkable
class SourceAdapter(Protocol):
    """Common contract every broker / exchange adapter must satisfy."""

    source: str

    async def list_positions(self) -> list[Position]: ...

    async def list_balances(self) -> list[CashBalance]: ...

    async def list_transactions(
        self,
        *,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[Transaction]: ...

    def stream_quotes(self, symbols: Iterable[str]) -> AsyncIterator[Quote]: ...

    async def healthcheck(self) -> SourceHealth: ...
