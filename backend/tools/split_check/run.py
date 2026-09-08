"""Fetch what the scan needs from Ghostfolio, and apply the repair.

The repair is delete-then-reimport rather than an update, because
Ghostfolio's activity API has no partial update and a corrected row
pushed beside a stale one double-counts the position. `comment` — the
external id — is carried across unchanged, so the idempotency ledger
stays in agreement and a later real sync is still a no-op.

Trades and non-tradeable activities are imported in separate requests
for the reason recorded in `doc/ARCHITECTURE_NOTES.md` §13; the repair
only ever touches BUY and SELL, so its batch is uniformly tradeable.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.services.ghostfolio.audit import (
    DuplicateActivity,
    duplicate_external_ids,
)
from app.services.splits import restate
from tools.split_check.detect import DERIVED_MARKER, SymbolFinding, scan

_LOG = logging.getLogger("split_check.run")

#: How far back to ask Ghostfolio for prices. Two and a half years covers
#: every activity the portfolio has and keeps the response small enough
#: to fetch one symbol at a time.
HISTORY_DAYS = 900


async def load_history(client: Any, symbols: list[str]) -> dict[str, dict[str, Decimal]]:
    """Daily closes per symbol, as the provider reports them *today*.

    "Today" matters: the provider restates this series after every split,
    which is exactly why a check that passed last year can fail now.
    """
    history: dict[str, dict[str, Decimal]] = {}
    for symbol in symbols:
        response = await client._request(
            "GET", f"/api/v1/symbol/YAHOO/{symbol}?includeHistoricalData={HISTORY_DAYS}"
        )
        if response.status_code != 200:
            _LOG.warning("no price history for %s (%s)", symbol, response.status_code)
            continue
        history[symbol] = {
            str(point["date"])[:10]: Decimal(str(point["value"]))
            for point in (response.json().get("historicalData") or [])
            if point.get("value")
        }
    return history


def tradeable_symbols(activities: list[dict]) -> list[str]:
    """Symbols worth asking the provider about: real, priced instruments.

    Skips the ``GF_``-prefixed placeholders that account-level fees sit
    on — they have no price history and never had a split.
    """
    found = set()
    for activity in activities:
        profile = activity.get("SymbolProfile") or {}
        if str(profile.get("dataSource")) != "YAHOO":
            continue
        symbol = str(profile.get("symbol") or "")
        if symbol and not symbol.startswith("GF_"):
            found.add(symbol)
    return sorted(found)


async def find(
    client: Any,
) -> tuple[list[SymbolFinding], list[DuplicateActivity], list[dict]]:
    """Everything the CLI needs, from one pass over the activities.

    Duplicates are checked here rather than in a tool of their own
    because they are not independent of splits: a duplicated SELL drove
    TQQQ's replayed quantity negative, which made the opening-balance
    pass invent a BUY to cover it, and restating a split across an
    invented row would have carried the error forward looking tidier.
    Remove duplicates first, then restate.
    """
    activities = await client.list_activities()
    history = await load_history(client, tradeable_symbols(activities))
    return (
        scan(activities, history),
        duplicate_external_ids(activities),
        activities,
    )


async def remove_duplicates(
    client: Any, duplicates: list[DuplicateActivity]
) -> tuple[int, set[str]]:
    """Delete the extra copies, keeping one of each.

    Returns how many went and which symbols they touched — the caller
    needs the second half, because any opening balance for those symbols
    was derived from the quantities that are about to change.
    """
    removed, symbols = 0, set()
    for duplicate in duplicates:
        for extra in duplicate.extra:
            await client.delete_activity(str(extra.get("id")))
            symbols.add(str((extra.get("SymbolProfile") or {}).get("symbol") or ""))
            removed += 1
    return removed, symbols


async def retract_derived(
    client: Any, activities: list[dict], symbols: set[str]
) -> int:
    """Delete opening balances for symbols whose activities just changed.

    An opening balance exists to close the gap between what the broker
    holds and what the activities replay to. Change the activities and it
    is stale by construction — TQQQ's covered a SELL that only existed
    because it had been pushed twice, and leaving it behind would turn a
    closed position into a phantom holding of 3 shares. The next sync
    recomputes them from the broker's own position list.
    """
    removed = 0
    for activity in activities:
        symbol = str((activity.get("SymbolProfile") or {}).get("symbol") or "")
        if symbol not in symbols:
            continue
        if DERIVED_MARKER not in str(activity.get("comment") or ""):
            continue
        await client.delete_activity(str(activity.get("id")))
        removed += 1
    return removed


def restated_payload(activity: dict, finding: SymbolFinding) -> dict[str, Any]:
    """One activity re-expressed on the provider's post-split basis."""
    factor = finding.factor
    assert factor is not None  # guarded by `repairable`
    quantity, price = restate(
        Decimal(str(activity.get("quantity") or 0)),
        Decimal(str(activity.get("unitPrice") or 0)),
        factor,
    )
    profile = activity.get("SymbolProfile") or {}
    return {
        "accountId": activity.get("accountId"),
        "comment": activity.get("comment"),
        "currency": activity.get("currency"),
        "date": activity.get("date"),
        # Untouched. A split does not refund the commission.
        "fee": activity.get("fee") or 0,
        "quantity": quantity,
        "symbol": str(profile.get("symbol")),
        "dataSource": str(profile.get("dataSource") or "YAHOO"),
        "type": activity.get("type"),
        "unitPrice": price,
    }


async def repair(
    client: Any, findings: list[SymbolFinding], activities: list[dict]
) -> int:
    """Restate every activity of every uniformly-split symbol.

    Straddling symbols are skipped on purpose — see `SymbolFinding`.
    """
    by_id = {str(a.get("id")): a for a in activities}
    payloads, doomed = [], []
    for finding in findings:
        if not finding.repairable:
            continue
        # Derived rows go with the reported ones. Their quantity was
        # computed to close a gap in the pre-split replay, so it needs
        # the same factor for the position to still net out.
        for activity in (
            *(by_id.get(row.activity_id) for row in finding.rows),
            *finding.carried,
        ):
            if activity is None:
                continue
            payloads.append(restated_payload(activity, finding))
            doomed.append(str(activity.get("id")))

    if not payloads:
        return 0
    for activity_id in doomed:
        await client.delete_activity(activity_id)
    await client.import_activities(payloads)
    return len(payloads)


__all__ = [
    "HISTORY_DAYS",
    "find",
    "remove_duplicates",
    "retract_derived",
    "load_history",
    "repair",
    "restated_payload",
    "tradeable_symbols",
]
