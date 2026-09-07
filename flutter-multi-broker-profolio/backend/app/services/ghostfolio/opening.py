"""Opening balances: close the gap between held and derivable (spec §6.4).

The problem this solves
-----------------------
Ghostfolio derives what you hold by replaying activities. We can only
push the activities a broker still reports, and brokers do not keep
history forever:

* IBKR Flex serves at most a 365-day window per request.
* Futu's deal history ran out at 2024-09.
* LongBridge returned five years, which happened to be everything.

Anything bought before the earliest available trade is invisible, so the
replayed quantity is smaller than the real one — and every number built
on it is wrong in the same direction. Observed live: Futu's real holding
was HKD 21,703 and Ghostfolio derived HKD 4,590, because most of a
2.707-share VOO position was accumulated before the window. The
performance figure was worse than useless: +321% against a cost basis
that was mostly missing.

The fix
-------
The adapter's position list is authoritative — it is what the broker says
you hold right now. Compare it against the quantity the pushed activities
imply, and book the difference as a single BUY dated just before the
earliest known activity, priced at the broker's own average cost.

This is a *derived* row, and it is labelled as one. It is not a guess:
both the quantity and the unit cost come from the broker. What it cannot
recover is when those shares were bought, which is why the date is
explicitly the start of the known window rather than a fabricated
purchase date — the position is right, the cost basis is right, and only
the shape of the return curve before that date is unknown.

Re-running
----------
An opening balance is derived, so it goes stale the moment more history
becomes available. Each row carries a recognisable comment, and
`retract_opening_balances` removes them so the next pass recomputes from
scratch. Leaving a stale row and adding a corrected one would double the
position.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.models.domain import Position, Transaction, TransactionType

_LOG = logging.getLogger("mbp.ghostfolio.opening")

#: Marks a row as derived rather than reported. Also how the retraction
#: pass finds them again.
OPENING_COMMENT_PREFIX = "opening-balance"


@dataclass(slots=True)
class OpeningGap:
    """One instrument's shortfall between held and derivable quantity."""

    symbol: str
    held: Decimal
    derived: Decimal
    currency: str
    avg_cost: Decimal | None
    exchange: str | None = None

    @property
    def missing(self) -> Decimal:
        return self.held - self.derived

    def describe(self) -> str:
        return (
            f"{self.symbol}: held {self.held}, activities imply {self.derived}, "
            f"missing {self.missing}"
        )


@dataclass(slots=True)
class OpeningReport:
    """What the opening pass found and what it could act on."""

    gaps: list[OpeningGap] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    #: Positions held in excess of what is reported — never fabricated
    #: away, because a negative gap means something else is wrong.
    surplus: list[OpeningGap] = field(default_factory=list)
    #: Gaps we refuse to book because the broker gave us no cost.
    no_cost: list[OpeningGap] = field(default_factory=list)


def _currency_for(transactions: list[Transaction], symbol: str) -> str:
    for tx in transactions:
        if tx.symbol and tx.symbol.upper() == symbol.upper() and tx.currency:
            return tx.currency
    return "USD"


def _quantity_from_activities(transactions: list[Transaction]) -> dict[str, Decimal]:
    """Replay pushed activities the way Ghostfolio does."""
    totals: dict[str, Decimal] = {}
    for tx in transactions:
        if not tx.symbol or tx.quantity is None:
            continue
        if tx.type is TransactionType.BUY:
            sign = Decimal("1")
        elif tx.type is TransactionType.SELL:
            sign = Decimal("-1")
        else:
            # DIVIDEND, FEE, TRANSFER and friends move no shares.
            continue
        key = tx.symbol.upper()
        totals[key] = totals.get(key, Decimal("0")) + sign * abs(tx.quantity)
    return totals


def _earliest_sell_price(
    transactions: list[Transaction], symbol: str
) -> Decimal | None:
    """The price of the oldest disposal of a symbol we have on record."""
    sells = [
        tx for tx in transactions
        if tx.symbol and tx.symbol.upper() == symbol.upper()
        and tx.type is TransactionType.SELL and tx.price is not None
    ]
    if not sells:
        return None
    return min(sells, key=lambda tx: tx.timestamp).price


