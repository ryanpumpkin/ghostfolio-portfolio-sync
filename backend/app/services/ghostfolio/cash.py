"""Push cash balances into Ghostfolio account balances (spec §6.2, §7.1).

Why this is a separate step
---------------------------
Ghostfolio models an account's cash as a single `balance` field, entirely
separate from its activities. The activity import never touches it, and
§6.3 excludes deposits and withdrawals from the push — correctly, since
they are not trades — so nothing in the trade sync can ever tell
Ghostfolio how much cash an account holds.

The consequence is quiet and easy to miss: every account reports its
positions correctly and its cash as zero, so the portfolio total is
understated by exactly the cash. Found by the owner comparing the
Ghostfolio figure against what IBKR showed.

The one-currency problem
------------------------
A Ghostfolio account has one currency. A real brokerage account does
not: IBKR holds USD, HKD and CNH at once; LongBridge holds USD and HKD.
Balances in other currencies are therefore converted to the account's
currency at the **current** rate (§14 Q3), which is the right convention
for a balance — it is a statement about now, not a historical cost.

A currency that cannot be converted is **not** silently treated as zero.
That is what makes an understated total look like a correct one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from app.models.domain import CashBalance
from app.services.fx import FxRateUnavailableError, FxService
from app.services.ghostfolio.client import GhostfolioClient

_LOG = logging.getLogger("mbp.ghostfolio.cash")


class CashConversionError(RuntimeError):
    """A cash balance could not be expressed in the account's currency."""


@dataclass(slots=True)
class CashResult:
    """What one account's balance update did, and what it could not do."""

    account_name: str
    currency: str
    balance: Decimal = Decimal("0")
    #: Per-currency detail, so a wrong total can be traced to its source.
    components: dict[str, Decimal] = field(default_factory=dict)
    #: Currencies dropped because no rate was available. Never silent.
    unconverted: list[str] = field(default_factory=list)
    updated: bool = False

    @property
    def complete(self) -> bool:
        return not self.unconverted

    def describe(self) -> str:
        parts = ", ".join(
            f"{code} {amount}" for code, amount in sorted(self.components.items())
        )
        line = f"{self.account_name}: {self.currency} {self.balance} ({parts or 'none'})"
        if self.unconverted:
            line += f" — UNCONVERTED: {', '.join(self.unconverted)}"
        return line


async def convert_to_account_currency(
    balances: list[CashBalance],
    *,
    currency: str,
    fx: FxService,
) -> CashResult:
    """Sum cash into one currency, refusing to guess at a missing rate."""
    target = currency.upper()
    result = CashResult(account_name="", currency=target)

    for balance in balances:
        code = balance.currency.upper()
        result.components[code] = result.components.get(code, Decimal("0")) + balance.amount

    for code, amount in result.components.items():
        if code == target:
            result.balance += amount
            continue
        if amount == 0:
            # A zero balance needs no rate, and demanding one would fail
            # an account over a currency it does not actually hold.
            continue
        try:
            rate = await fx.get_rate(code, target)
        except (FxRateUnavailableError, OSError, ValueError) as exc:
            _LOG.warning(
                "no %s->%s rate (%s); %s %s is NOT included in the balance",
                code, target, exc, code, amount,
            )
            result.unconverted.append(f"{code} {amount}")
            continue
        result.balance += amount * rate.rate

    return result


async def push_cash_balances(
    *,
    client: GhostfolioClient,
    fx: FxService,
    balances_by_account: dict[str, list[CashBalance]],
    dry_run: bool = False,
) -> list[CashResult]:
    """Set each named Ghostfolio account's balance from its cash.

    Accounts are matched by name, not by a hardcoded id, so this keeps
    working if they are recreated. An account named in the input but
    absent from Ghostfolio is reported rather than created: creating one
    here would put activities and cash in different places.
    """
    accounts = {str(a.get("name")): a for a in await client.list_accounts()}
    results: list[CashResult] = []

    for name, balances in sorted(balances_by_account.items()):
        account = accounts.get(name)
        if account is None:
            _LOG.error(
                "no Ghostfolio account named %r; cash not pushed. Create the "
                "account first (§7.1: one per source).", name,
            )
            continue

        currency = str(account.get("currency") or "USD")
        result = await convert_to_account_currency(
            balances, currency=currency, fx=fx
        )
        result.account_name = name

        if not result.complete:
            # Writing a partial balance would replace a visibly-zero
            # number with a plausible-looking wrong one, which is worse.
            _LOG.error(
                "refusing to set %s balance: %s could not be converted to %s",
                name, ", ".join(result.unconverted), currency,
            )
            results.append(result)
            continue

        if not dry_run:
            await client.update_account(
                str(account.get("id")),
                name=name,
                currency=currency,
                balance=result.balance,
                platform_id=account.get("platformId"),
                comment=account.get("comment"),
            )
            result.updated = True

        results.append(result)
        _LOG.info("%s", result.describe())

    return results


__all__ = [
    "CashConversionError",
    "CashResult",
    "convert_to_account_currency",
    "push_cash_balances",
]
