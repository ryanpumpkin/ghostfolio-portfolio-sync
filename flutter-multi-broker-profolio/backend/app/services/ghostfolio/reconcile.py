"""Reconcile one source against Ghostfolio (spec §3.3, §6.2, §6.3, §6.4).

Every source needs the same four steps in the same order, and getting
the order wrong has cost real money in this repo more than once:

1. **Trades.** Idempotent via the ledger (§3.3), so a re-run pushes only
   what is new. This is the load-bearing data.
2. **Basis alignment.** Broker data is re-expressed on whatever basis
   Ghostfolio already stores. A split restates the provider's history
   but never the broker's, so without this a derived row lands beside
   activities measured on a different scale.
3. **Opening balances.** Recomputed from scratch, old ones retracted
   first — a derived row goes stale the moment more history arrives, and
   a corrected row pushed beside a stale one doubles the position (§6.4).
4. **Cash.** A Ghostfolio account's balance is a separate field the
   activity import never touches, so without this every account reports
   its positions correctly and its cash as zero (§6.2).

Cash is last on purpose: it is the only step that overwrites rather than
appends, and there is no reason to overwrite a balance if the trade push
has already failed.

This lives in `app/services` rather than beside one tool because all
three sources must share it. When it existed only inside the Futu tool,
IBKR and LongBridge were syncing without step 2 — which is precisely how
a fully-sold SQQQ position came back as 7.68 phantom shares worth 2,299
HKD inside a 99,000 HKD total.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.ghostfolio.cash import push_cash_balances
from app.services.ghostfolio.config import load_crypto_overrides
from app.services.ghostfolio.mapper import external_id_for
from app.services.ghostfolio.opening import (
    OPENING_COMMENT_PREFIX,
    build_opening_transactions,
    compute_gaps,
    opening_start_date,
    retract_opening_balances,
)
from app.services.ghostfolio.sync import GhostfolioSync
from app.services.own_accounts import OwnAccountsRegistry
from app.services.splits import align_to_stored_basis, detect_stored_basis

_LOG = logging.getLogger("mbp.ghostfolio.reconcile")


@dataclass(slots=True)
class SyncOutcome:
    """What the run did, in enough detail to tell a no-op from a failure."""

    source: str = ""
    positions: int = 0
    transactions: int = 0
    pushed: int = 0
    already: int = 0
    retracted: int = 0
    basis: list[str] = field(default_factory=list)
    opening: list[str] = field(default_factory=list)
    surplus: list[str] = field(default_factory=list)
    no_cost: list[str] = field(default_factory=list)
    cash: list[str] = field(default_factory=list)
    #: Activities the mapper refused, named. A count alone cannot tell a
    #: deliberately-refused option contract from a holding silently
    #: dropped for want of a venue (§7.1).
    skipped: list[str] = field(default_factory=list)
    #: Set when this run saw less history than Ghostfolio already holds.
    #: Opening balances are then left alone rather than recomputed from a
    #: subset — the difference would be booked as missing shares.
    narrower_than_stored: str = ""
    ok: bool = True

    def summary(self) -> str:
        return (
            f"{self.source}: {self.transactions} transaction(s), "
            f"{self.positions} position(s); "
            f"pushed={self.pushed} already={self.already}"
        )

    def report_lines(self) -> list[str]:
        """Everything worth a human's attention, most alarming last."""
        lines = [f"  cash     {line}" for line in self.cash]
        lines += [f"  SKIPPED  {line}" for line in self.skipped]
        lines += [f"  BASIS    {line}" for line in self.basis]
        if self.retracted:
            lines.append(f"  retracted {self.retracted} stale opening balance(s)")
        lines += [f"  OPENING  {line}" for line in self.opening]
        lines += [f"  NO COST  {line}" for line in self.no_cost]
        # Last because it is the one that means something is wrong rather
        # than merely missing: more shares implied than held is a
        # duplicate or a lost disposal, never a history gap.
        lines += [f"  SURPLUS  {line}   <-- investigate" for line in self.surplus]
        if self.narrower_than_stored:
            lines.append(f"  PARTIAL  {self.narrower_than_stored}")
        return lines