def compute_gaps(
    *,
    positions: list[Position],
    transactions: list[Transaction],
) -> OpeningReport:
    """Diff authoritative holdings against what the activities imply.

    Symbols are compared as the *source* reports them on both sides, so
    this runs before any Ghostfolio symbol mapping — a mapping difference
    would otherwise look like a missing position.

    Two kinds of gap, and the second one is easy to miss: a position that
    existed before the window and was *entirely sold inside it* never
    appears in the position list at all. Replaying only the sales leaves a
    negative quantity — a short position that was never held — which
    subtracts real value from the portfolio total. Live: three of them,
    together -11,893 HKD against an 82,796 HKD total.
    """
    derived = _quantity_from_activities(transactions)
    report = OpeningReport()
    held_symbols = {p.symbol.upper() for p in positions}

    for symbol, implied in sorted(derived.items()):
        if symbol in held_symbols or implied >= 0:
            continue
        # Sold more than we bought, and nothing is held today: the shares
        # predate the window. A negative holding is not merely missing,
        # it is arithmetically impossible, so this is booked even though
        # the broker can no longer tell us what the shares cost.
        cost = _earliest_sell_price(transactions, symbol)
        gap = OpeningGap(
            symbol=symbol,
            held=Decimal("0"),
            derived=implied,
            currency=_currency_for(transactions, symbol),
            avg_cost=cost,
        )
        if cost is None:
            _LOG.error(
                "%s: %s share(s) sold with no purchase on record and no sale "
                "price to value them at. Left as a negative holding rather "
                "than invented.", symbol, -implied,
            )
            report.no_cost.append(gap)
            continue
        # Valuing them at the first sale price makes the realised result on
        # the unexplained portion zero. That is the honest answer to "what
        # did these cost?" — unknown — rather than a fabricated gain.
        _LOG.info(
            "%s: %s share(s) sold without a purchase; booking at the first "
            "sale price %s so the unknown portion realises nothing",
            symbol, -implied, cost,
        )
        report.gaps.append(gap)

    for position in positions:
        symbol = position.symbol.upper()
        held = position.quantity
        implied = derived.get(symbol, Decimal("0"))
        gap = OpeningGap(
            symbol=position.symbol,
            held=held,
            derived=implied,
            currency=position.currency,
            avg_cost=position.avg_cost,
            exchange=position.exchange,
        )
        if gap.missing == 0:
            continue
        if gap.missing < 0:
            # More shares implied than held. That is not a history gap —
            # it is a duplicate, a missed sale, or a bad symbol match, and
            # booking a negative opening balance would paper over it.
            _LOG.error(
                "%s: activities imply MORE than is held (%s vs %s). Not "
                "booking an opening balance; investigate the duplicate or "
                "the missing disposal.",
                gap.symbol, implied, held,
            )
            report.surplus.append(gap)
            continue
        if gap.avg_cost is None or gap.avg_cost <= 0:
            # Without a cost the position could be added at the right
            # quantity and a wrong basis, which corrupts every return
            # figure that follows. §7.1's rule against guessing applies to
            # prices as much as to symbols.
            _LOG.error(
                "%s: %s share(s) missing but the broker reported no average "
                "cost. Not booking an opening balance — the quantity would "
                "be right and the cost basis invented.",
                gap.symbol, gap.missing,
            )
            report.no_cost.append(gap)
            continue
        report.gaps.append(gap)

    return report


def build_opening_transactions(
    report: OpeningReport,
    *,
    source: str,
    account_id: str | None,
    as_of: datetime,
) -> list[Transaction]:
    """Turn each gap into one dated BUY at the broker's average cost."""
    built: list[Transaction] = []
    for gap in report.gaps:
        built.append(
            Transaction(
                source=source,
                account_id=account_id,
                # Stable per (source, symbol) so a re-run without a
                # retraction is a no-op rather than a second position.
                transaction_id=f"opening:{gap.symbol.upper()}",
                external_id=f"{source}:opening:{gap.symbol.upper()}",
                symbol=gap.symbol,
                exchange=gap.exchange,
                side="buy",
                type=TransactionType.BUY,
                quantity=gap.missing,
                price=gap.avg_cost,
                currency=gap.currency,
                amount=gap.missing * (gap.avg_cost or Decimal("0")),
                timestamp=as_of,
            )
        )
    report.transactions = built
    return built


def opening_start_date(
    transactions: list[Transaction], *, fallback: datetime | None = None
) -> datetime:
    """The day before the earliest known activity.

    Dating the opening balance *inside* the known window would make it
    look like a purchase we have a record of, and would distort the
    return curve over a period we can actually account for.
    """
    dated = [tx.timestamp for tx in transactions if tx.timestamp]
    if dated:
        return min(dated) - timedelta(days=1)
    return fallback or datetime.now(UTC) - timedelta(days=1)


async def retract_opening_balances(
    *,
    client: Any,
    account_id: str,
    ledger: Any | None = None,
) -> int:
    """Delete previously booked opening balances for one account.

    Called before recomputing. An opening balance is derived from the
    history available at the time; once more history arrives the old row
    is simply wrong, and adding a corrected one beside it would double
    the position.
    """
    removed = 0
    forgotten: list[str] = []
    for activity in await client.list_activities():
        if str(activity.get("accountId") or "") != account_id:
            continue
        comment = str(activity.get("comment") or "")
        if OPENING_COMMENT_PREFIX not in comment and ":opening:" not in comment:
            continue
        await client.delete_activity(str(activity.get("id")))
        removed += 1
        if comment:
            forgotten.append(comment)

    if ledger is not None and forgotten:
        # The ledger must forget them too, or the recomputed rows are
        # treated as already pushed and silently dropped (§3.3).
        ledger.forget(forgotten)
    if removed:
        _LOG.info("retracted %d opening balance(s) for account %s", removed, account_id)
    return removed


__all__ = [
    "OPENING_COMMENT_PREFIX",
    "OpeningGap",
    "OpeningReport",
    "build_opening_transactions",
    "compute_gaps",
    "opening_start_date",
    "retract_opening_balances",
]
