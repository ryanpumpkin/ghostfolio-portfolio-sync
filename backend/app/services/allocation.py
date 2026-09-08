"""Target allocation, drift, and new-money splitting (spec §8).

§8 calls this "the feature the owner actually wants and nothing off the
shelf provides it for this setup". Ghostfolio deliberately does not do it
(§7.2).

The engine works in the base currency and takes values already converted.
FX belongs to the existing `app.services.fx`, and mixing the two here
would make both harder to test.

Three things in §8 are easy to get subtly wrong, so they are called out:

* **`total_value` must include cash** (§8.3). Excluding it inflates every
  other class's percentage and makes the whole calculation wrong. The API
  therefore takes cash as a required argument rather than an optional one
  — you cannot forget it by accident.
* **`band: 100` excludes a class from rebalancing** without special-casing
  it in code (§8.2). Physical gold is held as tail-risk insurance and is
  deliberately never traded; the wide band expresses that declaratively.
* **New money does the work, not selling** (§8.4). Selling costs fees and
  realises gains. Sell-side suggestions are emitted only when a band is
  breached *and* new money alone cannot correct it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import yaml

_LOG = logging.getLogger("mbp.allocation")

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_HUNDRED = Decimal(100)

#: A class whose band is this wide is never rebalanced (§8.2).
NEVER_REBALANCE_BAND = Decimal(100)

#: Asset class used for cash when classification does not say otherwise.
CASH_CLASS = "cash"


class AllocationConfigError(RuntimeError):
    """classification.yaml or targets.yaml is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Target:
    """Target weight and tolerance band for one asset class, in percent."""

    target: Decimal
    band: Decimal

    @property
    def never_rebalance(self) -> bool:
        return self.band >= NEVER_REBALANCE_BAND


@dataclass(frozen=True, slots=True)
class Holding:
    """One instrument's value, already converted to the base currency."""

    symbol: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class ClassDrift:
    """Where one asset class actually sits against its target."""

    asset_class: str
    value: Decimal
    current_pct: Decimal
    target_pct: Decimal
    band: Decimal

    @property
    def drift(self) -> Decimal:
        """Signed, in percentage points. Positive means over target."""
        return self.current_pct - self.target_pct

    @property
    def breached(self) -> bool:
        if self.band >= NEVER_REBALANCE_BAND:
            return False
        return self.drift.copy_abs() > self.band

    @property
    def label(self) -> str:
        if not self.breached:
            return "ok"
        return "OVER" if self.drift > 0 else "UNDER"


@dataclass(slots=True)
class DriftReport:
    """Every class, plus the total the percentages were computed against."""

    total_value: Decimal
    classes: list[ClassDrift] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)

    @property
    def breached(self) -> list[ClassDrift]:
        return [c for c in self.classes if c.breached]

    def digest_lines(self) -> list[str]:
        """Plain-text table for the monthly digest (§10)."""
        lines = ["Class          Current   Target   Drift"]
        for item in sorted(self.classes, key=lambda c: c.asset_class):
            lines.append(
                f"{item.asset_class:<14}{item.current_pct:>6.1f}%  "
                f"{item.target_pct:>6.0f}  {item.drift:>+6.1f}   {item.label}"
            )
        return lines


@dataclass(frozen=True, slots=True)
class Allocation:
    """How much new money goes to one class."""

    asset_class: str
    amount: Decimal


@dataclass(slots=True)
class AllocationPlan:
    """The primary output of §8: where the next contribution should go."""

    new_money: Decimal
    allocations: list[Allocation] = field(default_factory=list)
    dropped: list[Allocation] = field(default_factory=list)
    sell_suggestions: list[ClassDrift] = field(default_factory=list)

    @property
    def allocated(self) -> Decimal:
        return sum((a.amount for a in self.allocations), Decimal(0))

    def digest_line(self, currency: str = "HKD", *, width: int = 58) -> str:
        """`Next HKD 50,000 ->  hk_equity 32,000 | crypto 18,000` (§10).

        Wraps onto continuation lines past `width`. The spec's example has
        two classes and fits on one line; with four it does not, and a
        line that wraps in the mail client destroys the alignment that
        makes the digest scannable on a phone.
        """
        if not self.allocations:
            return f"Next {currency} {self.new_money:,.0f} -> (no allocation)"

        head = f"Next {currency} {self.new_money:,.0f} -> "
        indent = " " * len(head)
        parts = [f"{a.asset_class} {a.amount:,.0f}" for a in self.allocations]

        lines: list[str] = []
        current = head + parts[0]
        for part in parts[1:]:
            candidate = f"{current} | {part}"
            if len(candidate) > width:
                lines.append(current)
                current = indent + part
            else:
                current = candidate
        lines.append(current)
        return "\n".join(lines)


