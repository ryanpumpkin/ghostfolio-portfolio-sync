"""Return measures (spec §8, §10).

The three figures answer three different questions, and the tests here
pin the differences — because quoting the wrong one is how a portfolio
looks better or worse than it is.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.returns import (
    CashFlow,
    build_report,
    simple_return,
    xirr,
    xnpv,
)


def _f(y: int, m: int, d: int, amount: str) -> CashFlow:
    return CashFlow(when=date(y, m, d), amount=Decimal(amount))


class TestXirr:
    def test_doubling_in_one_year_is_100_percent(self) -> None:
        # 2025 is not a leap year: exactly 365 days, so act/365 gives a
        # clean 100%. Across 2024 the same doubling reads 99.62%, which
        # is correct for a 366-day year, not an error.
        rate = xirr([_f(2025, 1, 1, "-100"), _f(2026, 1, 1, "200")])
        assert rate is not None
        assert abs(rate - 1.0) < 1e-4

    def test_a_leap_year_is_slightly_more_than_a_year(self) -> None:
        rate = xirr([_f(2024, 1, 1, "-100"), _f(2025, 1, 1, "200")])
        assert rate is not None
        assert 0.995 < rate < 1.0

    def test_flat_is_zero(self) -> None:
        rate = xirr([_f(2024, 1, 1, "-100"), _f(2025, 1, 1, "100")])
        assert rate is not None
        assert abs(rate) < 1e-6

    def test_a_loss_is_negative(self) -> None:
        rate = xirr([_f(2024, 1, 1, "-100"), _f(2025, 1, 1, "50")])
        assert rate is not None and rate < -0.4

    def test_all_one_direction_has_no_root(self) -> None:
        # Nothing ever came back. No rate reconciles that, and inventing
        # one would be worse than saying so.
        assert xirr([_f(2024, 1, 1, "-100"), _f(2025, 1, 1, "-50")]) is None

    def test_a_single_flow_is_not_a_return(self) -> None:
        assert xirr([_f(2024, 1, 1, "-100")]) is None

    def test_npv_at_the_solved_rate_is_zero(self) -> None:
        flows = [
            _f(2024, 1, 1, "-1000"),
            _f(2024, 6, 1, "-500"),
            _f(2025, 3, 1, "300"),
            _f(2026, 1, 1, "1400"),
        ]
        rate = xirr(flows)
        assert rate is not None
        assert abs(xnpv(rate, flows)) < 1e-6

    def test_adding_money_before_a_decline_lowers_the_rate(self) -> None:
        """Why money-weighted can sit below time-weighted.

        The holding doubles in year one and halves in year two, so
        buy-and-hold ends flat — a 0% time-weighted result either way.
        The investor who adds at the peak still loses, because MORE of
        their capital was present for the bad year. Timing is exactly
        what time-weighted removes and money-weighted keeps.
        """
        held = xirr([_f(2024, 1, 1, "-100"), _f(2026, 1, 1, "100")])
        topped_up = xirr([
            _f(2024, 1, 1, "-100"),
            _f(2025, 1, 1, "-100"),   # bought at the peak
            _f(2026, 1, 1, "150"),    # 300 halved
        ])
        assert held is not None and topped_up is not None
        assert abs(held) < 1e-6
        assert topped_up < held

    def test_less_capital_time_for_the_same_profit_raises_the_rate(self) -> None:
        """The mirror case, and the one that is easy to get backwards.

        Same money in and same money out, but the second contribution
        was only deployed for three months — so the RATE it earned is
        higher, not lower.
        """
        early = xirr([_f(2024, 1, 1, "-200"), _f(2026, 1, 1, "260")])
        late = xirr([_f(2024, 1, 1, "-100"), _f(2025, 10, 1, "-100"),
                     _f(2026, 1, 1, "260")])
        assert early is not None and late is not None
        assert late > early


class TestSimpleReturn:
    def test_profit_over_capital(self) -> None:
        assert simple_return(Decimal("50"), Decimal("500")) == 0.1

    def test_no_capital_is_not_zero_percent(self) -> None:
        # Zero deployed is undefined, not break-even.
        assert simple_return(Decimal("50"), Decimal("0")) is None


class TestReport:
    def test_excluded_cash_is_recorded_as_an_assumption(self) -> None:
        report = build_report(
            flows=[_f(2024, 1, 1, "-1000")],
            terminal_value=Decimal("1200"),
            cash_excluded=Decimal("300"),
            today=date(2025, 1, 1),
        )
        assert any("account cash" in a for a in report.assumptions)
        assert report.cash_excluded == Decimal("300")

    def test_terminal_flow_is_added_once(self) -> None:
        report = build_report(
            flows=[_f(2024, 1, 1, "-1000")],
            terminal_value=Decimal("1200"),
            today=date(2025, 1, 1),
        )
        assert report.flows == 2
        assert report.deployed_xirr is not None
        assert abs(report.deployed_xirr - 0.2) < 1e-3

    def test_paid_and_received_are_split_by_sign(self) -> None:
        report = build_report(
            flows=[_f(2024, 1, 1, "-1000"), _f(2024, 6, 1, "250")],
            terminal_value=Decimal("900"),
            today=date(2025, 1, 1),
        )
        assert report.paid_out == Decimal("1000")
        assert report.received == Decimal("250")

    def test_span_is_measured_from_the_first_flow(self) -> None:
        report = build_report(
            flows=[_f(2024, 1, 1, "-1000")],
            terminal_value=Decimal("1100"),
            today=date(2026, 1, 1),
        )
        assert abs(report.span_years - 2.0) < 0.01


class TestAnnualisation:
    """A cumulative figure and an annualised one are not comparable.

    Setting Ghostfolio's cumulative TWR beside a money-weighted annual
    rate makes contribution timing look far more damaging than it is.
    On the real portfolio that read as a 5.7-point gap; annualised, the
    two sit 0.12 points apart.
    """

    def test_cumulative_twr_is_annualised_over_the_span(self) -> None:
        report = build_report(
            flows=[_f(2024, 1, 1, "-1000")],
            terminal_value=Decimal("1100"),
            twr=0.1087,
            today=date(2026, 1, 1),
        )
        assert report.twr == 0.1087
        assert report.twr_annualised is not None
        assert abs(report.twr_annualised - 0.0529) < 5e-4

    def test_one_year_annualises_to_itself(self) -> None:
        report = build_report(
            flows=[_f(2025, 1, 1, "-1000")],
            terminal_value=Decimal("1100"),
            twr=0.10,
            today=date(2026, 1, 1),
        )
        assert report.twr_annualised is not None
        assert abs(report.twr_annualised - 0.10) < 1e-3

    def test_no_twr_means_no_annualised_twr(self) -> None:
        report = build_report(
            flows=[_f(2024, 1, 1, "-1000")],
            terminal_value=Decimal("1100"),
            today=date(2026, 1, 1),
        )
        assert report.twr_annualised is None
