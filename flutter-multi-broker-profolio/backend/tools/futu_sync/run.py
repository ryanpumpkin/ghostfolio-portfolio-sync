"""Push everything Futu reports into Ghostfolio, in the right order.

Three steps, and the order matters:

1. **Trades.** Idempotent via the ledger (§3.3), so a re-run pushes only
   what is new. This is the load-bearing data.
2. **Opening balances.** Recomputed from scratch every time, because a
   derived row goes stale the moment more history arrives — the old one
   is retracted first, since a corrected row pushed beside a stale one
   doubles the position (§6.4).
3. **Cash.** A Ghostfolio account's balance is a separate field the
   activity import never touches, so without this every account reports
   its positions correctly and its cash as zero (§6.2).

Cash comes last on purpose: it is the only step that overwrites rather
than appends, and there is no reason to overwrite a balance if the trade
push has already failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.adapters._common import RetryPolicy
from app.adapters.futu.adapter import SOURCE_NAME, FutuAdapter
from app.adapters.futu.client import FutuOpenDClient
from app.services.ghostfolio.cash import push_cash_balances
from app.services.ghostfolio.config import load_crypto_overrides
from app.services.ghostfolio.opening import (
    build_opening_transactions,
    compute_gaps,
    opening_start_date,
    retract_opening_balances,
)
from app.services.ghostfolio.sync import GhostfolioSync
from app.services.own_accounts import OwnAccountsRegistry

_LOG = logging.getLogger("futu_sync")


@dataclass(slots=True)
class SyncOutcome:
    """What the run did, in enough detail to tell a no-op from a failure."""

    positions: int = 0
    transactions: int = 0
    pushed: int = 0
    already: int = 0
    retracted: int = 0
    opening: list[str] = field(default_factory=list)
    surplus: list[str] = field(default_factory=list)
    no_cost: list[str] = field(default_factory=list)
    cash: list[str] = field(default_factory=list)
    ok: bool = True

    def summary(self) -> str:
        return (
            f"{self.transactions} transaction(s), {self.positions} position(s); "
            f"pushed={self.pushed} already={self.already}"
        )


async def sync_futu(
    *,
    client: Any,
    ledger: Any,
    account_id: str,
    account_name: str = "Futu",
    fx: Any | None = None,
    adapter: Any | None = None,
    dry_run: bool = False,
) -> SyncOutcome:
    """Read Futu once, then reconcile Ghostfolio against it."""
    adapter = adapter or FutuAdapter(
        FutuOpenDClient(),
        # One attempt: OpenD is running only for this window, and a retry
        # of a multi-minute walk can outlive it.
        retry=RetryPolicy(max_attempts=1),
    )
    outcome = SyncOutcome()

    positions = await adapter.list_positions()
    transactions = await adapter.list_transactions()
    balances = await adapter.list_balances()
    outcome.positions = len(positions)
    outcome.transactions = len(transactions)

    def _sync() -> GhostfolioSync:
        return GhostfolioSync(
            client=client,
            ledger=ledger,
            account_id_by_source={SOURCE_NAME: account_id},
            crypto_overrides=load_crypto_overrides(),
            own_accounts=OwnAccountsRegistry.load(),
        )

    if not dry_run:
        report = await _sync().push(transactions)
        outcome.pushed = report.pushed
        outcome.already = report.already_pushed
        outcome.ok = report.ok
        outcome.retracted = await retract_opening_balances(
            client=client, account_id=account_id, ledger=ledger
        )

    gaps = compute_gaps(positions=positions, transactions=transactions)
    outcome.opening = [g.describe() for g in gaps.gaps]
    outcome.surplus = [g.describe() for g in gaps.surplus]
    outcome.no_cost = [g.describe() for g in gaps.no_cost]

    if gaps.gaps and not dry_run:
        built = build_opening_transactions(
            gaps,
            source=SOURCE_NAME,
            account_id=account_id,
            as_of=opening_start_date(transactions),
        )
        opening_report = await _sync().push(built)
        outcome.pushed += opening_report.pushed

    if fx is not None:
        results = await push_cash_balances(
            client=client,
            fx=fx,
            balances_by_account={account_name: balances},
            dry_run=dry_run,
        )
        outcome.cash = [r.describe() for r in results]

    return outcome


__all__ = ["SyncOutcome", "sync_futu"]
