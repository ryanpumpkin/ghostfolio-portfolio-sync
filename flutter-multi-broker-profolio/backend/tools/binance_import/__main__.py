"""CLI for the one-off Binance import (spec §5).

    python -m tools.binance_import --dry-run
    python -m tools.binance_import --push

Credentials arrive on **stdin**, one per line:

    1. Binance API key
    2. Binance API secret
    3. Ghostfolio token   (only needed with --push)

A read-only key (Enable Reading ONLY — trading and withdrawals OFF, §5.2).

Stdin rather than the environment: an env var is visible in `docker
inspect` for the life of the container, and argv is visible in the host
process list. Neither is acceptable for a key that can read an entire
trading history, even a short-lived one. The environment is still
honoured as a fallback so an existing runbook keeps working, but the
piped form is the one to use.

`--dry-run` is the default on purpose. Crawl first, read the summary,
look at the archived raw responses, and only then push.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from tools.binance_import.client import (
    BinanceBannedError,
    BinanceClient,
    BinanceConfig,
    BinanceImportError,
)
from tools.binance_import.run import DEFAULT_START, EarnNotEmptyError, run_import


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.binance_import",
        description="One-off Binance historical import (spec §5). Run once, "
                    "verify, then revoke the API key.",
    )
    parser.add_argument(
        "--push", action="store_true",
        help="push to Ghostfolio after crawling (default: crawl only)",
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("BINANCE_BASE_URL"),
        help="override the API host — some regions need a different one (§5.3)",
    )
    parser.add_argument(
        "--start", default=None,
        help=f"ISO date to crawl from (default {DEFAULT_START.date()}, "
             "Binance's launch)",
    )
    parser.add_argument(
        "--raw-dir", default="data/binance_raw", type=Path,
        help="where raw responses are archived before normalising (§5.5)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s %(message)s",
    )

    # Stdin first, environment only as a fallback (see module docstring).
    piped: list[str] = []
    if not sys.stdin.isatty():
        piped = [sys.stdin.readline().strip() for _ in range(3)]
    api_key = (piped[0] if piped else "") or os.environ.get("BINANCE_API_KEY")
    api_secret = (piped[1] if piped else "") or os.environ.get("BINANCE_API_SECRET")
    gf_token = (piped[2] if piped else "") or os.environ.get("GHOSTFOLIO_TOKEN")
    if not api_key or not api_secret:
        print(
            "No Binance credentials. Pipe them in, one per line:\n"
            "  key, then secret, then the Ghostfolio token.\n"
            "Use a READ-ONLY key: Enable Reading only, trading and "
            "withdrawals OFF (§5.2).",
            file=sys.stderr,
        )
        return 2

    config = BinanceConfig(api_key=api_key, api_secret=api_secret, raw_dir=args.raw_dir)
    if args.base_url:
        config.base_url = args.base_url

    start = (
        datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        if args.start
        else DEFAULT_START
    )

    try:
        with BinanceClient(config) as client:
            records = run_import(client, start=start)
    except EarnNotEmptyError as exc:
        print(f"\nSTOPPED: {exc}", file=sys.stderr)
        return 3
    except BinanceBannedError as exc:
        print(f"\nSTOPPED: {exc}", file=sys.stderr)
        return 4
    except BinanceImportError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1

    by_type = Counter(r.type.value if r.type else "unknown" for r in records)
    print(f"\n{len(records)} record(s) normalised")
    for kind, count in sorted(by_type.items()):
        print(f"  {kind:<12} {count}")
    print(f"\nRaw responses archived to {config.raw_dir}/")

    unconverted = [
        r for r in records
        if r.fee is not None and r.fee_currency and r.currency
        and r.fee_currency != r.currency
    ]
    if unconverted:
        # §5.6 — a fee left in its original asset is a known gap, not a
        # silent one. Surface it before anyone trusts the cost basis.
        print(
            f"\nWARNING: {len(unconverted)} fee(s) could not be converted to "
            "the trade currency and are recorded in their original asset. "
            "Supply a price lookup to resolve them (§5.6)."
        )

    if not args.push:
        print("\nDry run — nothing pushed. Re-run with --push when satisfied.")
        return 0

    return _push(
        records,
        ledger_path=args.raw_dir.parent / "binance_sync.db",
        token=gf_token,
    )


def _push(records: list, *, ledger_path: Path, token: str | None = None) -> int:
    """Push to Ghostfolio, idempotently (§3.3).

    Account ids are looked up by name rather than hardcoded, so this keeps
    working if the accounts are recreated.
    """
    import asyncio

    from app.services.ghostfolio.client import GhostfolioClient
    from app.services.ghostfolio.config import load_crypto_overrides
    from app.services.ghostfolio.ledger import SyncLedger
    from app.services.ghostfolio.sync import GhostfolioSync
    from app.services.own_accounts import OwnAccountsRegistry

    base_url = os.environ.get("GHOSTFOLIO_URL", "http://192.168.0.100:3333")
    if not token:
        print(
            "No Ghostfolio token. Pass it as the third stdin line to push.",
            file=sys.stderr,
        )
        return 2

    async def _run() -> int:
        async with GhostfolioClient(base_url=base_url, security_token=token) as gf:
            accounts = {
                str(a.get("name")): str(a.get("id")) for a in await gf.list_accounts()
            }
            account_id = accounts.get("Binance")
            if account_id is None:
                print(
                    "No Ghostfolio account named 'Binance'. Create one first "
                    "(§7.1: one account per source).",
                    file=sys.stderr,
                )
                return 2

            ledger = SyncLedger(ledger_path)
            try:
                sync = GhostfolioSync(
                    client=gf,
                    ledger=ledger,
                    account_id_by_source={"binance": account_id},
                    crypto_overrides=load_crypto_overrides(),
                    # Promotes withdrawals to the owner's own wallets into
                    # TRANSFERs, which are then excluded from the push (§6.3).
                    own_accounts=OwnAccountsRegistry.load(),
                )
                report = await sync.push(records)
            finally:
                ledger.close()

        print(f"\n{report.summary()}")
        for skip in report.skipped[:20]:
            print(f"  skipped {skip.external_id}: {skip.reason.value} {skip.detail}")
        if unresolved := sync.unresolved_symbols(report):
            print(
                "\nUnresolved symbols — these holdings will NOT appear in "
                "Ghostfolio until verified (§7.1):",
                file=sys.stderr,
            )
            for detail in unresolved[:10]:
                print(f"  {detail}", file=sys.stderr)
        return 0 if report.ok else 1

    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
