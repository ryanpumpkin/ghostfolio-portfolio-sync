"""The portfolio-boundary cash record (§6.3, §8).

A duplicated deposit does not merely double a number — it silently
improves the return by pretending less was contributed than really was.
That is why this store is keyed and idempotent rather than append-only.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.cashflows import CashFlowStore, CashMovement


def _m(ident: str, amount: str, *, source: str = "ibkr",
       kind: str = "deposit", internal: bool = False) -> CashMovement:
    return CashMovement(
        external_id=ident, source=source, when=date(2025, 3, 6),
        amount=Decimal(amount), currency="USD", kind=kind, internal=internal,
    )


class TestStore:
    def test_round_trips_through_the_file(self, tmp_path) -> None:
        path = tmp_path / "cash.json"
        CashFlowStore(path).record([_m("a", "100"), _m("b", "-50")])
        reloaded = CashFlowStore(path).all()
        assert [m.external_id for m in reloaded] == ["a", "b"]
        assert reloaded[0].amount == Decimal("100")

    def test_re_recording_the_same_id_does_not_duplicate(self, tmp_path) -> None:
        path = tmp_path / "cash.json"
        store = CashFlowStore(path)
        assert store.record([_m("a", "100")]) == 1
        assert store.record([_m("a", "100")]) == 0
        assert len(store.all()) == 1

    def test_decimal_survives_the_json_round_trip(self, tmp_path) -> None:
        path = tmp_path / "cash.json"
        CashFlowStore(path).record([_m("a", "1784.25597281451865")])
        assert CashFlowStore(path).all()[0].amount == Decimal(
            "1784.25597281451865"
        )

    def test_replace_source_drops_movements_the_broker_forgot(
        self, tmp_path
    ) -> None:
        path = tmp_path / "cash.json"
        store = CashFlowStore(path)
        store.record([_m("a", "100"), _m("b", "200")])
        store.replace_source("ibkr", [_m("a", "100")])
        assert [m.external_id for m in store.all()] == ["a"]

    def test_replace_source_leaves_other_sources_alone(self, tmp_path) -> None:
        path = tmp_path / "cash.json"
        store = CashFlowStore(path)
        store.record([_m("a", "100"), _m("f1", "50", source="futu")])
        store.replace_source("ibkr", [])
        assert [m.external_id for m in store.all()] == ["f1"]

    def test_internal_transfers_are_kept_but_excluded(self, tmp_path) -> None:
        """A move between your own accounts is real money that is not a
        contribution. Storing it makes the exclusion auditable."""
        path = tmp_path / "cash.json"
        store = CashFlowStore(path)
        store.record([
            _m("a", "100"),
            _m("t", "-500", kind="transfer", internal=True),
        ])
        assert len(store.all()) == 2
        assert [m.external_id for m in store.external()] == ["a"]

    def test_a_corrupt_file_refuses_rather_than_starting_empty(
        self, tmp_path
    ) -> None:
        """Silently forgetting every contribution would inflate the
        return and look like a good day."""
        path = tmp_path / "cash.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(RuntimeError, match="cannot read"):
            CashFlowStore(path)

    def test_a_missing_file_is_simply_empty(self, tmp_path) -> None:
        assert CashFlowStore(tmp_path / "absent.json").all() == []


class TestMergeNotReplace:
    """A cheap sync must never erase an expensive backfill.

    Futu's securities account answers cash flow one clearing date at a
    time, so a daily sync can only afford to look at recent days. Under
    replace-the-source semantics the next such run would delete a
    year-long backfill.

    The two errors are also not symmetric: a forgotten deposit makes
    contributions look smaller and FLATTERS the return, while a stale
    one merely depresses it.
    """

    def test_a_short_window_keeps_older_rows(self, tmp_path) -> None:
        from datetime import UTC, datetime

        from app.models.domain import Transaction, TransactionType
        from app.services.ghostfolio.reconcile import _record_cash_movements

        path = tmp_path / "cash.json"
        store = CashFlowStore(path)
        store.record([
            _m("futu:cash:old", "5000", source="futu"),
            _m("futu:cash:older", "3000", source="futu"),
        ])

        fresh = Transaction(
            source="futu",
            transaction_id="new",
            external_id="futu:cash:new",
            symbol=None,
            side="Deposit",
            type=TransactionType.DEPOSIT,
            amount=Decimal("100"),
            currency="HKD",
            timestamp=datetime(2026, 9, 8, tzinfo=UTC),
        )
        _record_cash_movements(store, "futu", [fresh])

        kept = {m.external_id for m in CashFlowStore(path).all()}
        assert kept == {"futu:cash:old", "futu:cash:older", "futu:cash:new"}


class TestClassification:
    """Telling a bank transfer from a trade settlement.

    Futu's `get_acc_cash_flow` returns BOTH. A 736-day backfill produced
    197 rows read as deposits and withdrawals that were in fact trade
    settlements — -70.2216 USD is 8 SQQQ at 8.7777, not money from a
    bank. Counting those at the portfolio boundary double-counts every
    trade in the account.
    """

    def test_bank_transfers_are_external(self) -> None:
        from app.services.cashflows import classify_cash_type

        for label in ("Deposit", "WITHDRAWAL", "Transfer In", "入金", "出金"):
            internal, unclassified = classify_cash_type(label)
            assert (internal, unclassified) == (False, False), label

    def test_trade_settlements_are_internal(self) -> None:
        from app.services.cashflows import classify_cash_type

        for label in ("Buy", "Sell settlement", "Dividend", "Commission",
                      "買入", "股息"):
            internal, unclassified = classify_cash_type(label)
            assert internal is True, label
            assert unclassified is False, label

    def test_an_unknown_label_is_flagged_not_assumed(self) -> None:
        """Not internal, not external — flagged.

        Assuming internal would drop a possible contribution and flatter
        the return; assuming external would invent one.
        """
        from app.services.cashflows import classify_cash_type

        assert classify_cash_type("Securities Lending Rebate") == (False, True)
        assert classify_cash_type("") == (False, True)

    def test_internal_wins_over_a_coincidental_external_word(self) -> None:
        from app.services.cashflows import classify_cash_type

        # "Buy" decides it; the word "transfer in" must not rescue it.
        assert classify_cash_type("Buy — transfer in settlement")[0] is True

    def test_unclassified_rows_are_kept_out_of_external(self, tmp_path) -> None:
        path = tmp_path / "cash.json"
        store = CashFlowStore(path)
        store.record([
            CashMovement(
                external_id="x", source="futu", when=date(2025, 1, 1),
                amount=Decimal("100"), currency="HKD", kind="deposit",
                raw_type="Mystery", unclassified=True,
            ),
            _m("ok", "50"),
        ])
        assert [m.external_id for m in store.external()] == ["ok"]
        assert [m.external_id for m in store.unclassified()] == ["x"]


class TestFutuVocabulary:
    """Futu's real `cashflow_type` values, confirmed against 197 live rows.

    `Others` is the one that matters: 80 of its 90 rows match a Futu
    trade's gross amount to within 3% — -70.2216 USD is 8 SQQQ at
    8.7777. It is the settlement leg, not money arriving from a bank,
    and counting it at the boundary double-counts every trade.
    """

    def test_others_is_a_settlement(self) -> None:
        from app.services.cashflows import classify_cash_type

        assert classify_cash_type("Others") == (True, False)

    def test_fund_and_fee_types_are_internal(self) -> None:
        from app.services.cashflows import classify_cash_type

        for label in ("Fund Subscription", "Fund Redemption", "Coupon",
                      "Currency Exchange", "Corporate Action Service Fee",
                      "ADR Fee", "Scrip Fee", "Cash Dividend", "Dividend Tax"):
            internal, unclassified = classify_cash_type(label)
            assert internal is True, label
            assert unclassified is False, label

    def test_money_transfers_is_ambiguous_not_a_contribution(self) -> None:
        """All six live rows are moves between the owner's OWN Futu
        accounts — a -300/+300 HKD pair netting to zero, and -2,000 HKD
        the day before a 1,980 HKD crypto purchase. None is a bank
        transfer. But the same label would carry a real deposit, so it
        is flagged rather than decided: counted it invents a
        contribution, dropped it hides one."""
        from app.services.cashflows import classify_cash_type

        assert classify_cash_type("Money Transfers") == (False, True)

    def test_ambiguous_outranks_a_coincidental_internal_word(self) -> None:
        from app.services.cashflows import classify_cash_type

        assert classify_cash_type("Money Transfer — fee")[1] is True
