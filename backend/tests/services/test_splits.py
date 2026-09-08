"""Split detection: snapping a measured ratio to a real split (§6.4).

The numbers here are the live ones. SQQQ reverse-split 1-for-25 after
September 2024, so Yahoo now reports $204.25 for a day Futu charged
$8.18; TQQQ forward-split 2-for-1 the other way. Every other symbol in
the portfolio measured within 13% of 1.0, which is what a volatile
session looks like and must not be mistaken for a split.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from app.services.splits import (
    EXACT_MULTIPLE_TOLERANCE,
    restate,
    snap_split_factor,
)


class TestSnapping:
    @pytest.mark.parametrize(
        ("ratio", "expected"),
        [
            # SQQQ, measured on three separate activities.
            ("25.491", Fraction(25)),
            ("24.970", Fraction(25)),
            ("24.500", Fraction(25)),
            # TQQQ.
            ("0.511", Fraction(1, 2)),
            ("0.500", Fraction(1, 2)),
        ],
    )
    def test_a_real_split_is_recognised(self, ratio: str, expected: Fraction) -> None:
        assert snap_split_factor(Decimal(ratio)) == expected

    @pytest.mark.parametrize(
        "ratio",
        [
            "1.000",  # agrees
            "1.114",  # 0823.HK — opening balance priced at average cost
            "0.914",  # NVDA on a volatile day
            "0.871",  # ITUB, the worst healthy reading in the portfolio
            "1.068",  # SOFI
            "0.931",  # DRAM
        ],
    )
    def test_ordinary_price_noise_is_not_a_split(self, ratio: str) -> None:
        assert snap_split_factor(Decimal(ratio)) is None

    def test_a_ratio_near_nothing_is_refused(self) -> None:
        """A wrong symbol or a bad print must not be "corrected"."""
        assert snap_split_factor(Decimal("1.7")) is None
        assert snap_split_factor(Decimal("13")) is None

    def test_a_quantity_ratio_needs_an_exact_multiple(self) -> None:
        """The tolerance the opening-balance guard uses, and why.

        2.707 held against 1.4 replayed is 1.934 — close enough to 2 to
        pass the price tolerance, and nothing like the exact doubling a
        split produces. Booking it as a split instead of a history gap
        would leave the portfolio understated.
        """
        ratio = Decimal("2.707") / Decimal("1.4")
        assert snap_split_factor(ratio) == Fraction(2)
        assert snap_split_factor(ratio, tolerance=EXACT_MULTIPLE_TOLERANCE) is None
        # A split really is exact: 2.707 shares become 5.414.
        exact = Decimal("5.414") / Decimal("2.707")
        assert snap_split_factor(exact, tolerance=EXACT_MULTIPLE_TOLERANCE) == Fraction(2)

    def test_a_nonsense_ratio_is_refused(self) -> None:
        assert snap_split_factor(Decimal("0")) is None
        assert snap_split_factor(Decimal("-2")) is None


class TestRestating:
    def test_the_money_does_not_move(self) -> None:
        """The whole safety argument for the repair, asserted.

        Cost basis, proceeds and realised P&L are all quantity x price.
        Restating changes how a position is valued between trades and
        must change nothing about what it cost.
        """
        quantity, price = Decimal("9"), Decimal("8.18")
        new_quantity, new_price = restate(quantity, price, Fraction(25))
        assert new_quantity * new_price == quantity * price

    def test_a_reverse_split_leaves_fewer_more_expensive_shares(self) -> None:
        # 25 shares at $8.18 became 1 share at $204.50 after 1-for-25.
        quantity, price = restate(Decimal("25"), Decimal("8.18"), Fraction(25))
        assert quantity == Decimal("1")
        assert price == Decimal("204.50")

    def test_a_forward_split_leaves_more_cheaper_shares(self) -> None:
        # 3 shares at $79.15 became 6 at $39.575 after 2-for-1.
        quantity, price = restate(Decimal("3"), Decimal("79.15"), Fraction(1, 2))
        assert quantity == Decimal("6")
        assert price == Decimal("39.575")
