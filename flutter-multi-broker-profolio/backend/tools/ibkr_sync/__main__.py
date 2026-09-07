"""IBKR -> Ghostfolio sync, over Flex Web Service.

    python -m tools.ibkr_sync --push < secrets

Stdin, one per line:
    1. the Ghostfolio token
    2. the IBKR Flex web-service token

The Flex token is read-only by construction: it cannot place an order,
move cash, or log in to Account Management, which is why this path is
strongly preferred over IB Gateway (§4.1). Gateway would need the
account password and a daily second factor.

`--backfill` walks calendar-year windows to recover history older than
the query's own period. Use it sparingly. Flex counts failed attempts
per ACCOUNT and answers 1025 once there have been too many — a lockout
that took roughly a day to clear. The default run asks for the query's
period in a single request, which is one attempt.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from app.adapters.ibkr.flex import (
    SOURCE_NAME,
    FlexConfig,
    FlexWebServiceClient,
    HttpxFlexTransport,
    IbkrFlexAdapter,
)
from tools._sync_cli import base_parser, main_for, read_secrets


def main(argv: list[str] | None = None) -> int:
    parser = base_parser(
        "python -m tools.ibkr_sync",
        "Sync IBKR holdings, trades and cash into Ghostfolio via Flex.",
        "IBKR",
    )
    parser.add_argument(
        "--query-id", default="1629433",
        help="Flex query id (the report definition, not a secret)",
    )
    parser.add_argument(
        "--backfill-from", metavar="YYYY-MM-DD", default=None,
        help="walk calendar-year windows back to this date to recover "
             "history older than the query's own period. Each window is a "
             "SEPARATE request, and Flex locks the ACCOUNT out (1025) once "
             "there have been too many failures — a lockout that took "
             "about a day to clear. Use only when history is genuinely "
             "missing; the default run is a single request.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s %(message)s",
    )

    gf_token, flex_token = read_secrets(2)
    if not gf_token or not flex_token:
        print(
            "Two secrets expected on stdin: the Ghostfolio token, then the "
            "IBKR Flex token.",
            file=sys.stderr,
        )
        return 2

    config = FlexConfig(token=flex_token, query_id=args.query_id)
    transport = HttpxFlexTransport(timeout=config.request_timeout)
    history_start = (
        datetime.strptime(args.backfill_from, "%Y-%m-%d").date()
        if args.backfill_from
        else None
    )

    def make_adapter() -> IbkrFlexAdapter:
        return IbkrFlexAdapter(
            FlexWebServiceClient(config=config, transport=transport),
            history_start=history_start,
        )

    # The transport is closed by the adapter's `aclose`, inside the same
    # event loop that opened it (see IbkrFlexAdapter.aclose).
    return main_for(
        args=args, token=gf_token, source=SOURCE_NAME, make_adapter=make_adapter
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
