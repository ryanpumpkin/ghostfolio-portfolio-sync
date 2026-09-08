"""Alert the owner when an unattended job fails (spec §9, §10).

Why this is separate from the digest
------------------------------------
The digest is scheduled, expected, and arrives whether or not anything
is wrong. An alert is the opposite: it exists to interrupt, and it earns
that right only by being rare.

So this sends on FAILURE ONLY. A daily "sync ok" mail would be read for
a week, filtered for a month, and then invisible — at which point the
one message that mattered would be invisible too. Silence is the signal
that things are fine.

What it does not solve
----------------------
This cannot report that the job never ran at all. A machine that is off,
a crontab DSM has rewritten, or a script that dies before reaching its
own error handling all produce the same thing: nothing. Detecting that
needs a watcher somewhere else — a dead-man's switch — and pretending
otherwise would be worse than the gap itself.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.text import MIMEText

_LOG = logging.getLogger("mbp.notify")

#: Keep the mail small. A failure alert is read on a phone, and the log
#: it quotes can run to thousands of lines.
MAX_BODY_CHARS = 4000


def compose_failure(
    *, job: str, failures: int, detail: str, host: str = ""
) -> tuple[str, str]:
    """Subject and body for a failed run.

    The subject carries the whole message — which job, how many sources
    — because that is all a lock screen shows, and a subject reading
    only "sync failed" makes you open your laptop to learn anything.
    """
    where = f" on {host}" if host else ""
    subject = f"[mbp] {job}: {failures} source(s) failed{where}"
    trimmed = detail.strip()
    if len(trimmed) > MAX_BODY_CHARS:
        # Keep the TAIL: errors and the summary land at the end, while
        # the head is the same startup chatter every run produces.
        trimmed = "...(earlier output trimmed)...\n" + trimmed[-MAX_BODY_CHARS:]
    body = (
        f"{job} reported {failures} failed source(s).\n\n"
        f"The portfolio in Ghostfolio may now be stale. Nothing was\n"
        f"corrupted — each source is idempotent and a failed one simply\n"
        f"did not update.\n\n"
        f"--- output ---\n{trimmed}\n"
    )
    return subject, body


def send_alert(
    subject: str,
    body: str,
    *,
    to_email: str,
    from_email: str,
    app_password: str,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 465,
) -> None:
    """Send one plain-text alert."""
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = from_email
    message["To"] = to_email

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
        server.login(from_email, app_password)
        server.sendmail(from_email, to_email, message.as_string())
    # The body quotes a job log, which can name accounts and balances.
    _LOG.info("alert sent to %s (%d chars)", to_email, len(body))


__all__ = ["MAX_BODY_CHARS", "compose_failure", "send_alert"]
