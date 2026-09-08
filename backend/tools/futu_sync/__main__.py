"""Futu -> Ghostfolio sync.

    infra/futu-opend/sync-futu.sh python -m tools.futu_sync --push < token

Run it through `sync-futu.sh`, not directly: OpenD has to be up, and it
must be stopped again afterwards (§4.3 rule 3).

Stdin: the Ghostfolio token. Futu itself needs no secret here — OpenD
already holds the session, and this never calls `unlock_trade`.
"""

from __future__ import annotations

import logging

from app.adapters._common import RetryPolicy
from app.adapters.futu.adapter import SOURCE_NAME, FutuAdapter
from app.adapters.futu.client import FutuOpenDClient
from tools._sync_cli import base_parser, main_for, read_secrets


def main(argv: list[str] | None = None) -> int:
    args = base_parser(
        "python -m tools.futu_sync",
        "Sync Futu holdings, trades and cash into Ghostfolio.",
        "Futu",
    ).parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s %(message)s",
    )
    (token,) = read_secrets(1)
    if not token:
        print("No Ghostfolio token on stdin.")
        return 2
    return main_for(
        args=args,
        token=token,
        source=SOURCE_NAME,
        # One attempt: OpenD runs only for this window, and a retry of a
        # multi-minute walk can outlive it.
        make_adapter=lambda: FutuAdapter(
            FutuOpenDClient(), retry=RetryPolicy(max_attempts=1)
        ),
        hard_exit=True,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
