"""Monthly portfolio digest (spec §10).

§10 is blunt about what this is for:

    "The primary user interface after this rebuild is **a text message,
     not a dashboard**. A dashboard requires the owner to remember to
     open it; a monthly message arrives."

So the output is plain text, and it stays plain text. No HTML, no
attachments, no links you have to follow to learn anything. It should be
readable on a phone lock screen without opening anything.

Composition is a pure function (`compose_digest`) separate from delivery
(`send_digest`), because the interesting failure mode is a digest that
reads fine but says something false — and that is only testable if you
can assert on the text without sending mail.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import UTC, date, datetime
from decimal import Decimal
from email.mime.text import MIMEText

from app.services.allocation import AllocationPlan, DriftReport
from app.services.reconciliation import ReconcileReport

_LOG = logging.getLogger("mbp.digest")

#: §9.1 — prompt for a bank-cash refresh once the number is older than this.
BANK_CASH_STALE_DAYS = 35


def _fmt_money(value: Decimal, currency: str) -> str:
    return f"{currency} {value:,.0f}"


def _mom_line(
    net_worth: Decimal, previous: Decimal | None, currency: str
) -> str:
    head = f"Net worth: {_fmt_money(net_worth, currency)}"
    if previous is None or previous == 0:
        # Say nothing rather than print a fake 0% — the first digest has
        # no prior month, and a made-up baseline would read as real.
        return head
    change = (net_worth - previous) / previous * Decimal(100)
    return f"{head}  (MoM {change:+.1f}%)"


def compose_digest(
    *,
    drift: DriftReport,
    reconciliation: ReconcileReport,
    net_worth: Decimal,
    base_currency: str = "HKD",
    as_of: date | None = None,
    previous_net_worth: Decimal | None = None,
    allocation: AllocationPlan | None = None,
    bank_cash_updated: date | None = None,
    reconciliation_checked: bool = True,
    prompt_bank_cash: bool = True,
) -> str:
    """Render the digest exactly as §10 specifies."""
    when = as_of or datetime.now(UTC).date()
    lines: list[str] = [
        f"Portfolio — {when.isoformat()}",
        _mom_line(net_worth, previous_net_worth, base_currency),
        "",
    ]

    if drift.classes:
        lines.extend(drift.digest_lines())
    else:
        lines.append("No asset classes configured (see config/targets.yaml).")
    lines.append("")

    if allocation is not None and allocation.allocations:
        lines.append(allocation.digest_line(base_currency))
        if allocation.sell_suggestions:
            # Only ever emitted when a band is breached AND contributions
            # cannot correct it (§8.4) — so if it appears, it means it.
            lines.append("")
            lines.append("Consider trimming (new money alone will not correct):")
            for item in allocation.sell_suggestions:
                lines.append(
                    f"  {item.asset_class} — {item.current_pct:.1f}% "
                    f"vs target {item.target_pct:.0f}%"
                )
        lines.append("")

    warnings = reconciliation.warnings
    if warnings:
        noun = "warning" if len(warnings) == 1 else "warnings"
        lines.append(f"Reconciliation: {len(warnings)} {noun}")
        lines.extend(reconciliation.digest_lines())
    elif reconciliation_checked:
        lines.append("Reconciliation: clean")
    else:
        # "Clean" and "nobody looked" are different claims, and only one
        # of them is reassuring. Printing the first when the second is
        # true is the failure this whole section exists to avoid.
        lines.append("Reconciliation: NOT CHECKED — no findings recorded.")
    lines.append("")

    if drift.unclassified:
        # A holding with no class is counted in the total but in no
        # percentage, so every weight is quietly slightly wrong until it
        # is classified. Worth a line.
        lines.append(
            f"Unclassified holdings ({len(drift.unclassified)}): "
            + ", ".join(drift.unclassified[:5])
        )
        lines.append("Add them to config/classification.yaml.")
        lines.append("")

    staleness = _bank_cash_line(bank_cash_updated, when) if prompt_bank_cash else None
    if staleness:
        lines.append(staleness)
        lines.append("")

    # Trim trailing blanks so the message ends cleanly on a lock screen.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _bank_cash_line(updated: date | None, today: date) -> str | None:
    """§9.1 — bank cash is manual, so nag when it goes stale."""
    if updated is None:
        return (
            "Bank cash has never been recorded — please add it "
            "(there is no API for retail bank balances, by design)."
        )
    age = (today - updated).days
    if age > BANK_CASH_STALE_DAYS:
        return f"Bank cash last updated {age} days ago — please refresh."
    return None


def send_digest(
    body: str,
    *,
    to_email: str,
    from_email: str,
    app_password: str,
    as_of: date | None = None,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 465,
) -> None:
    """Send the digest as plain text (§10, §14 Q4: email).

    Deliberately `MIMEText(..., "plain")`. The existing watchlist digest
    sends HTML; this one must not, because §10's whole premise is that
    the message is legible without opening anything.
    """
    when = (as_of or datetime.now(UTC).date()).isoformat()
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = f"Portfolio — {when}"
    message["From"] = from_email
    message["To"] = to_email

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
        server.login(from_email, app_password)
        server.sendmail(from_email, to_email, message.as_string())
    # Never log the body: it contains the complete financial picture.
    _LOG.info("portfolio digest sent to %s (%d chars)", to_email, len(body))


__all__ = [
    "BANK_CASH_STALE_DAYS",
    "compose_digest",
    "send_digest",
]
