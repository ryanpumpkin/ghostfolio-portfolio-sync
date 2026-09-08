"""Integrity checks against what Ghostfolio actually holds (§3.3, §6.4).

The idempotency ledger (§3.3) is what *should* stop a trade being pushed
twice, and it does — as long as it survives between runs. It is a SQLite
file on a mounted volume, and a sync that runs without that volume has an
empty ledger and cheerfully pushes everything again.

Found live: four Futu activities existed twice, each pair sharing one
external id. Their effect was not obvious from the totals —

* VOO   +0.0179 and +0.0174 extra shares, which is the entire 0.0353
  "surplus" that reconciliation had been reporting for days;
* SOFI  +20 extra shares;
* TQQQ  an extra SELL of 3, which drove the replayed quantity negative
  and caused the opening-balance pass to invent a BUY of 3 to cover it.

So a duplicate does not simply inflate a position: it can propagate into
a *derived* row that looks entirely reasonable on its own. The ledger
cannot detect this, because from its point of view nothing is wrong —
this has to be checked against Ghostfolio itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_LOG = logging.getLogger("mbp.ghostfolio.audit")


@dataclass(slots=True)
class DuplicateActivity:
    """One external id that Ghostfolio holds more than once."""

    external_id: str
    #: The copy to keep — the one created first.
    keep: dict[str, Any]
    #: The copies to remove.
    extra: list[dict[str, Any]]

    def describe(self) -> str:
        profile = self.keep.get("SymbolProfile") or {}
        return (
            f"{self.external_id}: {len(self.extra) + 1} copies of "
            f"{profile.get('symbol')} {self.keep.get('type')} "
            f"{self.keep.get('quantity')} @ {self.keep.get('unitPrice')} "
            f"({str(self.keep.get('date'))[:10]})"
        )


def duplicate_external_ids(activities: list[dict[str, Any]]) -> list[DuplicateActivity]:
    """Activities sharing an external id — the same trade pushed twice.

    The copy created first is kept. That is arbitrary between identical
    rows, and deliberately so: they *are* identical, and picking the
    oldest keeps the choice stable across runs rather than depending on
    the order Ghostfolio happened to return them in.

    An activity with no comment carries no external id and is skipped
    rather than grouped with every other blank — those are rows a human
    entered in the UI, and two of them are not evidence of anything.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for activity in activities:
        external_id = str(activity.get("comment") or "")
        if not external_id:
            continue
        grouped.setdefault(external_id, []).append(activity)

    duplicates = []
    for external_id, copies in sorted(grouped.items()):
        if len(copies) < 2:
            continue
        ordered = sorted(copies, key=lambda a: (str(a.get("createdAt") or ""),
                                                str(a.get("id") or "")))
        duplicates.append(
            DuplicateActivity(
                external_id=external_id, keep=ordered[0], extra=ordered[1:]
            )
        )
        _LOG.error(
            "%s exists %d times in Ghostfolio; the ledger cannot see this",
            external_id, len(copies),
        )
    return duplicates


__all__ = ["DuplicateActivity", "duplicate_external_ids"]
