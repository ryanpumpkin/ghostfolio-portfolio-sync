"""Return measures, and the honest difference between them (spec §8, §10).

Three numbers answer three different questions, and quoting the wrong
one is how a portfolio looks better or worse than it is:

* **Simple return** — profit over the capital deployed *today*. Easy to
  compute, and understates any portfolio whose money arrived recently.
* **Time-weighted (TWR)** — what Ghostfolio reports. Removes the effect
  of *when* money was added, so it measures the holdings rather than the
  saver. This is the figure comparable to an index.
* **Money-weighted (XIRR)** — the annualised rate the actual cash flows
  earned. Includes timing, so it measures the investor's real
  experience. When it sits below TWR, contributions were poorly timed —
  not necessarily that the holdings were bad.

What this module does NOT claim
--------------------------------
A textbook portfolio IRR uses flows at the portfolio *boundary* —
deposits in, withdrawals out, ending net worth — and treats trades as
internal. That needs deposit history from every broker, which §6.3
excludes from the activity set and most adapters do not report at all.

So `deployed_capital_xirr` measures something narrower and says so: the
money-weighted return on capital deployed *into positions*, with each
BUY an outflow and each SELL an inflow. Its terminal value therefore
excludes account cash — counting cash that arrived by an unrecorded
deposit would credit the portfolio with money no outflow ever paid for.

Every approximation a run actually relied on is recorded in
`ReturnsReport.assumptions` rather than left in a docstring, because the
number gets quoted and the docstring does not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

_LOG = logging.getLogger("mbp.returns")

#: Bounds for the XIRR search. -99.99% is total loss; 1000% a year is far
#: past anything a bisection needs to reach for a real portfolio.
_RATE_LOW = -0.9999
_RATE_HIGH = 10.0
_ITERATIONS = 200


@dataclass(frozen=True, slots=True)
class CashFlow:
    """One dated movement, positive into your pocket."""

    when: date
    amount: Decimal
    label: str = ""


@dataclass(slots=True)
class ReturnsReport:
    """Every figure, plus what had to be assumed to get it."""

    #: The textbook figure: flows at the portfolio BOUNDARY (deposits in,
    #: withdrawals out) against full net worth. None until every source
    #: reports its cash movements — a partial set understates
    #: contributions and flatters the return, so it is not computed from
    #: whatever happens to be available.
    portfolio_xirr: float | None = None
    deployed_xirr: float | None = None
    simple: float | None = None
    #: Ghostfolio's figure, CUMULATIVE over the whole span.
    twr: float | None = None
    #: The same, annualised. Kept as its own field because comparing a
    #: cumulative TWR against an annualised money-weighted rate makes
    #: contribution timing look far more damaging than it is — 10.87%
    #: over two years is 5.27% a year, not a figure to set beside 5.15%
    #: and call a six-point gap.
    twr_annualised: float | None = None
    terminal_value: Decimal = Decimal("0")
    cash_excluded: Decimal = Decimal("0")
    paid_out: Decimal = Decimal("0")
    received: Decimal = Decimal("0")
    flows: int = 0
    first_flow: date | None = None
    span_years: float = 0.0
    assumptions: list[str] = field(default_factory=list)

    def describe(self) -> list[str]:
        def pct(v: float | None) -> str:
            return "n/a" if v is None else f"{v:.2%}"

        lines = [
            f"time-weighted (TWR), cumulative      {pct(self.twr)}",
            f"time-weighted (TWR), annualised      {pct(self.twr_annualised)} per year",
            f"money-weighted, portfolio (IRR)     {pct(self.portfolio_xirr)} per year",
            f"money-weighted on deployed capital   {pct(self.deployed_xirr)} per year",
            f"simple (profit / capital today)      {pct(self.simple)}",
            "",
            "  the two annualised figures are the comparable pair;"
            " the cumulative one is not",
            "",
            f"terminal value (ex-cash)  {self.terminal_value:,.2f}",
            f"cash excluded             {self.cash_excluded:,.2f}",
            f"paid out / received back  {self.paid_out:,.2f} / {self.received:,.2f}",
            f"{self.flows} cash flow(s) from {self.first_flow} "
            f"({self.span_years:.2f} years)",
        ]
        if self.assumptions:
            lines += ["", "assumptions this run relied on:"]
            lines += [f"  - {a}" for a in self.assumptions]
        return lines


def xnpv(rate: float, flows: list[CashFlow]) -> float:
    """Net present value of dated flows at an annual `rate`."""
    if not flows:
        return 0.0
    start = min(f.when for f in flows)
    return sum(
        float(f.amount) / (1.0 + rate) ** ((f.when - start).days / 365.0)
        for f in flows
    )


def xirr(flows: list[CashFlow]) -> float | None:
    """The annual rate at which these flows have zero NPV.

    Bisection rather than Newton: it cannot diverge, and an irregular
    real-world flow series is exactly where Newton wanders off. Returns
    None when no sign change brackets a root — which happens when every
    flow points the same way, and no rate can reconcile that.
    """
    if len(flows) < 2:
        return None
    low, high = _RATE_LOW, _RATE_HIGH
    npv_low, npv_high = xnpv(low, flows), xnpv(high, flows)
    if npv_low * npv_high > 0:
        return None
    for _ in range(_ITERATIONS):
        mid = (low + high) / 2
        if xnpv(low, flows) * xnpv(mid, flows) <= 0:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def simple_return(profit: Decimal, invested: Decimal) -> float | None:
    """Profit over capital deployed today. None when nothing is deployed."""
    if invested == 0:
        return None
    return float(profit / invested)


def build_report(
    *,
    flows: list[CashFlow],
    terminal_value: Decimal,
    cash_excluded: Decimal = Decimal("0"),
    twr: float | None = None,
    invested_today: Decimal | None = None,
    profit_today: Decimal | None = None,
    boundary_flows: list[CashFlow] | None = None,
    net_worth: Decimal | None = None,
    boundary_complete: bool = False,
    assumptions: list[str] | None = None,
    today: date | None = None,
) -> ReturnsReport:
    """Assemble the three measures from one set of flows."""
    as_of = today or date.today()
    priced = [*flows, CashFlow(when=as_of, amount=terminal_value, label="terminal")]
    report = ReturnsReport(
        deployed_xirr=xirr(priced),
        twr=twr,
        terminal_value=terminal_value,
        cash_excluded=cash_excluded,
        paid_out=-sum((f.amount for f in flows if f.amount < 0), Decimal("0")),
        received=sum((f.amount for f in flows if f.amount > 0), Decimal("0")),
        flows=len(priced),
        first_flow=min((f.when for f in priced), default=None),
        assumptions=list(assumptions or []),
    )
    if report.first_flow is not None:
        report.span_years = (as_of - report.first_flow).days / 365.0
    if twr is not None and report.span_years > 0:
        report.twr_annualised = (1.0 + twr) ** (1.0 / report.span_years) - 1.0
    if invested_today is not None and profit_today is not None:
        report.simple = simple_return(profit_today, invested_today)
    # The portfolio IRR is computed ONLY when every source has reported
    # its cash movements. A partial set of deposits makes contributions
    # look smaller than they were, which flatters the return — the one
    # direction an error must never quietly go.
    if boundary_flows and net_worth is not None:
        if boundary_complete:
            report.portfolio_xirr = xirr(
                [*boundary_flows, CashFlow(when=as_of, amount=net_worth,
                                           label="net worth")]
            )
        else:
            report.assumptions.append(
                "portfolio IRR not computed: not every source reported its "
                "deposits, and a partial set understates contributions"
            )

    if cash_excluded:
        report.assumptions.append(
            f"terminal value excludes {cash_excluded:,.2f} of account cash — "
            "deposits are not in the activity set (§6.3), so counting it "
            "would credit money no outflow paid for"
        )
    return report


__all__ = [
    "CashFlow",
    "ReturnsReport",
    "build_report",
    "simple_return",
    "xirr",
    "xnpv",
]
