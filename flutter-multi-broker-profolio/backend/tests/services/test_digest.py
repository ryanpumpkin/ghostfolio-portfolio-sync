"""Monthly portfolio digest (spec §10, §9.1)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.domain import Position, Transaction, TransactionType
from app.services.allocation import (
    Holding,
    Target,
    allocate_new_money,
    compute_drift,
)
from app.services.digest import BANK_CASH_STALE_DAYS, compose_digest
from app.services.reconciliation import reconcile

_TODAY = date(2026, 10, 1)
_CLASSIFICATION = {
    "US:VOO": "us_equity",
    "HK:00700": "hk_equity",
    "CRYPTO:BTC": "crypto",
}
_TARGETS = {
    "us_equity": Target(target=Decimal(45), band=Decimal(5)),
    "hk_equity": Target(target=Decimal(20), band=Decimal(5)),
    "crypto": Target(target=Decimal(2), band=Decimal(2)),
    "cash": Target(target=Decimal(10), band=Decimal(5)),
}


def _drift():
    return compute_drift(
        [
            Holding("US:VOO", Decimal(47200)),
            Holding("HK:00700", Decimal(16100)),
            Holding("CRYPTO:BTC", Decimal(2100)),
        ],
        cash=Decimal(34600),
        classification=_CLASSIFICATION,
        targets=_TARGETS,
    )


def _clean_reconciliation():
    return reconcile([], [])


def _reconciliation_with_drift():
    position = Position(
        source="binance", symbol="BTCUSDT", quantity=Decimal("0.5120"),
        currency="USD",
    )
    activity = Transaction(
        source="binance", transaction_id="t1", symbol="BTCUSDT", side="buy",
        type=TransactionType.BUY, quantity=Decimal("0.4980"), currency="USD",
        timestamp=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC),
    )
    return reconcile([position], [activity])


class TestHeader:
    def test_starts_with_the_date_line(self) -> None:
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), as_of=_TODAY,
        )
        assert text.splitlines()[0] == "Portfolio — 2026-10-01"

    def test_net_worth_uses_the_base_currency(self) -> None:
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), as_of=_TODAY,
        )
        assert "Net worth: HKD 100,000" in text

    def test_month_on_month_when_a_baseline_exists(self) -> None:
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(110000), previous_net_worth=Decimal(100000),
            as_of=_TODAY,
        )
        assert "MoM +10.0%" in text

    def test_no_fake_baseline_on_the_first_digest(self) -> None:
        # Printing "MoM +0.0%" with nothing to compare against would read
        # as a real, flat month.
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), as_of=_TODAY,
        )
        assert "MoM" not in text

    def test_a_fall_is_signed(self) -> None:
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(90000), previous_net_worth=Decimal(100000),
            as_of=_TODAY,
        )
        assert "MoM -10.0%" in text


class TestDriftTable:
    def test_includes_every_class_with_its_label(self) -> None:
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), as_of=_TODAY,
        )
        assert "Class" in text
        for name in ("us_equity", "hk_equity", "crypto", "cash"):
            assert name in text

    def test_flags_a_breached_class(self) -> None:
        # cash at 34.6% against a 10% target, band 5.
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), as_of=_TODAY,
        )
        cash_line = next(ln for ln in text.splitlines() if ln.startswith("cash"))
        assert "OVER" in cash_line


class TestAllocation:
    def test_shows_where_the_next_contribution_goes(self) -> None:
        drift = _drift()
        plan = allocate_new_money(drift, Decimal(50000))
        text = compose_digest(
            drift=drift, reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), allocation=plan, as_of=_TODAY,
        )
        assert "Next HKD 50,000" in text

    def test_omitted_when_no_contribution_is_planned(self) -> None:
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), as_of=_TODAY,
        )
        assert "Next HKD" not in text


class TestReconciliationSection:
    def test_clean_says_so_explicitly(self) -> None:
        # Silence would be ambiguous — "clean" and "we did not check" must
        # not look the same.
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), as_of=_TODAY,
        )
        assert "Reconciliation: clean" in text

    def test_a_warning_is_counted_and_detailed(self) -> None:
        text = compose_digest(
            drift=_drift(), reconciliation=_reconciliation_with_drift(),
            net_worth=Decimal(100000), as_of=_TODAY,
        )
        assert "Reconciliation: 1 warning" in text
        assert "authoritative 0.5120" in text
        assert "derived 0.4980" in text

    def test_plural_is_correct(self) -> None:
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), as_of=_TODAY,
        )
        assert "1 warnings" not in text


class TestBankCashPrompt:
    """§9.1 — bank cash is manual by design, so the digest nags."""

    def test_prompts_once_stale(self) -> None:
        stale = date(2026, 8, 21)  # 41 days before 2026-10-01
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), as_of=_TODAY, bank_cash_updated=stale,
        )
        assert "Bank cash last updated 41 days ago — please refresh." in text

    def test_quiet_when_fresh(self) -> None:
        fresh = date(2026, 9, 25)
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), as_of=_TODAY, bank_cash_updated=fresh,
        )
        assert "Bank cash" not in text

    def test_boundary_is_not_off_by_one(self) -> None:
        exactly = date(_TODAY.year, _TODAY.month, _TODAY.day)
        from datetime import timedelta

        at_limit = exactly - timedelta(days=BANK_CASH_STALE_DAYS)
        over_limit = exactly - timedelta(days=BANK_CASH_STALE_DAYS + 1)
        assert "Bank cash" not in compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(1), as_of=_TODAY, bank_cash_updated=at_limit,
        )
        assert "Bank cash" in compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(1), as_of=_TODAY, bank_cash_updated=over_limit,
        )

    def test_never_recorded_is_called_out(self) -> None:
        text = compose_digest(
            drift=_drift(), reconciliation=_clean_reconciliation(),
            net_worth=Decimal(100000), as_of=_TODAY, bank_cash_updated=None,
        )
        assert "never been recorded" in text


class TestPlainTextDiscipline:
    """§10: 'Keep it plain text.'"""

    @pytest.fixture
    def text(self) -> str:
        drift = _drift()
        return compose_digest(
            drift=drift,
            reconciliation=_reconciliation_with_drift(),
            net_worth=Decimal(100000),
            previous_net_worth=Decimal(95000),
            allocation=allocate_new_money(drift, Decimal(50000)),
            as_of=_TODAY,
            bank_cash_updated=date(2026, 8, 21),
        )

    def test_contains_no_markup(self, text: str) -> None:
        for token in ("<html", "<p>", "<div", "<table", "<br", "](http"):
            assert token not in text.lower()

    def test_fits_a_phone_screen_width(self, text: str) -> None:
        # Wrapping destroys the drift table's alignment, which is the one
        # thing that makes it scannable.
        too_wide = [ln for ln in text.splitlines() if len(ln) > 60]
        assert not too_wide, too_wide

    def test_does_not_end_with_blank_lines(self, text: str) -> None:
        assert text == text.rstrip()

    def test_reads_top_to_bottom_in_the_specs_order(self, text: str) -> None:
        body = text
        assert body.index("Portfolio —") < body.index("Net worth:")
        assert body.index("Net worth:") < body.index("Class")
        assert body.index("Class") < body.index("Next HKD")
        assert body.index("Next HKD") < body.index("Reconciliation:")
        assert body.index("Reconciliation:") < body.index("Bank cash")


class TestNotChecked:
    """"Clean" and "nobody looked" are different claims.

    The digest runs offline, days after the last sync. Printing "clean"
    when no reconciliation has been recorded is the exact false
    reassurance this section exists to prevent — and it is the line you
    stop reading precisely because it is always reassuring.
    """

    @staticmethod
    def _empty_drift():
        from decimal import Decimal

        from app.services.allocation import DriftReport

        return DriftReport(total_value=Decimal("100"))

    def test_unchecked_says_so(self) -> None:
        from decimal import Decimal

        from app.services.digest import compose_digest
        from app.services.reconciliation import ReconcileReport

        text = compose_digest(
            drift=self._empty_drift(),
            reconciliation=ReconcileReport(),
            net_worth=Decimal("100"),
            reconciliation_checked=False,
        )
        assert "NOT CHECKED" in text
        assert "Reconciliation: clean" not in text

    def test_checked_and_empty_is_clean(self) -> None:
        from decimal import Decimal

        from app.services.digest import compose_digest
        from app.services.reconciliation import ReconcileReport

        text = compose_digest(
            drift=self._empty_drift(),
            reconciliation=ReconcileReport(),
            net_worth=Decimal("100"),
            reconciliation_checked=True,
        )
        assert "Reconciliation: clean" in text

    def test_bank_prompt_can_be_suppressed(self) -> None:
        from decimal import Decimal

        from app.services.digest import compose_digest
        from app.services.reconciliation import ReconcileReport

        text = compose_digest(
            drift=self._empty_drift(),
            reconciliation=ReconcileReport(),
            net_worth=Decimal("100"),
            prompt_bank_cash=False,
        )
        assert "Bank cash" not in text
