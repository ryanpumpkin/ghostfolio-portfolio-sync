"""LongBridge -> Ghostfolio sync.

    python -m tools.longbridge_sync --push < token

Stdin: the Ghostfolio token.

LongBridge's own credentials come from `MBP_LB_ANALYST_*`, which is
where the rest of the app already reads them. They are **trade-capable**
— the OpenAPI access token can place orders — so this tool calls only
read methods, and that token belongs in the encrypted store rather than
a file on disk.
"""

from __future__ import annotations

import logging
import os
import sys

from app.adapters.longbridge.adapter import SOURCE_NAME, LongBridgeAdapter
from app.adapters.longbridge.client import LongbridgeClient
from tools._sync_cli import base_parser, main_for, read_secrets


def main(argv: list[str] | None = None) -> int:
    args = base_parser(
        "python -m tools.longbridge_sync",
        "Sync LongBridge holdings, trades and cash into Ghostfolio.",
        "LongBridge",
    ).parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s %(message)s",
    )
    (token,) = read_secrets(1)
    if not token:
        print("No Ghostfolio token on stdin.", file=sys.stderr)
        return 2

    creds = {
        name: os.environ.get(name, "").strip()
        for name in (
            "MBP_LB_ANALYST_APP_KEY",
            "MBP_LB_ANALYST_APP_SECRET",
            "MBP_LB_ANALYST_ACCESS_TOKEN",
        )
    }
    if missing := [name for name, value in creds.items() if not value]:
        print(f"LongBridge credentials missing: {', '.join(missing)}", file=sys.stderr)
        return 2

    return main_for(
        args=args,
        token=token,
        source=SOURCE_NAME,
        make_adapter=lambda: LongBridgeAdapter(
            LongbridgeClient(
                app_key=creds["MBP_LB_ANALYST_APP_KEY"],
                app_secret=creds["MBP_LB_ANALYST_APP_SECRET"],
                access_token=creds["MBP_LB_ANALYST_ACCESS_TOKEN"],
            )
        ),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
