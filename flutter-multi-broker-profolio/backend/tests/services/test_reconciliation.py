"""Reconciliation of authoritative vs derived quantities (spec §6.4, §4.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.domain import (
    Position,
    SourceHealthStatus,
    Transaction,
    TransactionType,
)
from app.services.reconciliation import (
    ReconcileStatus,
    derived_quantities,
    reconcile,
)

_WHEN = datetime(2026, 1, 15, tzinfo=UTC)


def _pos(symbol: str, qty: str, *, source: str = "binance", custody: str | None = None) -> Position:
    return Position(
        source=source,
        custody=custody,
        symbol=symbol,
        quantity=Decimal(qty),
        currency="USD",
    )


def _tx(
    symbol: str,
    qty: str,
    side: str,
    *,
    source: str = "binance",
    tid: str = "t",
    tx_type: TransactionType | None = None,
) -> Transaction:
    return Transaction(
        source=source,
        transaction_id=tid,
        symbol=symbol,
        side=side,
        type=tx_type,
        quantity=Decimal(qty),
        currency="USD",
        timestamp=_WHEN,
    )


class TestAgreement:
    def test_matching_quantities_are_ok(self) -> None:
        report = reconcile(
            [_pos("BTCUSDT", "1.5")],
            [_tx("BTCUSDT", "1.5", "buy")],
        )
        assert report.ok
        assert report.items[0].status is ReconcileStatus.OK

    def test_buys_minus_sells(self) -> None:
        report = reconcile(
            [_pos("BTCUSDT", "0.5")],
            [
                _tx("BTCUSDT", "2", "buy", tid="b1"),
                _tx("BTCUSDT", "1.5", "sell", tid="s1"),
            ],
        )
        assert report.ok

    def test_small_difference_inside_tolerance_is_ok(self) -> None:
        # 0.4% drift, default tolerance 0.5%.
        report = reconcile(
            [_pos("BTCUSDT", "1.0")], [_tx("BTCUSDT", "0.996", "buy")]
        )
        assert report.ok


class TestDrift:
    def test_difference_beyond_tolerance_is_reported(self) -> None:
        report = reconcile(
            [_pos("BTCUSDT", "0.5120")], [_tx("BTCUSDT", "0.4980", "buy")]
        )
        item = report.items[0]
        assert item.status is ReconcileStatus.DRIFT
        assert item.delta == Decimal("0.0140")
        assert item.delta_pct is not None
        assert Decimal("0.027") < item.delta_pct < Decimal("0.028")

    def test_nothing_is_auto_corrected(self) -> None:
        # §6.4: "Do not auto-correct." The authoritative and derived
        # numbers must both survive into the report — adjusting either
        # destroys the only evidence something is missing.
        positions = [_pos("BTCUSDT", "0.5120")]
        transactions = [_tx("BTCUSDT", "0.4980", "buy")]
        report = reconcile(positions, transactions)
        assert report.items[0].authoritative == Decimal("0.5120")
        assert report.items[0].derived == Decimal("0.4980")
        assert positions[0].quantity == Decimal("0.5120")
        assert transactions[0].quantity == Decimal("0.4980")

    def test_per_asset_tolerance_overrides_the_default(self) -> None:
        args = ([_pos("BTCUSDT", "1.0")], [_tx("BTCUSDT", "0.97", "buy")])
        assert not reconcile(*args).ok
        loose = reconcile(*args, tolerance_by_asset={"CRYPTO:BTC": Decimal("0.05")})
        assert loose.ok

    def test_crypto_hint_points_at_convert_and_dust(self) -> None:
        report = reconcile(
            [_pos("BTCUSDT", "1.0")], [_tx("BTCUSDT", "0.5", "buy")]
        )
        hint = report.items[0].hint()
        assert hint is not None
        assert "convert" in hint

    def test_equity_hint_points_at_corporate_actions(self) -> None:
        # §6.5: splits and rights issues are the first thing to check when
        # an equity quantity fails to reconcile.
        report = reconcile(
            [_pos("700.HK", "200", source="longbridge")],
            [_tx("700.HK", "100", "buy", source="longbridge")],
        )
        hint = report.items[0].hint()
        assert hint is not None
        assert "corporate action" in hint


class TestMissingSides:
    def test_a_holding_with_no_history_is_not_called_drift(self) -> None:
        # Before the historical import runs, every holding has no history.
        # Reporting that as portfolio-wide drift would train you to ignore
        # the warning that actually matters.
        report = reconcile([_pos("BTCUSDT", "1.0")], [])
        assert report.items[0].status is ReconcileStatus.NO_HISTORY
        assert "historical import" in (report.items[0].hint() or "")

    def test_history_without_a_position_is_flagged(self) -> None:
        report = reconcile([], [_tx("BTCUSDT", "1.0", "buy")])
        assert report.items[0].status is ReconcileStatus.NO_POSITION

    def test_a_fully_closed_position_reconciles(self) -> None:
        report = reconcile(
            [],
            [
                _tx("BTCUSDT", "1", "buy", tid="b"),
                _tx("BTCUSDT", "1", "sell", tid="s"),
            ],
        )
        assert report.ok


class TestCustodySeparation:
    """§4.4 — the same asset in two places must not be summed blindly."""

    def test_the_same_asset_reconciles_per_location(self) -> None:
        # The exact §4.4 scenario: a sub-minimum balance stuck at Futu
        # waiting for the withdrawal threshold, plus the Ledger balance.
        positions = [
            _pos("BTC", "0.002", source="futu", custody="futu"),
            _pos("BTC", "0.00460179", source="manual", custody="ledger"),
        ]
        transactions = [
            _tx("BTC", "0.002", "buy", source="futu", tid="f1"),
        ]
        report = reconcile(positions, transactions)
        by_custody = {i.custody: i for i in report.items}
        assert by_custody["futu"].status is ReconcileStatus.OK
        assert by_custody["ledger"].status is ReconcileStatus.NO_HISTORY

    def test_blind_summing_would_have_hidden_the_gap(self) -> None:
        # If the two locations were summed, authoritative 0.00660179 vs
        # derived 0.002 would read as one big drift instead of "futu is
        # fine, ledger has no history" — the wrong diagnosis entirely.
        positions = [
            _pos("BTC", "0.002", source="futu", custody="futu"),
            _pos("BTC", "0.00460179", source="manual", custody="ledger"),
        ]
        report = reconcile(positions, [_tx("BTC", "0.002", "buy", source="futu")])
        assert len(report.items) == 2
        assert {i.status for i in report.items} == {
            ReconcileStatus.OK,
            ReconcileStatus.NO_HISTORY,
        }


class TestTransfersAffectDerivedQuantity:
    """Excluded from Ghostfolio (§6.3), essential here (§4.4)."""

    def test_a_withdrawal_reduces_the_source_quantity(self) -> None:
        derived = derived_quantities(
            [
                _tx("BTCUSDT", "1.0", "buy", tid="b"),
                _tx("BTCUSDT", "0.4", "withdrawal", tid="w"),
            ]
        )
        assert derived[("CRYPTO:BTC", "binance")] == Decimal("0.6")

    def test_a_transfer_out_reduces_the_source_quantity(self) -> None:
        derived = derived_quantities(
            [
                _tx("BTCUSDT", "1.0", "buy", tid="b"),
                _tx(
                    "BTCUSDT", "0.4", "withdrawal", tid="t",
                    tx_type=TransactionType.TRANSFER,
                ),
            ]
        )
        assert derived[("CRYPTO:BTC", "binance")] == Decimal("0.6")

    def test_a_deposit_increases_the_destination_quantity(self) -> None:
        derived = derived_quantities(
            [_tx("BTC", "0.5", "deposit", source="ledger", tid="d")]
        )
        assert derived[("CRYPTO:BTC", "ledger")] == Decimal("0.5")

    def test_ignoring_transfers_would_report_false_drift(self) -> None:
        # Bought 1 BTC on Binance, moved 0.4 to the Ledger. Binance now
        # holds 0.6. If transfers did not reduce the derived quantity, the
        # derived figure would still say 1.0 and this would look like a
        # 67% discrepancy at every venue the owner has ever moved from.
        report = reconcile(
            [_pos("BTCUSDT", "0.6")],
            [
                _tx("BTCUSDT", "1.0", "buy", tid="b"),
                _tx("BTCUSDT", "0.4", "withdrawal", tid="w"),
            ],
        )
        assert report.ok


class TestReporting:
    def test_source_health_marks_drift_as_degraded(self) -> None:
        report = reconcile(
            [_pos("BTCUSDT", "1.0")], [_tx("BTCUSDT", "0.5", "buy")]
        )
        health = report.to_source_health()
        assert health[0].source == "binance"
        assert health[0].status is SourceHealthStatus.DEGRADED
        assert "mismatch" in (health[0].message or "")

    def test_no_history_is_not_degraded(self) -> None:
        # A first run should not paint every source amber.
        health = reconcile([_pos("BTCUSDT", "1.0")], []).to_source_health()
        assert health[0].status is SourceHealthStatus.OK

    def test_clean_reconciliation_reports_no_message(self) -> None:
        health = reconcile(
            [_pos("BTCUSDT", "1.0")], [_tx("BTCUSDT", "1.0", "buy")]
        ).to_source_health()
        assert health[0].status is SourceHealthStatus.OK
        assert health[0].message is None

    def test_digest_line_matches_the_specs_format(self) -> None:
        # §10's example:
        #   BTC — authoritative 0.5120 / derived 0.4980 (delta 2.7%)
        report = reconcile(
            [_pos("BTCUSDT", "0.5120")], [_tx("BTCUSDT", "0.4980", "buy")]
        )
        line = report.digest_lines()[0]
        assert "authoritative 0.5120" in line
        assert "derived 0.4980" in line
        assert "2.7%" in line

    def test_clean_report_has_no_digest_lines(self) -> None:
        report = reconcile(
            [_pos("BTCUSDT", "1.0")], [_tx("BTCUSDT", "1.0", "buy")]
        )
        assert report.digest_lines() == []


class TestSymbolNormalisation:
    def test_different_source_spellings_reconcile_together(self) -> None:
        # A LongBridge position reported as 700.HK against Futu-style
        # HK.00700 history must not look like two separate instruments.
        report = reconcile(
            [_pos("700.HK", "100", source="longbridge")],
            [_tx("HK.00700", "100", "buy", source="longbridge")],
        )
        assert report.ok
        assert report.items[0].symbol == "HK:00700"

    def test_an_unresolvable_symbol_still_reconciles_against_itself(self) -> None:
        # Dropping it would hide the holding completely — the exact failure
        # §6.4 exists to catch.
        report = reconcile(
            [_pos("WEIRDTICKER", "10")], [_tx("WEIRDTICKER", "10", "buy")]
        )
        assert report.ok
        assert report.items[0].symbol == "WEIRDTICKER"
