"""Shared CLI plumbing for the per-source sync tools.

Secrets arrive on **stdin**, one per line, never on argv and never in
the environment. Argv is visible in the host process list and an env var
is visible in `docker inspect` for the life of the container; stdin is
neither, and it is how every job in this repo receives a secret.

Line 1 is always the Ghostfolio token. A source that needs a credential
of its own reads it from the following lines.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from app.services.dependencies import get_fx_service
from app.services.ghostfolio.client import GhostfolioClient
from app.services.ghostfolio.ledger import SyncLedger
from app.services.cashflows import CashFlowStore
from app.services.health_store import HealthStore
from app.services.ghostfolio.reconcile import reconcile_source

DEFAULT_GF_URL = "http://192.168.0.100:3333"


def base_parser(prog: str, description: str, account: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "--push", action="store_true",
        help="write to Ghostfolio (default: read and report only)",
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("MBP_GF_URL", DEFAULT_GF_URL),
        help="Ghostfolio base URL",
    )
    parser.add_argument("--account", default=account, help="Ghostfolio account name")
    parser.add_argument(
        "--ledger", type=Path, default=Path("/data/ghostfolio_sync.db"),
        help="idempotency ledger (§3.3)",
    )
    parser.add_argument(
        "--cash-store", type=Path, default=Path("/data/cash_flows.json"),
        help="where deposits/withdrawals are recorded for the "
             "money-weighted return — they are never pushed (§6.3)",
    )
    parser.add_argument(
        "--health-store", type=Path, default=Path("/data/sync_health.json"),
        help="where this run's reconciliation findings are written, so the "
             "monthly digest can report them instead of assuming them",
    )
    parser.add_argument(
        "--cash-days", type=int, default=7,
        help="how many days of cash movements to ask for. Futu bills one "
             "throttled call PER DAY, so a daily sync wants a handful and "
             "a one-off backfill wants years (e.g. 1100). The store "
             "merges, so a cheap run never erases an expensive one.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def read_secrets(count: int = 1) -> list[str]:
    """Read `count` secrets from stdin, one per line."""
    return [sys.stdin.readline().strip() for _ in range(count)]


async def run_sync(
    args: argparse.Namespace,
    *,
    token: str,
    source: str,
    make_adapter: Any,
) -> int:
    """Look the account up by name, reconcile, print what happened."""
    ledger = SyncLedger(args.ledger)
    try:
        async with GhostfolioClient(
            base_url=args.base_url, security_token=token
        ) as gf:
            accounts = {
                str(a.get("name")): str(a.get("id")) for a in await gf.list_accounts()
            }
            account_id = accounts.get(args.account)
            if account_id is None:
                print(
                    f"No Ghostfolio account named {args.account!r}. Create it "
                    "first (§7.1: one account per source).",
                    file=sys.stderr,
                )
                return 2
            adapter = make_adapter()
            try:
                outcome = await reconcile_source(
                    client=gf,
                    ledger=ledger,
                    adapter=adapter,
                    source=source,
                    account_id=account_id,
                    account_name=args.account,
                    fx=get_fx_service(),
                    cash_store=CashFlowStore(args.cash_store),
                    cash_days=args.cash_days,
                    health_store=HealthStore(args.health_store),
                    dry_run=not args.push,
                )
            finally:
                closer = getattr(adapter, "aclose", None)
                if callable(closer):
                    await closer()
    finally:
        ledger.close()

    print(f"\n{outcome.summary()}")
    for line in outcome.report_lines():
        print(line)
    if not args.push:
        print("\nDry run — nothing written. Re-run with --push when satisfied.")
    return 0 if outcome.ok else 1


def main_for(
    *,
    args: argparse.Namespace,
    token: str,
    source: str,
    make_adapter: Any,
    hard_exit: bool = False,
) -> int:
    code = asyncio.run(
        run_sync(args, token=token, source=source, make_adapter=make_adapter)
    )
    sys.stdout.flush()
    sys.stderr.flush()
    if hard_exit:
        # The futu SDK starts non-daemon threads, so a normal return never
        # reaches process exit — the job hangs and OpenD stays logged in
        # past its window, which is what §4.3 rule 3 exists to prevent.
        os._exit(code)
    return code


__all__ = ["base_parser", "main_for", "read_secrets", "run_sync"]
