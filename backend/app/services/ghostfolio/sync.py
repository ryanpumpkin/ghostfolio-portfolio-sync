"""Orchestrates the Ghostfolio push (spec §3.3, §6.3, §6.4, §7.1).

Ties together the three pieces that each own one concern:

* ``mapper``  — what an internal record *becomes* (and what it must not).
* ``ledger``  — what has already been pushed, and how far each source got.
* ``client``  — the HTTP surface.

The orchestration itself carries two decisions worth stating:

**Batch per source, not globally.** Ghostfolio validates an import as a
unit and rejects the whole body if any single activity is invalid. A
global batch therefore means one malformed LongBridge symbol also loses
every Binance trade in the same run. Per-source batches contain the blast
radius to the source that actually has the problem.

**Record after the push, never before.** If the ledger were written first,
a failed import would mark records done and they would never be retried —
silent data loss that no later run could detect.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.adapters._common import PermanentError, TransientError
from app.models.domain import Transaction
from app.services.ghostfolio.client import GhostfolioClient
from app.services.ghostfolio.ledger import SyncLedger
from app.services.ghostfolio.mapper import (
    ActivityType,
    MappedActivity,
    SkippedActivity,
    SkipReason,
    map_transactions,
)
from app.services.own_accounts import OwnAccountsRegistry, resolve_transfers

_LOG = logging.getLogger("mbp.ghostfolio.sync")

#: Activity types Ghostfolio backs with a real, priced asset profile.
#: Everything else it treats as a user-created MANUAL item — see
#: `_push_source` for why that distinction has to drive the batching.
_TRADEABLE = frozenset(
    {ActivityType.BUY.value, ActivityType.SELL.value, ActivityType.DIVIDEND.value}
)


@dataclass(slots=True)
class SourceResult:
    """Outcome for one source in one run."""

    source: str
    pushed: int = 0
    already_pushed: int = 0
    skipped: list[SkippedActivity] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class SyncReport:
    """What a run did, in enough detail to explain itself.

    Every exclusion is accounted for. §6.4's reconciliation and the §10
    digest both need to distinguish "nothing to do" from "quietly dropped
    forty records", and a bare success count cannot.
    """

    results: list[SourceResult] = field(default_factory=list)

    @property
    def pushed(self) -> int:
        return sum(r.pushed for r in self.results)

    @property
    def already_pushed(self) -> int:
        return sum(r.already_pushed for r in self.results)

    @property
    def skipped(self) -> list[SkippedActivity]:
        return [s for r in self.results for s in r.skipped]

    @property
    def failed_sources(self) -> list[str]:
        return [r.source for r in self.results if not r.ok]

    @property
    def ok(self) -> bool:
        return not self.failed_sources

    def skip_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for skip in self.skipped:
            counts[skip.reason.value] = counts.get(skip.reason.value, 0) + 1
        return counts

    def summary(self) -> str:
        parts = [f"pushed={self.pushed}", f"already={self.already_pushed}"]
        if self.skipped:
            parts.append(f"skipped={len(self.skipped)} {self.skip_counts()}")
        if self.failed_sources:
            parts.append(f"FAILED={self.failed_sources}")
        return " ".join(parts)


class GhostfolioSync:
    """Pushes normalized transactions into Ghostfolio, idempotently."""

    def __init__(
        self,
        *,
        client: GhostfolioClient,
        ledger: SyncLedger,
        account_id_by_source: dict[str, str],
        crypto_overrides: dict[str, str] | None = None,
        own_accounts: OwnAccountsRegistry | None = None,
    ) -> None:
        self._client = client
        self._ledger = ledger
        self._accounts = account_id_by_source
        self._crypto_overrides = crypto_overrides or {}
        # Defaults to a registry that recognises nothing, which is the safe
        # direction: movements stay labelled DEPOSIT/WITHDRAWAL, and both
        # are excluded from the push anyway (§6.3).
        self._own_accounts = own_accounts or OwnAccountsRegistry.empty()

    async def push(self, transactions: Iterable[Transaction]) -> SyncReport:
        """Map, filter, and push. Safe to re-run (§3.3)."""
        by_source: dict[str, list[Transaction]] = {}
        for transaction in transactions:
            by_source.setdefault(transaction.source, []).append(transaction)

        report = SyncReport()
        for source, records in sorted(by_source.items()):
            report.results.append(await self._push_source(source, records))

        _LOG.info("ghostfolio sync complete: %s", report.summary())
        return report

    async def _push_source(
        self, source: str, transactions: Sequence[Transaction]
    ) -> SourceResult:
        result = SourceResult(source=source)

        # Recognise own-account movements as custody changes before
        # mapping (§6.3). This can only ever move a record between two
        # non-pushable types, so it can never fabricate a trade.
        recognised = resolve_transfers(list(transactions), self._own_accounts)

        mapped, skipped = map_transactions(
            recognised,
            account_id_by_source=self._accounts,
            crypto_overrides=self._crypto_overrides,
        )
        result.skipped = skipped

        to_push: list[MappedActivity] = self._ledger.filter_unpushed(mapped)
        result.already_pushed = len(mapped) - len(to_push)

        if not to_push:
            return result

        # Tradeable and non-tradeable activities go in SEPARATE import
        # requests. Ghostfolio mints a MANUAL asset (random UUID symbol)
        # for every FEE, INTEREST or LIABILITY, and within one batch it
        # then files same-symbol BUYs under that UUID too — verified
        # against 3.67.0 by importing a FEE and a BUY of SOFI together
        # and getting both back on symbol `886aa1a9-…`. The mapper no
        # longer gives a fee a tradeable symbol, so a collision should be
        # impossible; splitting the batch makes it impossible twice, and
        # cheaply, because the failure is silent and corrupts cost basis.
        for batch in (
            [a for a in to_push if a.payload.get("type") in _TRADEABLE],
            [a for a in to_push if a.payload.get("type") not in _TRADEABLE],
        ):
            if not batch:
                continue
            try:
                await self._client.import_activities([a.payload for a in batch])
            except (PermanentError, TransientError) as exc:
                # Whatever already landed stays recorded; the next run
                # retries only this batch, and every other source keeps
                # whatever it achieved.
                result.error = str(exc)[:500]
                _LOG.warning(
                    "ghostfolio import failed for %s (%d activities not recorded): %s",
                    source,
                    len(batch),
                    result.error,
                )
                return result
            self._ledger.record_pushed(batch)
            result.pushed += len(batch)
        return result

    def unresolved_symbols(self, report: SyncReport) -> list[str]:
        """Symbols that could not be mapped, for the §6.4 health channel.

        These are the ones worth acting on: an unresolved symbol is a
        holding that will silently never appear in Ghostfolio, which
        reconciliation would otherwise surface only as a quantity
        mismatch much later.
        """
        return sorted(
            {
                skip.detail
                for skip in report.skipped
                if skip.reason is SkipReason.UNRESOLVED_SYMBOL
            }
        )


__all__ = ["GhostfolioSync", "SourceResult", "SyncReport"]
