"""Turn activities plus a price history into split findings.

Pure functions — no network, no Ghostfolio. `run.py` supplies the two
inputs and acts on what comes back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction

from app.services.splits import snap_split_factor

#: Activity types whose price is a real trade price and can be compared
#: with the provider's close. A DIVIDEND's "price" is a cash amount and a
#: FEE's is zero; neither says anything about a split.
COMPARABLE_TYPES = frozenset({"BUY", "SELL"})

#: Marks an activity we derived rather than one the broker reported —
#: `app.services.ghostfolio.opening` writes these. Its "price" is the
#: broker's average cost or the earliest disposal price, carried onto a
#: date chosen to sit before the known window, so comparing it with the
#: market close of that day is meaningless. SQQQ's opening balance read
#: as a factor of 30 against its real 25 and made the symbol look as
#: though it straddled two splits. Still restated, because it carries
#: quantity: leaving it alone unbalances the position.
DERIVED_MARKER = ":opening:"


@dataclass(slots=True)
class ActivityRatio:
    """One activity's price next to the provider's for the same day."""

    activity_id: str
    comment: str
    date: str
    quantity: Decimal
    our_price: Decimal
    provider_price: Decimal
    factor: Fraction | None

    @property
    def ratio(self) -> Decimal:
        return self.provider_price / self.our_price


@dataclass(slots=True)
class SymbolFinding:
    """What one symbol's activities say collectively."""

    symbol: str
    rows: list[ActivityRatio] = field(default_factory=list)
    #: Derived rows: restated with the rest, never used for the factor.
    carried: list[dict] = field(default_factory=list)
    #: BUY/SELL activities the provider had no price for. A restatement
    #: has to cover every one of them or it unbalances the position —
    #: see `repairable`.
    blind: int = 0

    @property
    def factors(self) -> set[Fraction | None]:
        return {row.factor for row in self.rows}

    @property
    def factor(self) -> Fraction | None:
        """The single split every checked activity agrees on, if any."""
        factors = self.factors
        return factors.pop() if len(factors) == 1 else None

    @property
    def healthy(self) -> bool:
        """Everything that could be checked agrees with the provider.

        Blind rows do not make a symbol unhealthy on their own — a
        symbol nobody can price is a gap in the check, not a split.
        """
        return self.factor is None and self.factors <= {None}

    @property
    def repairable(self) -> bool:
        """One split, every activity on the same side of it, none blind.

        The blind condition is not fussiness. Restating some of a
        symbol's trades and not others does not leave a slightly-wrong
        position, it leaves an invented one: dividing six of SQQQ's seven
        activities by 25 turns a position that nets to nothing into a
        phantom holding of 7.68 shares. Whole symbol or nothing.
        """
        return self.factor is not None and self.blind == 0

    @property
    def incomplete(self) -> bool:
        """A split is indicated but some activities could not be checked."""
        return self.factor is not None and self.blind > 0

    @property
    def straddles(self) -> bool:
        """Activities sit on both sides of a split.

        No single factor fixes them, and saying so is better than
        half-fixing: the two groups need different treatment and getting
        it wrong moves real money in the cost basis.
        """
        return len(self.factors) > 1

    def describe(self) -> str:
        if self.healthy:
            return f"{self.symbol}: agrees with the provider"
        ratios = ", ".join(f"{row.ratio:.3f}" for row in self.rows[:6])
        if self.repairable:
            return (
                f"{self.symbol}: split {self.factor} across all "
                f"{len(self.rows)} activities (ratios {ratios})"
            )
        if self.incomplete:
            return (
                f"{self.symbol}: split {self.factor} on {len(self.rows)} "
                f"activities, but {self.blind} more have no provider price "
                f"to check against — restating part of a symbol invents a "
                f"position"
            )
        return (
            f"{self.symbol}: activities STRADDLE a split — factors "
            f"{sorted(str(f) for f in self.factors)} (ratios {ratios})"
        )


#: How far from a trade date a close may be and still be comparable. A
#: split is a 2x-or-more signal, so a few days of ordinary drift cannot
#: manufacture one or hide one.
NEAREST_DAYS = 7


def nearest_price(
    history: dict[str, Decimal], day: str, *, max_days: int = NEAREST_DAYS
) -> Decimal | None:
    """The provider's close nearest to `day`, before or after.

    Before *or* after, which matters more than it looks. Ghostfolio only
    gathers prices from a symbol's first activity onwards, so the oldest
    activity of all — typically the derived opening balance, dated the
    day before everything else — has nothing before it. Skipping it
    leaves the symbol partly checked, which `repairable` then refuses;
    the position ends up neither correct nor repairable.
    """
    if not history:
        return None
    target = _ordinal(day)
    if target is None:
        return None
    best, best_distance = None, None
    for candidate, price in history.items():
        candidate_ordinal = _ordinal(candidate)
        if candidate_ordinal is None:
            continue
        distance = abs(candidate_ordinal - target)
        if distance > max_days:
            continue
        if best_distance is None or distance < best_distance:
            best, best_distance = price, distance
    return best


def _ordinal(day: str) -> int | None:
    try:
        return date.fromisoformat(day[:10]).toordinal()
    except ValueError:
        return None


def scan(
    activities: list[dict],
    history_by_symbol: dict[str, dict[str, Decimal]],
) -> list[SymbolFinding]:
    """Compare every comparable activity against the provider's history."""
    findings: dict[str, SymbolFinding] = {}
    for activity in activities:
        profile = activity.get("SymbolProfile") or {}
        symbol = str(profile.get("symbol") or "")
        if str(activity.get("type")) not in COMPARABLE_TYPES:
            continue
        our_price = Decimal(str(activity.get("unitPrice") or 0))
        if our_price <= 0:
            continue
        history = history_by_symbol.get(symbol)
        if not history:
            continue
        finding = findings.setdefault(symbol, SymbolFinding(symbol=symbol))
        if DERIVED_MARKER in str(activity.get("comment") or ""):
            finding.carried.append(activity)
            continue
        day = str(activity.get("date"))[:10]
        provider_price = nearest_price(history, day)
        if provider_price is None or provider_price <= 0:
            # Counted, not ignored: a symbol that cannot be checked in
            # full must not be repaired in part.
            finding.blind += 1
            continue
        finding.rows.append(
            ActivityRatio(
                activity_id=str(activity.get("id")),
                comment=str(activity.get("comment") or ""),
                date=day,
                quantity=Decimal(str(activity.get("quantity") or 0)),
                our_price=our_price,
                provider_price=provider_price,
                factor=snap_split_factor(provider_price / our_price),
            )
        )
    return sorted(
        (f for f in findings.values() if f.rows or f.blind or f.carried),
        key=lambda f: f.symbol,
    )


__all__ = [
    "COMPARABLE_TYPES",
    "DERIVED_MARKER",
    "ActivityRatio",
    "SymbolFinding",
    "NEAREST_DAYS",
    "nearest_price",
    "scan",
]
