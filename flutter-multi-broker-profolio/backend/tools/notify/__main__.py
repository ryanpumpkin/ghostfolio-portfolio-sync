"""Email a failure alert. Reads the job output on stdin.

    ... | python -m tools.notify --job sync-all --failures 2

Credentials come from the environment (MBP_GMAIL_*), which is where the
rest of the app already reads them and where the compose file already
supplies them. Exits 0 even when it cannot send: an alert that fails to
deliver must not also change the exit code of the job it is reporting
on, or a sync failure becomes a mail failure and the real cause is lost.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys

from app.services.notify import compose_failure, send_alert


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.notify")
    parser.add_argument("--job", required=True)
    parser.add_argument("--failures", type=int, required=True)
    parser.add_argument("--host", default=socket.gethostname())
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if args.failures <= 0:
        # Silence is the signal that things are fine.
        return 0

    to_email = os.environ.get("MBP_GMAIL_DIGEST_RECIPIENT", "").strip()
    from_email = os.environ.get("MBP_GMAIL_FROM_EMAIL", "").strip()
    password = os.environ.get("MBP_GMAIL_APP_PASSWORD", "").strip()
    if not (to_email and from_email and password):
        print(
            "cannot send alert: MBP_GMAIL_FROM_EMAIL, MBP_GMAIL_APP_PASSWORD "
            "and MBP_GMAIL_DIGEST_RECIPIENT must all be set",
            file=sys.stderr,
        )
        return 0

    subject, body = compose_failure(
        job=args.job,
        failures=args.failures,
        detail=sys.stdin.read(),
        host=args.host,
    )
    try:
        send_alert(
            subject, body, to_email=to_email,
            from_email=from_email, app_password=password,
        )
    except Exception as exc:  # noqa: BLE001 — see module docstring
        print(f"alert could not be sent: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
