"""Tests for quote hub subscription fan-out and reference counts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.adapters.base import SourceAdapter
from app.models.domain import (
    CashBalance,
    Position,
    Quote,
    SourceHealth,
    SourceHealthStatus,
    Transaction,
)
from app.services.dependencies import StaticAdapterRegistry
from app.services.quote_hub import QuoteHub


class _QueueQuoteAdapter(SourceAdapter):
    source = "longbridge"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def list_positions(self) -> list[Position]:
        return []

    async def list_balances(self) -> list[CashBalance]:
        return []

    async def list_transactions(
        self, *, since: str | None = None, limit: int | None = None
    ) -> list[Transaction]:
        _ = (since, limit)
        return []

    async def stream_quotes(self, symbols: Iterable[str]) -> AsyncIterator[Quote]:
        self.calls.append(list(symbols))
        quote = Quote(
            source="longbridge",
            symbol="AAPL",
            price=Decimal("100"),
            currency="USD",
            timestamp=datetime.now(UTC),
        )
        yield quote
        await asyncio.sleep(3600)

    async def healthcheck(self) -> SourceHealth:
        return SourceHealth(source="longbridge", status=SourceHealthStatus.OK)


@pytest.mark.asyncio
async def test_quote_hub_reference_count_tracks_subscribers() -> None:
    adapter = _QueueQuoteAdapter()
    hub = QuoteHub(
        StaticAdapterRegistry({"longbridge": adapter}),
        heartbeat_interval=3600,
        reconnect_delay=0.01,
    )

    q1 = await hub.register_client("c1")
    q2 = await hub.register_client("c2")
    assert q1 is not q2

    await hub.subscribe("c1", source="longbridge", symbols=["AAPL"])
    await hub.subscribe("c2", source="longbridge", symbols=["AAPL"])

    assert hub.ref_count("longbridge", "AAPL") == 2

    # Ensure at least one quote fan-out happens.
    msg = await asyncio.wait_for(q1.get(), timeout=1.0)
    assert msg["type"] == "quote"

    await hub.unsubscribe("c1", source="longbridge", symbols=["AAPL"])
    assert hub.ref_count("longbridge", "AAPL") == 1

    await hub.unsubscribe("c2", source="longbridge", symbols=["AAPL"])
    assert hub.ref_count("longbridge", "AAPL") == 0

    await hub.unregister_client("c1")
    await hub.unregister_client("c2")
    await hub.aclose()
