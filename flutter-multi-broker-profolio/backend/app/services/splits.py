"""Share splits: our records and the price provider disagree (§6.4, §7.1).

The problem
-----------
A split restates the price provider's history but not the broker's
records. SQQQ reverse-split 1-for-25 after September 2024, so Yahoo now
reports $204.25 for 2024-09-13 — a day on which Futu actually charged
$8.18 a share. Our activity keeps the $8.18 and the share count that went
with it.

Ghostfolio marks a position at *its* price times *our* quantity, so for
as long as that position is held it is valued 25x too high. Found live:
the portfolio chart spiked to +244% between 2024-09-02 and 2024-10-09 and
snapped back the moment SQQQ was sold. TQQQ had the same problem in the
other direction (2-for-1 forward split, ratio 0.511, valued at half).

Why a closed position is the easy case
--------------------------------------
Restating it is exact: ``quantity / factor`` and ``price * factor`` leave
``quantity * price`` — the money — untouched, so cost, proceeds and
realised P&L do not move at all. Only the mark-to-market in between is
corrected.

A position still held is the dangerous one. Ghostfolio derives the
holding by replaying activities, so a pre-split quantity replayed against
a post-split world comes out short — and `opening.compute_gaps` would
read that as missing history and book an opening balance to cover it,
inventing a cost basis for shares that were never bought. That is why
this module is used there too: a gap that is a clean split multiple is
reported, never booked.

Why the factor is snapped rather than trusted
---------------------------------------------
The ratio is measured against a daily close, so a volatile day moves it a
few percent either way. Real splits are simple rationals. Snapping to a
short list of them and refusing anything that does not land close is what
separates a split from an ordinary bad print — measured across the live
portfolio, healthy symbols sat within 13% of 1.0 while the two split
names sat within 2% of 25 and 1/2.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

def _with_reciprocals(ratios: tuple[Fraction, ...]) -> tuple[Fraction, ...]:
    """Every split runs both ways: 2-for-1 forward, 1-for-2 reverse."""
    return tuple(sorted(set(ratios) | {1 / r for r in ratios}))


#: Ratios a real split can produce. Deliberately short: every entry added
#: here is one more shape that ordinary price noise can be mistaken for.
#: 4/3 and 5/4 are left out for exactly that reason — they sit close
#: enough to 1.0 that a volatile session reaches them.
SPLIT_CANDIDATES: tuple[Fraction, ...] = _with_reciprocals(
    tuple(
        Fraction(n, 1)
        for n in (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100)
    )
    + (Fraction(3, 2), Fraction(5, 2))
)

#: How far a measured *price* ratio may sit from a candidate and still
#: count. 6% clears a volatile session without reaching the nearest
#: candidate: the live scan's worst healthy symbol was 13% from 1.0, and
#: still 9% away from the closest split ratio.
DEFAULT_TOLERANCE = Decimal("0.06")

#: For a *quantity* ratio. A split multiplies a share count exactly —
#: 2.707 shares become 5.414, never 5.4 — so anything but an exact
#: multiple is something else. This is far tighter than the price
#: tolerance because a quantity ratio is a much weaker signal on its own:
#: a portfolio of whole shares that is missing half its history reads as
#: a clean 2x, and refusing to book that real gap is the very bug the
#: opening-balance pass exists to fix. Price evidence decides; this only
#: names the factor.
EXACT_MULTIPLE_TOLERANCE = Decimal("0.001")


def snap_split_factor(
    ratio: Decimal, *, tolerance: Decimal = DEFAULT_TOLERANCE
) -> Fraction | None:
    """The split this price ratio implies, or None if it implies none.

    ``ratio`` is the provider's price for the trade date divided by the
    price we recorded. A ratio near 1 means the two agree and returns
    None — as does anything that lands near no candidate at all, which is
    a bad print or a wrong symbol and must not be silently "corrected".
    """
    if ratio <= 0:
        return None
    best: Fraction | None = None
    best_error: Decimal | None = None
    for candidate in SPLIT_CANDIDATES:
        value = Decimal(candidate.numerator) / Decimal(candidate.denominator)
        error = abs(ratio - value) / value
        if error > tolerance:
            continue
        if best_error is None or error < best_error:
            best, best_error = candidate, error
    return best


def restate(
    quantity: Decimal, price: Decimal, factor: Fraction
) -> tuple[Decimal, Decimal]:
    """Re-express a trade on the provider's post-split basis.

    The provider says the share was worth ``factor`` times what we paid,
    so the same money bought ``1 / factor`` as many of its shares. The
    product is preserved exactly, which is the whole point: this changes
    how the position is *valued* over time and changes nothing about what
    it cost.
    """
    numerator = Decimal(factor.numerator)
    denominator = Decimal(factor.denominator)
    return quantity * denominator / numerator, price * numerator / denominator




@dataclass(slots=True)
class BasisReport:
    """Where our raw broker data and Ghostfolio's stored rows disagree."""

    #: symbol -> factor that turns OUR basis into the STORED one.
    factors: dict[str, Fraction] = field(default_factory=dict)
    #: symbol -> the differing ratios, when one factor cannot explain them.
    conflicts: dict[str, list[str]] = field(default_factory=dict)

    def describe(self) -> list[str]:
        out = [f"{sym}: stored on a {f} basis" for sym, f in sorted(self.factors.items())]
        out += [
            f"{sym}: stored rows disagree among themselves ({', '.join(r)})"
            for sym, r in sorted(self.conflicts.items())
        ]
        return out


