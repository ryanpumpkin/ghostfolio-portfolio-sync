"""Reconcile authoritative quantities against derived ones (spec §6.4).

§6.4 calls this "the main advantage of a custom back end over an
off-the-shelf tracker: only this system holds both the authoritative and
the derived view."

* **authoritative** — what the source says you hold right now.
* **derived** — what the normalized activity history implies you hold.

A gap between them almost always means a missing data source: convert
history, dust conversions, an airdrop, Earn distributions, a corporate
action. The gap is the signal to go and find it.

Three rules this module follows, all from §6.4
----------------------------------------------
1. **Never auto-correct.** Silently adjusting a quantity to match destroys
   the only evidence that something is missing. Every function here is
   read-only and returns findings.
2. **Compare like with like.** §4.4: the owner will hold the same asset in
   two places at once — a sub-minimum BTC balance waiting at Futu to reach
   the withdrawal threshold, plus the self-custody balance on the Ledger.
   Reconciliation is therefore per (asset, custody location), never a
   blind sum.
3. **Tolerance is configurable per asset.** 0.5% is the suggested default,
   but crypto dust and equity board lots behave differently.

Why transfers matter here but not in Ghostfolio
-----------------------------------------------
§6.3 excludes ``TRANSFER`` from the Ghostfolio push because Ghostfolio
tracks the asset, not where it sits. Reconciliation is the opposite: it is
*entirely* about where the asset sits, so a withdrawal from Binance to the
Ledger has to reduce Binance's derived quantity and raise the Ledger's.
Ignoring transfers here would report drift at both ends of every move the
owner has ever made.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from app.models.domain import (
    Position,
    SourceHealth,
    SourceHealthStatus,
    Transaction,
    TransactionType,
)
from app.services.symbols import (
    AssetKind,
    SymbolResolutionError,
    display_symbol,
    resolve,
)

_LOG = logging.getLogger("mbp.reconciliation")

DEFAULT_TOLERANCE = Decimal("0.005")  # 0.5%, per §6.4's suggestion


class ReconcileStatus(StrEnum):
    """Outcome for one (asset, custody) pair."""

    OK = "ok"
    #: Both views exist and disagree by more than the tolerance.
    DRIFT = "drift"
    #: A holding with no activity history at all. Expected before the
    #: historical import has run; distinct from DRIFT so a first run does
    #: not look like a portfolio-wide emergency.
    NO_HISTORY = "no_history"
    #: Activities imply a holding the source does not report. Usually a
    #: fully-closed position, or a transfer whose other leg is missing.
    NO_POSITION = "no_position"


@dataclass(frozen=True, slots=True)
class ReconcileItem:
    """One asset at one custody location, both ways round."""

    symbol: str
    custody: str
    authoritative: Decimal
    derived: Decimal
    status: ReconcileStatus
    kind: AssetKind = AssetKind.EQUITY

    @property
    def delta(self) -> Decimal:
        return self.authoritative - self.derived

    @property
    def delta_pct(self) -> Decimal | None:
        """Relative to the authoritative quantity, or None if that is 0."""
        if self.authoritative == 0:
            return None
        return (self.delta / self.authoritative).copy_abs()

    @property
    def needs_attention(self) -> bool:
        return self.status is not ReconcileStatus.OK

    def hint(self) -> str | None:
        """Where to look first. §6.4/§6.5."""
        if self.status is ReconcileStatus.OK:
            return None
        if self.status is ReconcileStatus.NO_HISTORY:
            return (
                "no activity history for this holding — expected until the "
                "historical import for this source has run"
            )
        if self.status is ReconcileStatus.NO_POSITION:
            return (
                "history implies a holding the source does not report — a "
                "closed position, or a transfer whose other leg is missing"
            )
        if self.kind is AssetKind.CRYPTO:
            return (
                "check convert/dust records and any airdrop — these are "
                "invisible to a plain trade-history endpoint (§5.7)"
            )
        return (
            "check corporate actions first: splits, reverse splits, rights "
            "issues and stock dividends are not always flagged in broker "
            "exports (§6.5)"
        )

    def describe(self) -> str:
        pct = f"{self.delta_pct:.2%}" if self.delta_pct is not None else "n/a"
        return (
            f"{self.symbol} @ {self.custody} — authoritative {self.authoritative} "
            f"/ derived {self.derived} (delta {self.delta}, {pct})"
        )


@dataclass(slots=True)
class ReconcileReport:
    """Findings, never corrections."""

    items: list[ReconcileItem] = field(default_factory=list)

    @property
    def warnings(self) -> list[ReconcileItem]:
        return [i for i in self.items if i.needs_attention]

    @property
    def ok(self) -> bool:
        return not self.warnings

    def to_source_health(self) -> list[SourceHealth]:
        """Per-source health, for the existing `source_health` channel (§6.4)."""
        by_custody: dict[str, list[ReconcileItem]] = defaultdict(list)
        for item in self.items:
            by_custody[item.custody].append(item)

        health: list[SourceHealth] = []
        for custody, items in sorted(by_custody.items()):
            problems = [i for i in items if i.needs_attention]
            drift = [i for i in problems if i.status is ReconcileStatus.DRIFT]
            if drift:
                status = SourceHealthStatus.DEGRADED
                message = f"{len(drift)} quantity mismatch(es): " + "; ".join(
                    i.describe() for i in drift[:3]
                )
            elif problems:
                status = SourceHealthStatus.OK
                message = f"{len(problems)} holding(s) without history yet"
            else:
                status = SourceHealthStatus.OK
                message = None
            health.append(
                SourceHealth(source=custody, status=status, message=message)
            )
        return health

    def digest_lines(self) -> list[str]:
        """Plain-text lines for the monthly digest (§10).

        Format mirrors the spec's example:
            BTC — authoritative 0.5120 / derived 0.4980 (delta 2.7%)
        """
        lines: list[str] = []
        for item in self.warnings:
            if item.status is ReconcileStatus.DRIFT:
                pct = f"{item.delta_pct:.1%}" if item.delta_pct is not None else "n/a"
                lines.append(
                    f"  {display_symbol(item.symbol)} — authoritative "
                    f"{item.authoritative} / derived {item.derived} "
                    f"(delta {pct})"
                )
            else:
                lines.append(
                    f"  {display_symbol(item.symbol)} — {item.status.value}"
                )
        return lines


def _canonical_key(symbol: str, exchange: str | None, currency: str | None) -> tuple[str, AssetKind]:
    try:
        canonical = resolve(symbol, exchange=exchange, currency=currency)
    except SymbolResolutionError:
        # An unresolvable symbol still reconciles against itself — dropping
        # it would hide a holding entirely, which is the failure mode §6.4
        # exists to catch.
        return symbol.strip().upper(), AssetKind.EQUITY
    return canonical.canonical_id, canonical.kind


def derived_quantities(
    transactions: list[Transaction],
) -> dict[tuple[str, str], Decimal]:
    """Quantity implied by the activity history, per (asset, custody).

    Buys add, sells subtract. Transfers and withdrawals *leave* the source
    they happened at; deposits arrive at it. That asymmetry is what makes
    the per-location comparison meaningful (§4.4).
    """
    derived: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for transaction in transactions:
        if transaction.symbol is None or transaction.quantity is None:
            continue
        symbol, _ = _canonical_key(
            transaction.symbol, None, transaction.currency
        )
        key = (symbol, transaction.source)
        quantity = transaction.quantity

        if transaction.type is TransactionType.BUY:
            derived[key] += quantity
        elif transaction.type is TransactionType.SELL:
            derived[key] -= quantity
        elif transaction.type in (
            TransactionType.TRANSFER,
            TransactionType.WITHDRAWAL,
        ):
            # Left this venue. Where it went is the counterparty's problem;
            # if that is another tracked location its own DEPOSIT records
            # the arrival.
            derived[key] -= quantity
        elif transaction.type is TransactionType.DEPOSIT:
            derived[key] += quantity
    return dict(derived)


def reconcile(
    positions: list[Position],
    transactions: list[Transaction],
    *,
    tolerance: Decimal = DEFAULT_TOLERANCE,
    tolerance_by_asset: dict[str, Decimal] | None = None,
) -> ReconcileReport:
    """Compare authoritative against derived. Reports; never corrects."""
    per_asset = tolerance_by_asset or {}

    authoritative: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    kinds: dict[str, AssetKind] = {}
    for position in positions:
        symbol, kind = _canonical_key(
            position.symbol, position.exchange, position.currency
        )
        kinds[symbol] = kind
        authoritative[(symbol, position.custody_location)] += position.quantity

    derived = derived_quantities(transactions)

    report = ReconcileReport()
    for key in sorted(set(authoritative) | set(derived)):
        symbol, custody = key
        auth = authoritative.get(key, Decimal(0))
        der = derived.get(key, Decimal(0))
        kind = kinds.get(symbol, AssetKind.EQUITY)

        if auth != 0 and key not in derived:
            status = ReconcileStatus.NO_HISTORY
        elif auth == 0 and der != 0:
            status = ReconcileStatus.NO_POSITION
        else:
            limit = per_asset.get(symbol, tolerance)
            if auth == 0:
                status = ReconcileStatus.OK if der == 0 else ReconcileStatus.DRIFT
            else:
                relative = ((auth - der) / auth).copy_abs()
                status = (
                    ReconcileStatus.OK if relative <= limit else ReconcileStatus.DRIFT
                )

        report.items.append(
            ReconcileItem(
                symbol=symbol,
                custody=custody,
                authoritative=auth,
                derived=der,
                status=status,
                kind=kind,
            )
        )

    if report.warnings:
        # Warn, do not fix. §6.4: "Do not auto-correct."
        _LOG.warning(
            "reconciliation: %d of %d holdings need attention: %s",
            len(report.warnings),
            len(report.items),
            "; ".join(i.describe() for i in report.warnings[:5]),
        )
    return report


__all__ = [
    "DEFAULT_TOLERANCE",
    "ReconcileItem",
    "ReconcileReport",
    "ReconcileStatus",
    "derived_quantities",
    "reconcile",
]
