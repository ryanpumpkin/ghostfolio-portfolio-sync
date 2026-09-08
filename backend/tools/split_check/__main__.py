"""CLI: portfolio data integrity against Ghostfolio (§3.3, §6.4).

Two checks that turn out to be entangled: activities pushed twice, and
activities whose prices disagree with the provider because of a share
split. A duplicated SELL made TQQQ's replayed quantity go negative,
which made the opening-balance pass invent a BUY to cover it — so
duplicates are removed first and the restatement runs on what is left.

    python -m tools.split_check                # report only
    python -m tools.split_check --repair       # remove and restate

    GHOSTFOLIO_URL, GHOSTFOLIO_TOKEN

Reporting is the default because the repair rewrites activities. The
restatement itself is value-preserving — `quantity / factor` and
`price * factor` leave the money exactly as it was, so cost basis,
proceeds and realised P&L do not move — but it is still a rewrite of
records that came from a broker, and it should be looked at first.

Worth re-running periodically, not once. The provider restates its
history at every split, so an activity that agreed with it last year can
disagree today without anything on our side changing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from app.services.ghostfolio.client import GhostfolioClient
from tools.split_check.run import (
    find,
    remove_duplicates,
    repair,
    retract_derived,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.split_check",
        description="Find activities Ghostfolio holds twice, and activities "
                    "whose price disagrees with the price provider because of "
                    "a share split. Optionally repair both.",
    )
    parser.add_argument(
        "--repair", action="store_true",
        help="remove duplicated activities, then restate the activities of "
             "symbols where every activity sits on the same side of the same "
             "split (default: report only)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s %(message)s",
    )

    base_url = os.environ.get("GHOSTFOLIO_URL", "http://192.168.0.100:3333")
    # Stdin first, environment only as a fallback. An env var is visible
    # in `docker inspect` for the life of the container; stdin is not,
    # and it is how every other job in this repo receives a secret.
    token = ""
    if not sys.stdin.isatty():
        token = sys.stdin.readline().strip()
    token = token or os.environ.get("GHOSTFOLIO_TOKEN", "")
    if not base_url or not token:
        print(
            "No Ghostfolio token. Pipe it in:\n"
            "  ... python -m tools.split_check < /path/to/token",
            file=sys.stderr,
        )
        return 2

    async def _run() -> int:
        async with GhostfolioClient(base_url=base_url, security_token=token) as gf:
            findings, duplicates, activities = await find(gf)

            print(f"{len(duplicates)} duplicated external id(s)")
            for duplicate in duplicates:
                print(f"  {duplicate.describe()}")

            healthy = [f for f in findings if f.healthy]
            split = [f for f in findings if f.repairable]
            blocked = [f for f in findings if f.straddles or f.incomplete]

            print(f"{len(findings)} symbol(s) checked, {len(healthy)} agree")
            for finding in split + blocked:
                print(f"  {finding.describe()}")
                for row in finding.rows:
                    if row.factor is None:
                        continue
                    print(f"      {row.date}  {row.quantity} @ {row.our_price} "
                          f"-> provider {row.provider_price}  ({row.comment})")

            if blocked:
                print(
                    "NOT repaired automatically — restating these would invent "
                    "a position rather than correct one. See the line against "
                    "each symbol.",
                    file=sys.stderr,
                )

            if not args.repair:
                print()
                print(
                    "Report only — re-run with --repair to act."
                    if duplicates or split
                    else "Nothing to do."
                )
                return 1 if blocked else 0

            if duplicates:
                # Removed before the restatement: a duplicate that
                # survived into it would come back looking tidy and still
                # be duplicated.
                removed, touched = await remove_duplicates(gf, duplicates)
                print()
                print(f"removed {removed} duplicate activity/activities")
                dropped = await retract_derived(gf, activities, touched)
                if dropped:
                    print(f"retracted {dropped} now-stale opening balance(s) "
                          f"for {', '.join(sorted(touched))}")
                findings, _, activities = await find(gf)
                blocked = [f for f in findings if f.straddles or f.incomplete]

            count = await repair(gf, findings, activities)
            print(f"restated {count} activity/activities")
            return 1 if blocked else 0

    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
