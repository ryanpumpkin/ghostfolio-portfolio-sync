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

from decimal import Decimal
from fractions import Fraction

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


__all__ = [
    "DEFAULT_TOLERANCE",
    "EXACT_MULTIPLE_TOLERANCE",
    "SPLIT_CANDIDATES",
    "restate",
    "snap_split_factor",
]
