"""Opening balances (spec §6.4).

Found live: Ghostfolio derived HKD 4,590 for a Futu account really worth
HKD 21,703, because most of the position was accumulated before the
oldest trade the broker still reports. Every number built on the replayed
quantity was wrong in the same direction, including a +321% return
against a cost basis that was mostly missing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.domain import Position, Transaction, TransactionType
from app.services.ghostfolio.opening import (
    build_opening_transactions,
    compute_gaps,
    opening_start_date,
    retract_opening_balances,
)


def _pos(symbol: str, qty: str, cost: str | None = "100", currency: str = "USD") -> Position:
    return Position(
        source="futu", symbol=symbol, quantity=Decimal(qty),
        avg_cost=Decimal(cost) if cost is not None else None,
        currency=currency,
    )


def _tx(symbol: str, qty: str, kind: TransactionType, day: int = 1) -> Transaction:
    return Transaction(
        source="futu", transaction_id=f"{symbol}-{kind}-{day}-{qty}",
        symbol=symbol, quantity=Decimal(qty), type=kind,
        timestamp=datetime(2025, 1, day, tzinfo=UTC),
    )


def test_gap_is_held_minus_replayed() -> None:
    report = compute_gaps(
        positions=[_pos("VOO", "2.707", "526.627")],
        transactions=[
            _tx("VOO", "0.3", TransactionType.BUY),
            _tx("VOO", "0.1", TransactionType.BUY, day=2),
        ],
    )
    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert gap.derived == Decimal("0.4")
    assert gap.missing == Decimal("2.307")


def test_sells_reduce_the_replayed_quantity() -> None:
    report = compute_gaps(
        positions=[_pos("DRAM", "15")],
        transactions=[
            _tx("DRAM", "20", TransactionType.BUY),
            _tx("DRAM", "10", TransactionType.SELL, day=2),
        ],
    )
    assert report.gaps[0].derived == Decimal("10")
    assert report.gaps[0].missing == Decimal("5")


def test_dividends_and_fees_move_no_shares() -> None:
    report = compute_gaps(
        positions=[_pos("VOO", "1")],
        transactions=[
            _tx("VOO", "1", TransactionType.BUY),
            _tx("VOO", "1", TransactionType.DIVIDEND, day=2),
            _tx("VOO", "1", TransactionType.FEE, day=3),
        ],
    )
    assert report.gaps == []


def test_a_matching_position_produces_no_opening_row() -> None:
    report = compute_gaps(
        positions=[_pos("VOO", "5")],
        transactions=[_tx("VOO", "5", TransactionType.BUY)],
    )
    assert report.gaps == []
    assert report.transactions == []


def test_surplus_is_reported_never_booked_away() -> None:
    """More implied than held means a duplicate or a missed sale.

    Booking a negative opening balance would hide the real defect.
    """
    report = compute_gaps(
        positions=[_pos("VOO", "1")],
        transactions=[_tx("VOO", "5", TransactionType.BUY)],
    )
    assert report.gaps == []
    assert len(report.surplus) == 1
    assert report.surplus[0].missing == Decimal("-4")


def test_a_gap_with_no_cost_is_refused() -> None:
    # Right quantity with an invented cost basis corrupts every return
    # figure downstream; §7.1's no-guessing rule covers prices too.
    report = compute_gaps(
        positions=[_pos("VOO", "5", cost=None)],
        transactions=[_tx("VOO", "1", TransactionType.BUY)],
    )
    assert report.gaps == []
    assert len(report.no_cost) == 1


def test_opening_transaction_uses_the_brokers_own_cost() -> None:
    report = compute_gaps(
        positions=[_pos("VOO", "2.707", "526.627")],
        transactions=[_tx("VOO", "0.707", TransactionType.BUY)],
    )
    built = build_opening_transactions(
        report, source="futu", account_id="acct",
        as_of=datetime(2024, 8, 31, tzinfo=UTC),
    )
    assert len(built) == 1
    opening = built[0]
    assert opening.type is TransactionType.BUY
    assert opening.quantity == Decimal("2")
    assert opening.price == Decimal("526.627")
    # Stable per (source, symbol): a re-run without retraction is a no-op,
    # not a second position.
    assert opening.external_id == "futu:opening:VOO"


def test_opening_is_dated_before_the_known_window() -> None:
    # Dating it inside the window would look like a purchase we have a
    # record of, and would distort a period we can actually account for.
    txs = [
        _tx("VOO", "1", TransactionType.BUY, day=10),
        _tx("VOO", "1", TransactionType.BUY, day=20),
    ]
    assert opening_start_date(txs) == datetime(2025, 1, 9, tzinfo=UTC)


class FakeClient:
    def __init__(self, activities: list[dict]) -> None:
        self.activities = activities
        self.deleted: list[str] = []

    async def list_activities(self) -> list[dict]:
        return self.activities

    async def delete_activity(self, activity_id: str) -> None:
        self.deleted.append(activity_id)


class FakeLedger:
    def __init__(self) -> None:
        self.forgotten: list[str] = []

    def forget(self, ids) -> int:
        self.forgotten.extend(ids)
        return len(self.forgotten)


@pytest.mark.asyncio
async def test_retraction_removes_only_this_accounts_opening_rows() -> None:
    client = FakeClient([
        {"id": "1", "accountId": "a1", "comment": "futu:opening:VOO"},
        {"id": "2", "accountId": "a1", "comment": "futu:12345:deal-1"},
        {"id": "3", "accountId": "OTHER", "comment": "ibkr:opening:VOO"},
    ])
    ledger = FakeLedger()
    removed = await retract_opening_balances(
        client=client, account_id="a1", ledger=ledger
    )
    assert removed == 1
    assert client.deleted == ["1"]
    # The ledger must forget it too, or the recomputed row is treated as
    # already pushed and silently dropped (§3.3).
    assert ledger.forgotten == ["futu:opening:VOO"]
