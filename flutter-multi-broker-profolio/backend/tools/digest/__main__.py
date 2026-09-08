"""Compose and send the monthly digest (§10).

    python -m tools.digest --send < token
    python -m tools.digest < token          # print it, send nothing

Stdin: the Ghostfolio token. Mail credentials come from MBP_GMAIL_*.

§10: "The primary user interface after this rebuild is a text message,
not a dashboard." So this arrives on a schedule; there is nothing to
open and nothing to remember.

Reconciliation comes from what the SYNCS found, not from a live check —
this fires when Futu's OpenD is not even running. Their age is reported
alongside, because a clean result from three weeks ago is a different
claim from a clean result this morning.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.allocation import (
    Holding,
    allocate_new_money,
    compute_drift,
    load_classification,
    load_targets,
)
from app.services.digest import compose_digest, send_digest
from app.services.ghostfolio.client import GhostfolioClient
from app.services.health_store import HealthStore
from app.services.reconciliation import (
    AssetKind,
    ReconcileItem,
    ReconcileReport,
    ReconcileStatus,
)

_CASH_SUB_CLASSES = {"CASH"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tools.digest")
    parser.add_argument("--send", action="store_true",
                        help="email it (default: print and send nothing)")
    parser.add_argument("--new-money", type=Decimal, default=Decimal(0))
    parser.add_argument("--health-store", default="/data/sync_health.json")
    parser.add_argument(
        "--no-bank-prompt", action="store_true",
        help="drop the §9.1 bank-cash reminder — the owner has decided "
             "that balance stays outside the portfolio",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MBP_GF_URL", "http://192.168.0.100:3333"),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def _reconciliation(store: HealthStore) -> ReconcileReport:
    """Turn the syncs' findings into the shape §10 renders.

    A source that reported nothing is NOT rendered as clean. It is
    absent, and the caller says so separately — the digest must never
    claim a check it did not make.

    SURPLUS becomes NO_POSITION: activities imply a holding the source
    does not report, which is a duplicate or a lost disposal, never a
    history gap.
    """
    report = ReconcileReport()
    for findings in store.all():
        for line in findings.surplus:
            report.items.append(ReconcileItem(
                symbol=line.split(":")[0].strip() or findings.source,
                custody=findings.source,
                authoritative=Decimal(0), derived=Decimal(0),
                status=ReconcileStatus.NO_POSITION, kind=AssetKind.EQUITY,
            ))
        for line in findings.no_cost:
            report.items.append(ReconcileItem(
                symbol=line.split(":")[0].strip() or findings.source,
                custody=findings.source,
                authoritative=Decimal(0), derived=Decimal(0),
                status=ReconcileStatus.NO_HISTORY, kind=AssetKind.EQUITY,
            ))
    return report


async def _run(args: argparse.Namespace, token: str) -> int:
    async with GhostfolioClient(base_url=args.base_url, security_token=token) as gf:
        rows = (await gf._request(
            "GET", "/api/v1/portfolio/holdings")).json().get("holdings") or []
        perf = (await gf._request(
            "GET", "/api/v2/portfolio/performance?range=max")).json()
    net_worth = Decimal(str(perf["performance"]["currentNetWorth"]))

    # A month ago BY DATE. The chart is not daily — 449 points over two
    # years — so `chart[-31]` is about 50 days back and reported a 28.1%
    # "month-on-month" change that covered nearly two.
    chart = perf.get("chart") or []
    previous = None
    cutoff = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()
    invested_then = None
    for point in reversed(chart):
        stamp = str(point.get("date") or "")[:10]
        if stamp and stamp <= cutoff:
            raw = point.get("netWorth") or point.get("value")
            if raw:
                previous = Decimal(str(raw))
            if point.get("totalInvestment"):
                invested_then = Decimal(str(point["totalInvestment"]))
            break
    invested_now = Decimal(
        str(perf["performance"].get("totalInvestment") or 0)
    )

    holdings: list[Holding] = []
    cash = Decimal(0)
    for row in rows:
        profile = row.get("assetProfile") or {}
        symbol = str(profile.get("symbol") or "").strip()
        value = Decimal(str(row.get("valueInBaseCurrency") or 0))
        if not value:
            continue
        sub = str(profile.get("assetSubClass") or "").upper()
        if sub in _CASH_SUB_CLASSES or not symbol:
            cash += value
            continue
        holdings.append(Holding(symbol=symbol, value=value))

    drift = compute_drift(
        holdings=holdings, cash=cash,
        classification=load_classification(), targets=load_targets(),
    )
    plan = allocate_new_money(drift, args.new_money) if args.new_money else None

    store = HealthStore(args.health_store)
    stalest = store.stalest()
    text = compose_digest(
        drift=drift,
        reconciliation=_reconciliation(store),
        net_worth=net_worth,
        previous_net_worth=previous,
        allocation=plan,
        reconciliation_checked=stalest is not None,
        prompt_bank_cash=not args.no_bank_prompt,
    )
    if stalest is not None:
        # Age belongs with the verdict: clean three weeks ago is not the
        # same claim as clean this morning.
        age = (datetime.now(UTC) - stalest).days
        text = text.replace(
            "Reconciliation: clean",
            f"Reconciliation: clean (last checked {age} day(s) ago)",
        )

    # A month-on-month figure reads as performance. It is not, when the
    # tracker gained assets it previously did not know about — importing
    # the Ledger coins and Futu's bitcoin moved net worth 15,087 in one
    # day, and calling that a 28% month would be a lie of framing.
    if invested_then is not None and invested_now:
        added = invested_now - invested_then
        if added.copy_abs() > invested_then / 20:
            text += (
                f"\n\nNOTE: invested capital changed by "
                f"{added:+,.0f} over this window, so the month-on-month "
                f"figure above is not all performance."
            )

    print(text)
    if not args.send:
        print("\n--- not sent (pass --send) ---")
        return 0

    to_email = os.environ.get("MBP_GMAIL_DIGEST_RECIPIENT", "").strip()
    from_email = os.environ.get("MBP_GMAIL_FROM_EMAIL", "").strip()
    password = os.environ.get("MBP_GMAIL_APP_PASSWORD", "").strip()
    if not (to_email and from_email and password):
        print("MBP_GMAIL_* not set; nothing sent.", file=sys.stderr)
        return 2
    send_digest(text, to_email=to_email, from_email=from_email,
                app_password=password)
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