def detect_stored_basis(
    pairs: Iterable[tuple[str, Decimal, Decimal]],
    *,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> BasisReport:
    """Compare the same trades as we read them and as Ghostfolio holds them.

    Why this is needed at all: once a symbol's activities have been
    restated onto the provider's post-split basis, the broker keeps
    reporting the old one forever. Every subsequent sync then derives an
    opening balance from raw broker numbers and pushes it beside
    activities that live on a different scale.

    That is not theoretical. SQQQ's trades sat restated at ~$204 while a
    freshly derived opening balance went in at $7.83 for 8 shares — and
    a position that had been fully sold replayed to 7.68 shares worth
    2,299 HKD that the owner does not hold.

    Each pair is ``(symbol, our_price, stored_price)`` for ONE trade
    present on both sides, so the ratio is measured on identical rows —
    no date alignment, no market movement, no guessing. A symbol whose
    rows do not all agree on one factor is reported, never repaired:
    that means the stored history itself straddles a split.
    """
    ratios: dict[str, list[Decimal]] = {}
    for symbol, ours, stored in pairs:
        if ours is None or stored is None or ours <= 0 or stored <= 0:
            continue
        ratios.setdefault(symbol, []).append(Decimal(stored) / Decimal(ours))

    report = BasisReport()
    for symbol, values in ratios.items():
        snapped = {snap_split_factor(v, tolerance=tolerance) for v in values}
        if snapped == {None}:
            continue  # same basis on both sides, which is the normal case
        if len(snapped) != 1:
            report.conflicts[symbol] = sorted(f"{v:.4f}" for v in values)
            continue
        factor = snapped.pop()
        if factor is not None:
            report.factors[symbol] = factor
    return report


def _with(record: Any, **changes: Any) -> Any:
    """Copy a record with fields changed, whatever it is built from.

    The domain models are pydantic, so `dataclasses.replace` does not
    apply to them; keeping both paths means this helper stays usable if
    a source ever hands us a plain dataclass.
    """
    if hasattr(record, "model_copy"):
        return record.model_copy(update=changes)
    from dataclasses import replace

    return replace(record, **changes)


def align_to_stored_basis(
    transactions: Sequence[Any],
    positions: Sequence[Any],
    factors: Mapping[str, Fraction],
) -> tuple[list[Any], list[Any]]:
    """Re-express broker data on the basis Ghostfolio already stores.

    Applied before reconciliation, never before the trade push: the push
    is idempotent on ids the broker assigns, and those do not change.
    What must change is the arithmetic the gap is computed from, so that
    a derived opening balance lands on the same scale as the activities
    it is meant to complete.
    """
    if not factors:
        return list(transactions), list(positions)

    out_tx: list[Any] = []
    for tx in transactions:
        factor = factors.get((tx.symbol or "").upper()) or factors.get(tx.symbol or "")
        if factor is None or tx.quantity is None or tx.price is None:
            out_tx.append(tx)
            continue
        quantity, price = restate(tx.quantity, tx.price, factor)
        out_tx.append(_with(tx, quantity=quantity, price=price))

    out_pos: list[Any] = []
    for position in positions:
        factor = (
            factors.get((position.symbol or "").upper())
            or factors.get(position.symbol or "")
        )
        if factor is None:
            out_pos.append(position)
            continue
        quantity, cost = restate(
            position.quantity, position.avg_cost or Decimal("0"), factor
        )
        out_pos.append(
            _with(
                position,
                quantity=quantity,
                avg_cost=cost if position.avg_cost is not None else None,
            )
        )
    return out_tx, out_pos


__all__ = [
    "DEFAULT_TOLERANCE",
    "BasisReport",
    "align_to_stored_basis",
    "detect_stored_basis",
    "EXACT_MULTIPLE_TOLERANCE",
    "SPLIT_CANDIDATES",
    "restate",
    "snap_split_factor",
]