# ── configuration ────────────────────────────────────────────────────────


def load_classification(path: str | Path | None = None) -> dict[str, str]:
    """canonical symbol -> asset class (§8.1).

    Hand-written by design: no automatic classifier can decide whether
    gold is "commodity" or "inflation hedge" for this owner's framework.
    """
    target = Path(path) if path else _CONFIG_DIR / "classification.yaml"
    document = _load_yaml(target, "classification")
    return {str(k).strip(): str(v).strip() for k, v in document.items()}


def load_targets(path: str | Path | None = None) -> dict[str, Target]:
    """asset class -> target percentage and tolerance band (§8.2)."""
    target_path = Path(path) if path else _CONFIG_DIR / "targets.yaml"
    document = _load_yaml(target_path, "targets")

    targets: dict[str, Target] = {}
    for name, spec in document.items():
        if not isinstance(spec, dict) or "target" not in spec:
            raise AllocationConfigError(
                f"{target_path}: '{name}' needs at least a 'target' value"
            )
        targets[str(name).strip()] = Target(
            target=Decimal(str(spec["target"])),
            band=Decimal(str(spec.get("band", 0))),
        )

    total = sum((t.target for t in targets.values()), Decimal(0))
    if total != _HUNDRED:
        # Not fatal — a deliberate under-allocation is legitimate — but it
        # silently skews every percentage, so it must not pass unnoticed.
        _LOG.warning(
            "targets sum to %s%%, not 100%%. Drift figures are still "
            "computed against actual total value, so a gap here means the "
            "targets themselves are incomplete.",
            total,
        )
    return targets


def _load_yaml(path: Path, what: str) -> dict[str, object]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AllocationConfigError(
            f"cannot read {what} config at {path}: {exc}"
        ) from exc
    document = yaml.safe_load(raw)
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise AllocationConfigError(f"{path} did not parse to a mapping")
    return document


# ── drift (§8.3) ─────────────────────────────────────────────────────────


def compute_drift(
    holdings: list[Holding],
    *,
    cash: Decimal,
    classification: dict[str, str],
    targets: dict[str, Target],
) -> DriftReport:
    """Current weights against target weights.

    ``cash`` is required, not optional: §8.3 says total_value must include
    it, and an argument you can forget is an argument that will be
    forgotten.
    """
    by_class: dict[str, Decimal] = {}
    unclassified: list[str] = []

    for holding in holdings:
        asset_class = classification.get(holding.symbol)
        if asset_class is None:
            # Counted in the total but attributed to no class — otherwise
            # the percentages would silently not add up.
            unclassified.append(holding.symbol)
            continue
        by_class[asset_class] = by_class.get(asset_class, Decimal(0)) + holding.value

    if cash:
        by_class[CASH_CLASS] = by_class.get(CASH_CLASS, Decimal(0)) + cash

    unclassified_value = sum(
        (h.value for h in holdings if h.symbol in set(unclassified)), Decimal(0)
    )
    total = sum(by_class.values(), Decimal(0)) + unclassified_value

    report = DriftReport(total_value=total, unclassified=sorted(set(unclassified)))
    if total <= 0:
        return report

    for asset_class in sorted(set(by_class) | set(targets)):
        value = by_class.get(asset_class, Decimal(0))
        target = targets.get(asset_class, Target(target=Decimal(0), band=Decimal(0)))
        report.classes.append(
            ClassDrift(
                asset_class=asset_class,
                value=value,
                current_pct=(value / total) * _HUNDRED,
                target_pct=target.target,
                band=target.band,
            )
        )

    if unclassified:
        _LOG.warning(
            "%d holding(s) have no asset class and are counted only in the "
            "total: %s. Add them to classification.yaml (§8.1).",
            len(unclassified),
            ", ".join(sorted(set(unclassified))[:10]),
        )
    return report


# ── new-money allocation (§8.4) ──────────────────────────────────────────


