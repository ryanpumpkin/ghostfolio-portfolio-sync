"""Tests for the portfolio aggregation service."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.adapters.base import SourceAdapter
from app.models.domain import (
    CashBalance,
    Connection,
    FxRate,
    Position,
    SourceHealth,
    SourceHealthStatus,
    Transaction,
)
from app.services.aggregator import InMemoryConnectionRepository, PortfolioAggregator
from app.services.connection_status import (
    CapturingConnectionStatusWriter,
    InMemoryConnectionStatusPublisher,
    listener_from_writer,
)
from app.services.dependencies import StaticAdapterRegistry


class _GoodAdapter(SourceAdapter):
    source = "good"

    async def list_positions(self) -> list[Position]:
        return [
            Position(
                source="good",
                symbol="AAPL",
                quantity=Decimal("10"),
                avg_cost=Decimal("100"),
                last_price=Decimal("120"),
                currency="USD",
                market_value=Decimal("1200"),
                unrealized_pnl=Decimal("200"),
            )
        ]

    async def list_balances(self) -> list[CashBalance]:
        return [CashBalance(source="good", currency="USD", amount=Decimal("300"))]

    async def list_transactions(
        self, *, since: str | None = None, limit: int | None = None
    ) -> list[Transaction]:
        _ = (since, limit)
        return []

    def stream_quotes(self, symbols: Iterable[str]) -> AsyncIterator:  # pragma: no cover - unused
        _ = symbols

        async def _empty() -> AsyncIterator:
            if False:
                yield

        return _empty()

    async def healthcheck(self) -> SourceHealth:
        return SourceHealth(source="good", status=SourceHealthStatus.OK)


class _FailingAdapter(SourceAdapter):
    source = "bad"

    async def list_positions(self) -> list[Position]:
        raise RuntimeError("positions unavailable")

    async def list_balances(self) -> list[CashBalance]:
        raise RuntimeError("balances unavailable")

    async def list_transactions(
        self, *, since: str | None = None, limit: int | None = None
    ) -> list[Transaction]:
        _ = (since, limit)
        raise RuntimeError("transactions unavailable")

    def stream_quotes(self, symbols: Iterable[str]) -> AsyncIterator:  # pragma: no cover - unused
        _ = symbols

        async def _empty() -> AsyncIterator:
            if False:
                yield

        return _empty()

    async def healthcheck(self) -> SourceHealth:
        return SourceHealth(source="bad", status=SourceHealthStatus.DOWN, message="down")


class _StaticFx:
    async def get_rates_for(self, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], FxRate]:
        out: dict[tuple[str, str], FxRate] = {}
        for base, quote in pairs:
            out[(base, quote)] = FxRate(
                base=base,
                quote=quote,
                rate=Decimal("1"),
                as_of=datetime.now(UTC),
            )
        return out


@pytest.mark.asyncio
async def test_snapshot_surfaces_partial_failure_without_crashing() -> None:
    repository = InMemoryConnectionRepository(
        [
            Connection(source="good", connection_id="c-good", display_name="good"),
            Connection(source="bad", connection_id="c-bad", display_name="bad"),
        ]
    )
    registry = StaticAdapterRegistry({"good": _GoodAdapter(), "bad": _FailingAdapter()})
    aggregator = PortfolioAggregator(connections=repository, adapters=registry, fx=_StaticFx())

    snapshot = await aggregator.get_snapshot("user-1", base_currency="USD")

    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].symbol == "AAPL"
    assert snapshot.total_market_value == Decimal("1500")

    health = {item.source: item for item in snapshot.source_health}
    assert health["good"].status is SourceHealthStatus.OK
    assert health["bad"].status is SourceHealthStatus.DOWN
    assert health["bad"].message is not None


@pytest.mark.asyncio
async def test_positions_publish_connection_status_events() -> None:
    repository = InMemoryConnectionRepository(
        [
            Connection(source="good", connection_id="c-good", display_name="good"),
            Connection(source="bad", connection_id="c-bad", display_name="bad"),
        ]
    )
    registry = StaticAdapterRegistry({"good": _GoodAdapter(), "bad": _FailingAdapter()})
    writer = CapturingConnectionStatusWriter()
    publisher = InMemoryConnectionStatusPublisher([listener_from_writer(writer)])
    aggregator = PortfolioAggregator(
        connections=repository,
        adapters=registry,
        fx=_StaticFx(),
        status_publisher=publisher,
    )

    result = await aggregator.get_positions("user-1")

    assert len(result.items) == 1
    writes = {(uid, event.connection_id): event for uid, event in writer.writes}
    ok = writes[("user-1", "c-good")]
    err = writes[("user-1", "c-bad")]
    assert ok.status.value == "ok"
    assert ok.error_message is None
    assert err.status.value == "error"
    assert err.error_message is not None and "positions unavailable" in err.error_message
