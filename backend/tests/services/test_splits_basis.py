"""Reconciling against the basis Ghostfolio stores, not the broker's.

Once a split has been restated in Ghostfolio, the broker keeps reporting
the OLD scale forever. Every later sync then derives an opening balance
from raw broker numbers and pushes it beside activities measured
differently.

Live consequence, on the real portfolio: SQQQ's trades sat restated at
~$204 (a 1-for-25 reverse split) while a freshly derived opening balance
went in at $7.83 for 8 shares. A position that had been fully sold
replayed to 7.68 shares — 2,299 HKD the owner does not hold, inside a
98,990 HKD total.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction

from app.models.domain import Position, Transaction, TransactionType
from app.services.splits import align_to_stored_basis, detect_stored_basis


def _tx(symbol: str, qty: str, price: str) -> Transaction:
    return Transaction(
        source="futu", transaction_id=f"{symbol}-{qty}",
        symbol=symbol, side="buy", type=TransactionType.BUY,
        quantity=Decimal(qty), price=Decimal(price), currency="USD",
        timestamp=datetime(2024, 9, 3, tzinfo=UTC),
    )


class TestDetect:
    def test_same_basis_reports_nothing(self) -> None:
        report = detect_stored_basis([
            ("US.VOO", Decimal("529.74"), Decimal("529.74")),
            ("US.VOO", Decimal("611.91"), Decimal("611.92")),
        ])
        assert report.factors == {}
        assert report.conflicts == {}

    def test_reverse_split_is_found_exactly(self) -> None:
        # The real SQQQ rows: what Futu reports vs what Ghostfolio holds.
        report = detect_stored_basis([
            ("US.SQQQ", Decimal("8.7777"), Decimal("219.4425")),
            ("US.SQQQ", Decimal("8.18"), Decimal("204.5")),
            ("US.SQQQ", Decimal("7.83"), Decimal("195.75")),
        ])
        assert report.factors == {"US.SQQQ": Fraction(25)}

    def test_forward_split_is_found(self) -> None:
        report = detect_stored_basis([
            ("US.TQQQ", Decimal("74.0683"), Decimal("37.03415")),
            ("US.TQQQ", Decimal("79.15"), Decimal("39.575")),
        ])
        assert report.factors == {"US.TQQQ": Fraction(1, 2)}

    def test_disagreeing_rows_are_reported_never_repaired(self) -> None:
        """Half restated and half not means the stored history itself
        straddles the split. Picking a factor would corrupt one half."""
        report = detect_stored_basis([
            ("US.SQQQ", Decimal("8.18"), Decimal("204.5")),
            ("US.SQQQ", Decimal("7.83"), Decimal("7.83")),
        ])
        assert report.factors == {}
        assert "US.SQQQ" in report.conflicts


class TestAlign:
    def test_money_is_preserved_exactly(self) -> None:
        tx = _tx("US.SQQQ", "8", "8.7777")
        (aligned,), _ = align_to_stored_basis([tx], [], {"US.SQQQ": Fraction(25)})
        assert aligned.quantity * aligned.price == tx.quantity * tx.price
        assert aligned.quantity == Decimal("0.32")

    def test_untouched_symbols_pass_through(self) -> None:
        tx = _tx("US.VOO", "2", "529.74")
        (aligned,), _ = align_to_stored_basis([tx], [], {"US.SQQQ": Fraction(25)})
        assert aligned is tx

    def test_position_quantity_and_cost_both_move(self) -> None:
        position = Position(
            source="futu", symbol="US.TQQQ", quantity=Decimal("3"),
            avg_cost=Decimal("79.15"), currency="USD",
        )
        _, (aligned,) = align_to_stored_basis([], [position], {"US.TQQQ": Fraction(1, 2)})
        # A 2-for-1 forward split: twice the shares at half the cost.
        assert aligned.quantity == Decimal("6")
        assert aligned.avg_cost == Decimal("39.575")

    def test_the_phantom_position_does_not_appear(self) -> None:
        """The whole point. SQQQ was fully sold; before the fix the gap
        pass saw 8 unexplained shares and booked them."""
        from app.services.ghostfolio.opening import compute_gaps

        broker = [
            _tx("US.SQQQ", "8", "8.7777"),
            _tx("US.SQQQ", "9", "8.18"),
        ]
        sells = [
            Transaction(
                source="futu", transaction_id="s1", symbol="US.SQQQ",
                side="sell", type=TransactionType.SELL,
                quantity=Decimal("17"), price=Decimal("7.83"), currency="USD",
                timestamp=datetime(2024, 9, 19, tzinfo=UTC),
            )
        ]
        aligned, _ = align_to_stored_basis(
            broker + sells, [], {"US.SQQQ": Fraction(25)}
        )
        report = compute_gaps(positions=[], transactions=aligned)
        assert report.gaps == []
        assert report.no_cost == []
