"""Report time-weighted, money-weighted and simple returns.

    python -m tools.returns < token

Stdin: the Ghostfolio token.

Each activity is converted at the rate published for ITS OWN date, not
today's — a cost basis is a historical fact, and repricing a 2024 trade
at a 2026 rate is a quiet way to move the answer. Rates that could not
be sourced historically are named in the assumptions.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from datetime import datetime
from decimal import Decimal

from app.services.dependencies import get_fx_service
from app.services.fx import FxRateUnavailableError
from app.services.ghostfolio.client import GhostfolioClient
from app.services.cashflows import CashFlowStore
from app.services.returns import CashFlow, build_report

_INFLOW = {"SELL", "DIVIDEND"}
_OUTFLOW = {"BUY", "FEE"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.returns",
        description="Time-weighted, money-weighted and simple returns.",
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("MBP_GF_URL", "http://192.168.0.100:3333"),
    )
    parser.add_argument(
        "--current-fx", action="store_true",
        help="convert every activity at today's rate instead of its own "
             "date's (faster, and enough when the pair is pegged)",
    )
    parser.add_argument(
        "--cash-store", type=Path, default=Path("/data/cash_flows.json"),
        help="deposits/withdrawals recorded by the syncs — needed for the "
             "portfolio IRR, which trades alone cannot produce",
    )
    parser.add_argument(
        "--sources", default="futu,ibkr,longbridge",
        help="sources that must ALL have reported cash movements before "
             "the portfolio IRR is trusted",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, token: str) -> int:
    fx = get_fx_service()
    assumptions: list[str] = []
    approximated: set[str] = set()
    unconvertible: set[str] = set()
    proxied: set[str] = set()

    async with GhostfolioClient(base_url=args.base_url, security_token=token) as gf:
        accounts = {str(a["id"]): a for a in await gf.list_accounts()}
        perf = (await gf._request(
            "GET", "/api/v2/portfolio/performance?range=max")).json()["performance"]
        base = "HKD"
        worth = Decimal(str(perf["currentNetWorth"]))
        invested = Decimal(str(perf["totalInvestment"]))
        profit = Decimal(str(perf["netPerformanceWithCurrencyEffect"]))
        twr = float(perf["netPerformancePercentageWithCurrencyEffect"])

        async def to_base(amount: Decimal, currency: str, when) -> Decimal | None:
            """Convert, or return None when no rate can be had.

            None rather than a raise: one unconvertible movement used to
            end the whole report, which is the wrong trade — the other
            figures are still worth having, and the missing one is
            recorded as an assumption instead.
            """
            code = (currency or base).upper()
            if code == base:
                return amount
            try:
                if args.current_fx:
                    rate = await fx.get_rate(code, base)
                    approximated.add(code)
                else:
                    rate = await fx.get_rate_on(code, base, when)
                    if rate.as_of.date() > when:
                        approximated.add(code)
            except FxRateUnavailableError:
                unconvertible.add(code)
                return None
            if getattr(rate, "proxied", False):
                proxied.add(code)
            return amount * rate.rate

        # Account cash: excluded from the terminal value, see returns.py.
        cash = Decimal("0")
        for account in accounts.values():
            balance = Decimal(str(account.get("balance") or 0))
            if balance:
                converted = await to_base(
                    balance, str(account.get("currency") or base), datetime.now().date()
                )
                cash += converted if converted is not None else Decimal("0")

        flows: list[CashFlow] = []
        for a in await gf.list_activities():
            kind = str(a.get("type"))
            if kind not in _INFLOW and kind not in _OUTFLOW:
                continue
            when = datetime.fromisoformat(
                str(a["date"]).replace("Z", "+00:00")
            ).date()
            gross = Decimal(str(a.get("quantity") or 0)) * Decimal(
                str(a.get("unitPrice") or 0)
            )
            fee = Decimal(str(a.get("fee") or 0))
            native = gross - fee if kind in _INFLOW else -(gross + fee)
            if kind == "FEE":
                native = -fee if fee else -gross
            converted = await to_base(native, str(a.get("currency") or base), when)
            if converted is None:
                continue
            flows.append(
                CashFlow(
                    when=when,
                    amount=converted,
                    label=f"{kind} {a.get('comment') or ''}".strip(),
                )
            )

    if unconvertible:
        assumptions.append(
            "no FX rate available, amounts EXCLUDED: " + ", ".join(sorted(unconvertible))
        )
    if proxied:
        assumptions.append(
            "priced via a documented proxy currency (CNH as CNY): "
            + ", ".join(sorted(proxied))
        )
    if approximated:
        assumptions.append(
            "converted at TODAY's rate (no historical rate available): "
            + ", ".join(sorted(approximated))
        )
    assumptions.append(
        "flows are trades, not portfolio deposits/withdrawals — this is the "
        "return on capital deployed into positions, not a textbook portfolio IRR"
    )

    # Portfolio-boundary flows. A deposit is money leaving your pocket,
    # so it is negative; a withdrawal returns it. Internal transfers
    # between accounts you own are excluded by the store.
    boundary: list[CashFlow] = []
    reported: set[str] = set()
    missing_boundary: set[str] = set()
    store = CashFlowStore(args.cash_store)
    for movement in store.external():
        reported.add(movement.source)
        amount = await to_base(movement.amount, movement.currency, movement.when)
        if amount is None:
            # A contribution we cannot value must not be silently
            # dropped: leaving it out understates what was paid in and
            # flatters the return.
            missing_boundary.add(f"{movement.currency} {movement.amount}")
            continue
        boundary.append(
            CashFlow(
                when=movement.when,
                amount=-amount if movement.kind == "deposit" else abs(amount),
                label=f"{movement.kind} {movement.source}",
            )
        )
    required = {s.strip() for s in args.sources.split(",") if s.strip()}
    complete = bool(required) and required <= reported and not missing_boundary
    if missing_boundary:
        assumptions.append(
            "boundary movements that could not be valued: "
            + ", ".join(sorted(missing_boundary))
        )
    if not complete:
        assumptions.append(
            "sources with no cash movements recorded yet: "
            + (", ".join(sorted(required - reported)) or "none")
        )

    report = build_report(
        flows=flows,
        terminal_value=worth - cash,
        cash_excluded=cash,
        twr=twr,
        invested_today=invested,
        profit_today=profit,
        boundary_flows=boundary,
        net_worth=worth,
        boundary_complete=complete,
        assumptions=assumptions,
    )
    print()
    for line in report.describe():
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s %(message)s",
    )
    token = sys.stdin.readline().strip()
    if not token:
        print("No Ghostfolio token on stdin.", file=sys.stderr)
        return 2
    return asyncio.run(_run(args, token))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