def allocate_new_money(
    report: DriftReport,
    new_money: Decimal,
    *,
    minimum_order: Decimal = Decimal(0),
    contributions_to_correct: int = 6,
) -> AllocationPlan:
    """Split new money to move closest to target **without selling**.

        target_value_after = (total_value + new_money) * target_pct
        shortfall          = max(0, target_value_after - current_value)
        allocation         = new_money * shortfall / sum(all shortfalls)

    Selling costs fees and realises gains; for someone contributing
    regularly, new money does almost all the work (§8.4).
    """
    plan = AllocationPlan(new_money=new_money)
    if new_money <= 0:
        return plan

    total_after = report.total_value + new_money

    shortfalls: dict[str, Decimal] = {}
    for item in report.classes:
        if item.band >= NEVER_REBALANCE_BAND:
            # Never rebalanced, so it never receives new money either.
            continue
        target_value_after = total_after * (item.target_pct / _HUNDRED)
        shortfall = target_value_after - item.value
        if shortfall > 0:
            shortfalls[item.asset_class] = shortfall

    total_shortfall = sum(shortfalls.values(), Decimal(0))
    if total_shortfall <= 0:
        return plan

    raw = [
        Allocation(
            asset_class=asset_class,
            amount=new_money * shortfall / total_shortfall,
        )
        for asset_class, shortfall in sorted(shortfalls.items())
    ]

    # Drop uneconomic orders rather than emitting them, then redistribute
    # what they would have received across the survivors — otherwise the
    # contribution silently under-invests (§8.4).
    kept = [a for a in raw if a.amount >= minimum_order]
    plan.dropped = [a for a in raw if a.amount < minimum_order]

    if kept:
        kept_shortfall = sum(shortfalls[a.asset_class] for a in kept)
        plan.allocations = [
            Allocation(
                asset_class=a.asset_class,
                amount=new_money * shortfalls[a.asset_class] / kept_shortfall,
            )
            for a in kept
        ]
    else:
        # Everything was below the threshold: allocate it all to the single
        # largest shortfall rather than investing nothing.
        largest = max(shortfalls.items(), key=lambda kv: kv[1])[0]
        plan.allocations = [Allocation(asset_class=largest, amount=new_money)]
        plan.dropped = []

    plan.sell_suggestions = _sell_suggestions(
        report, new_money, contributions_to_correct
    )
    return plan


def _sell_suggestions(
    report: DriftReport, new_money: Decimal, contributions: int
) -> list[ClassDrift]:
    """Only where a band is breached AND new money cannot fix it (§8.4)."""
    if not report.breached:
        return []
    suggestions: list[ClassDrift] = []
    projected_total = report.total_value + (new_money * contributions)
    for item in report.breached:
        if item.asset_class == CASH_CLASS:
            # Never suggest "selling" cash — it is a category error. An
            # over-weight cash position is not a holding to dispose of, it
            # is un-deployed money: the fix is to feed the excess into
            # `allocate_new_money` as the contribution, which is the same
            # operation §8.4 already describes.
            continue
        if item.drift <= 0:
            # Under target: contributions fix this by definition.
            continue
        # Over target. Its value does not change if we only ever buy, so
        # ask whether dilution alone brings it back inside the band.
        if projected_total <= 0:
            continue
        projected_pct = (item.value / projected_total) * _HUNDRED
        if (projected_pct - item.target_pct) > item.band:
            suggestions.append(item)
    return suggestions


def round_to_lot(
    amount: Decimal, *, price: Decimal, lot_size: int
) -> tuple[Decimal, int]:
    """Largest whole number of board lots affordable, and its cost (§8.4).

    HK equities trade in board lots, so an allocation of "32,000" is not
    directly actionable. Rounds DOWN — overshooting the budget is worse
    than under-investing slightly.
    """
    if price <= 0 or lot_size <= 0:
        raise ValueError("price and lot_size must be positive")
    lot_cost = price * lot_size
    lots = int((amount / lot_cost).to_integral_value(rounding=ROUND_DOWN))
    return (lot_cost * lots, lots)


__all__ = [
    "CASH_CLASS",
    "NEVER_REBALANCE_BAND",
    "Allocation",
    "AllocationConfigError",
    "AllocationPlan",
    "ClassDrift",
    "DriftReport",
    "Holding",
    "Target",
    "allocate_new_money",
    "compute_drift",
    "load_classification",
    "load_targets",
    "round_to_lot",
]
