"""CLI for the Futu -> Ghostfolio sync.

    infra/futu-opend/sync-futu.sh python -m tools.futu_sync --push < token

Run it through `sync-futu.sh`, not directly: OpenD has to be up, and it
must be stopped again afterwards (§4.3 rule 3).

The Ghostfolio token is read from **stdin**, never from argv or the
environment. Argv is visible in the host process list and an env var is
visible in `docker inspect`; stdin is neither, and `sync-futu.sh`
deliberately reserves its own stdin for exactly this.

`--dry-run` is the default. It reads Futu and reports what it would do
without writing anything to Ghostfolio.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from app.services.dependencies import get_fx_service
from app.services.ghostfolio.client import GhostfolioClient
from app.services.ghostfolio.ledger import SyncLedger
from tools.futu_sync.run import sync_futu


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.futu_sync",
        description="Sync Futu holdings, trades and cash into Ghostfolio.",
    )
    parser.add_argument(
        "--push", action="store_true",
        help="write to Ghostfolio (default: read and report only)",
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("MBP_GF_URL", "http://192.168.0.100:3333"),
        help="Ghostfolio base URL",
    )
    parser.add_argument("--account", default="Futu", help="Ghostfolio account name")
    parser.add_argument(
        "--ledger", type=Path, default=Path("/data/ghostfolio_sync.db"),
        help="idempotency ledger (§3.3)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, token: str) -> int:
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

            outcome = await sync_futu(
                client=gf,
                ledger=ledger,
                account_id=account_id,
                account_name=args.account,
                fx=get_fx_service(),
                dry_run=not args.push,
            )
    finally:
        ledger.close()

    print(f"\n{outcome.summary()}")
    for line in outcome.cash:
        print(f"  cash     {line}")
    for line in outcome.skipped:
        print(f"  SKIPPED  {line}")
    for line in outcome.basis:
        print(f"  BASIS    {line}")
    if outcome.retracted:
        print(f"  retracted {outcome.retracted} stale opening balance(s)")
    for line in outcome.opening:
        print(f"  OPENING  {line}")
    for line in outcome.surplus:
        # Activities imply MORE than is held. Never booked away — it is a
        # duplicate or a missed disposal, and papering over it hides both.
        print(f"  SURPLUS  {line}   <-- investigate")
    for line in outcome.no_cost:
        print(f"  NO COST  {line}")
    if not args.push:
        print("\nDry run — nothing written. Re-run with --push when satisfied.")
    return 0 if outcome.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s %(message)s",
    )
    token = sys.stdin.readline().strip()
    if not token:
        print(
            "No Ghostfolio token on stdin. Pipe it in:\n"
            "  ... python -m tools.futu_sync --push < /path/to/token",
            file=sys.stderr,
        )
        return 2
    code = asyncio.run(_run(args, token))
    sys.stdout.flush()
    sys.stderr.flush()
    # The futu SDK starts non-daemon threads, so a normal return never
    # reaches process exit — the job hangs and OpenD stays logged in past
    # its window, which is what rule 3 exists to prevent.
    os._exit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