def _stored_basis(stored_activities: list[Any], transactions: list[Any]) -> Any:
    """Measure our basis against Ghostfolio's on the SAME trades.

    Joined on the id the broker assigned, so the ratio is taken from
    identical rows — no date alignment, no market movement, no guessing.
    """
    stored = {
        str(a.get("comment") or ""): a.get("unitPrice") for a in stored_activities
    }
    return detect_stored_basis(
        (tx.symbol, tx.price, Decimal(str(stored[key])))
        for tx in transactions
        if tx.symbol and tx.price
        and (key := tx.external_id or external_id_for(tx)) in stored
        and stored[key]
    )


async def reconcile_source(
    *,
    client: Any,
    ledger: Any,
    adapter: Any,
    source: str,
    account_id: str,
    account_name: str,
    fx: Any | None = None,
    dry_run: bool = False,
) -> SyncOutcome:
    """Read one source once, then bring Ghostfolio into line with it."""
    outcome = SyncOutcome(source=source)

    positions = await adapter.list_positions()
    transactions = await adapter.list_transactions()
    balances = await adapter.list_balances()
    outcome.positions = len(positions)
    outcome.transactions = len(transactions)

    def _sync() -> GhostfolioSync:
        return GhostfolioSync(
            client=client,
            ledger=ledger,
            account_id_by_source={source: account_id},
            crypto_overrides=load_crypto_overrides(),
            own_accounts=OwnAccountsRegistry.load(),
        )

    # An opening balance is the difference between what the broker holds
    # and what the pushed activities replay to. That subtraction is only
    # valid if THIS run read at least as much history as Ghostfolio
    # already stores — otherwise the gap is measured against a subset and
    # the rebooked row double-counts whatever the run could not see.
    #
    # Live: IBKR's default Flex window returns 43 trades while Ghostfolio
    # holds 104 from an earlier backfill. Reconciling on the 43 asked for
    # an opening balance of 3.6742 VOO that the stored history already
    # accounted for.
    stored_activities = [
        a
        for a in await client.list_activities()
        if str(a.get("accountId") or "") == account_id
    ]
    stored_real = [
        a for a in stored_activities
        if OPENING_COMMENT_PREFIX not in str(a.get("comment") or "")
        and ":opening:" not in str(a.get("comment") or "")
    ]
    history_complete = len(transactions) >= len(stored_real)
    if not history_complete:
        outcome.narrower_than_stored = (
            f"read {len(transactions)} transaction(s) but Ghostfolio holds "
            f"{len(stored_real)} for this account — opening balances left "
            "untouched. Re-run with the full history to recompute them."
        )

    if not dry_run:
        report = await _sync().push(transactions)
        outcome.pushed = report.pushed
        outcome.already = report.already_pushed
        outcome.ok = report.ok
        outcome.skipped = [
            f"{s.external_id}: {s.reason.value} {s.detail}".rstrip()
            for s in report.skipped
        ]
        if history_complete:
            outcome.retracted = await retract_opening_balances(
                client=client, account_id=account_id, ledger=ledger
            )

    basis = _stored_basis(stored_activities, transactions)
    outcome.basis = basis.describe()
    transactions, positions = align_to_stored_basis(
        transactions, positions, basis.factors
    )

    gaps = compute_gaps(positions=positions, transactions=transactions)
    outcome.opening = [g.describe() for g in gaps.gaps]
    outcome.surplus = [g.describe() for g in gaps.surplus]
    outcome.no_cost = [g.describe() for g in gaps.no_cost]

    if gaps.gaps and not dry_run and history_complete:
        built = build_opening_transactions(
            gaps,
            source=source,
            account_id=account_id,
            as_of=opening_start_date(transactions),
        )
        outcome.pushed += (await _sync().push(built)).pushed

    if fx is not None:
        results = await push_cash_balances(
            client=client,
            fx=fx,
            balances_by_account={account_name: balances},
            dry_run=dry_run,
        )
        outcome.cash = [r.describe() for r in results]

    return outcome


__all__ = ["SyncOutcome", "reconcile_source"]
