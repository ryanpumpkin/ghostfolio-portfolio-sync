"""Where you are against target, and where the next contribution goes.

    python -m tools.allocation < token
    python -m tools.allocation --new-money 10000 < token

Stdin: the Ghostfolio token.

§8.4: new money does the work, not selling. Sell-side suggestions appear
only when a band is breached AND new money alone cannot correct it — so
a plan silent about selling is the normal case, not a gap.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from decimal import Decimal

from app.services.allocation import (
    Holding,
    allocate_new_money,
    compute_drift,
    load_classification,
    load_targets,
)
from app.services.ghostfolio.client import GhostfolioClient

#: Ghostfolio returns cash as a holding like any other. Identified by
#: assetSubClass, NOT assetClass: Ghostfolio files cryptocurrency under
#: `assetClass: LIQUIDITY` alongside cash, so testing the outer class
#: swept 6,872 HKD of bitcoin, ethereum and dogecoin into the cash line
#: and left crypto reading 2.7% when it is 9.5%.
#:
#: §8.3 requires cash in the total, so it is summed separately — never
#: dropped, and never counted twice.
_CASH_SUB_CLASSES = {"CASH"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tools.allocation")
    parser.add_argument(
        "--new-money", type=Decimal, default=Decimal(0),
        help="amount to allocate this month, in the base currency",
    )
    parser.add_argument(
        "--minimum-order", type=Decimal, default=Decimal(0),
        help="skip suggestions below this — a tiny order is eaten by commission",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MBP_GF_URL", "http://192.168.0.100:3333"),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, token: str) -> int:
    async with GhostfolioClient(base_url=args.base_url, security_token=token) as gf:
        response = await gf._request("GET", "/api/v1/portfolio/holdings")
        payload = response.json().get("holdings") or []
        rows = list(payload.values()) if isinstance(payload, dict) else list(payload)

    holdings: list[Holding] = []
    cash = Decimal(0)
    for row in rows:
        # The symbol lives under `assetProfile`, not at the top level.
        # Reading `row["symbol"]` returns nothing, sends every holding
        # down the cash branch, and reports the whole portfolio as cash.
        profile = row.get("assetProfile") or {}
        symbol = str(profile.get("symbol") or "").strip()
        value = Decimal(str(row.get("valueInBaseCurrency") or 0))
        if not value:
            continue
        sub_class = str(profile.get("assetSubClass") or "").upper()
        if sub_class in _CASH_SUB_CLASSES or not symbol:
            cash += value
            continue
        holdings.append(Holding(symbol=symbol, value=value))

    report = compute_drift(
        holdings=holdings,
        cash=cash,
        classification=load_classification(),
        targets=load_targets(),
    )

    print(f"\ntotal {report.total_value:,.2f}   (cash {cash:,.2f})\n")
    for item in sorted(report.classes, key=lambda c: -c.value):
        if item.band >= 100:
            note = "  (never rebalanced)"
        elif item.label == "ok":
            note = ""
        else:
            note = f"  <-- {item.label}"
        print(
            f"  {item.asset_class:<12} {item.value:>11,.2f}  "
            f"{item.current_pct:>6.1f}%  target {item.target_pct:>5.1f}%  "
            f"drift {item.drift:>+6.1f}pp{note}"
        )
    if report.unclassified:
        # §8.1: counted in the total, in no class, and named — never
        # silently dropped, which would make every percentage wrong.
        print("\n  UNCLASSIFIED: " + ", ".join(report.unclassified))

    if args.new_money:
        plan = allocate_new_money(
            report, args.new_money, minimum_order=args.minimum_order
        )
        print(f"\nallocating {args.new_money:,.2f}:")
        print(f"  {plan.digest_line()}")
        for dropped in plan.dropped:
            print(
                f"  below minimum order: {dropped.asset_class} "
                f"{dropped.amount:,.2f}"
            )
        for suggestion in plan.sell_suggestions:
            print(
                f"  SELL-SIDE  {suggestion.asset_class} "
                f"{suggestion.drift:+.1f}pp from target"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s %(message)s",
    )
    token = sys.stdin.readline().strip()
    if not token:
        print("No Ghostfolio token on stdin.", file=sys.stderr)
        return 2
    return asyncio.run(_run(args, token))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
