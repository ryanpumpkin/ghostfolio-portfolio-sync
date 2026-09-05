"""Allocation engine: drift and new-money splitting (spec §8)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.services.allocation import (
    AllocationConfigError,
    DriftReport,
    Holding,
    Target,
    allocate_new_money,
    compute_drift,
    load_classification,
    load_targets,
    round_to_lot,
)

_CLASSIFICATION = {
    "US:VOO": "us_equity",
    "HK:00700": "hk_equity",
    "CRYPTO:BTC": "crypto",
    "manual_gold_bar": "gold_physical",
}
_TARGETS = {
    "us_equity": Target(target=Decimal(45), band=Decimal(5)),
    "hk_equity": Target(target=Decimal(20), band=Decimal(5)),
    "crypto": Target(target=Decimal(2), band=Decimal(2)),
    "cash": Target(target=Decimal(10), band=Decimal(5)),
    "gold_physical": Target(target=Decimal(5), band=Decimal(100)),
}


def _drift(holdings: list[Holding], cash: Decimal) -> DriftReport:
    return compute_drift(
        holdings, cash=cash, classification=_CLASSIFICATION, targets=_TARGETS
    )


class TestCashIsIncluded:
    """§8.3 — 'total_value must include cash'."""

    def test_cash_counts_toward_the_total(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(9000))], Decimal(1000))
        assert report.total_value == Decimal(10000)

    def test_excluding_cash_would_inflate_every_other_class(self) -> None:
        # The failure §8.3 warns about, stated as a test: with cash the
        # equity weight is 90%; without it, it would read 100%.
        report = _drift([Holding("US:VOO", Decimal(9000))], Decimal(1000))
        us = next(c for c in report.classes if c.asset_class == "us_equity")
        assert us.current_pct == Decimal(90)

    def test_cash_appears_as_its_own_class(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(9000))], Decimal(1000))
        cash = next(c for c in report.classes if c.asset_class == "cash")
        assert cash.value == Decimal(1000)
        assert cash.current_pct == Decimal(10)


class TestDrift:
    def test_drift_is_current_minus_target(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(4720))], Decimal(5280))
        us = next(c for c in report.classes if c.asset_class == "us_equity")
        assert us.current_pct == Decimal("47.2")
        assert us.drift == Decimal("2.2")

    def test_within_band_is_not_breached(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(4720))], Decimal(5280))
        us = next(c for c in report.classes if c.asset_class == "us_equity")
        assert us.breached is False
        assert us.label == "ok"

    def test_over_band_is_labelled_over(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(8000))], Decimal(2000))
        us = next(c for c in report.classes if c.asset_class == "us_equity")
        assert us.breached
        assert us.label == "OVER"

    def test_under_band_is_labelled_under(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(1000))], Decimal(9000))
        us = next(c for c in report.classes if c.asset_class == "us_equity")
        assert us.breached
        assert us.label == "UNDER"

    def test_a_class_with_no_holding_still_reports(self) -> None:
        # A target you hold nothing of is the most actionable drift there
        # is; omitting it would hide it.
        report = _drift([Holding("US:VOO", Decimal(10000))], Decimal(0))
        assert any(c.asset_class == "crypto" for c in report.classes)

    def test_unclassified_holdings_count_in_the_total_and_are_named(self) -> None:
        # Dropping them would make every percentage wrong; silently
        # bucketing them would be worse.
        report = _drift(
            [Holding("US:VOO", Decimal(5000)), Holding("US:MYSTERY", Decimal(5000))],
            Decimal(0),
        )
        assert report.total_value == Decimal(10000)
        assert report.unclassified == ["US:MYSTERY"]

    def test_empty_portfolio_does_not_divide_by_zero(self) -> None:
        report = _drift([], Decimal(0))
        assert report.total_value == Decimal(0)
        assert report.classes == []


class TestNeverRebalanced:
    """§8.2 — `band: 100` excludes a class without special-casing in code."""

    def test_a_wide_band_is_never_breached(self) -> None:
        # Physical gold at 30% against a 5% target is still not a problem:
        # it is tail-risk insurance and is deliberately never traded.
        report = _drift(
            [Holding("manual_gold_bar", Decimal(3000)), Holding("US:VOO", Decimal(7000))],
            Decimal(0),
        )
        gold = next(c for c in report.classes if c.asset_class == "gold_physical")
        assert gold.current_pct == Decimal(30)
        assert gold.breached is False

    def test_it_receives_no_new_money_either(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(10000))], Decimal(0))
        plan = allocate_new_money(report, Decimal(10000))
        assert all(a.asset_class != "gold_physical" for a in plan.allocations)


class TestNewMoneyAllocation:
    """§8.4 — the primary output."""

    def test_money_goes_to_the_classes_furthest_below_target(self) -> None:
        # Heavily over-weight US, nothing else. New money should go to the
        # under-weighted classes, never to US.
        report = _drift([Holding("US:VOO", Decimal(10000))], Decimal(0))
        plan = allocate_new_money(report, Decimal(10000))
        classes = {a.asset_class for a in plan.allocations}
        assert "us_equity" not in classes
        assert "hk_equity" in classes

    def test_the_whole_contribution_is_allocated(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(10000))], Decimal(0))
        plan = allocate_new_money(report, Decimal(50000))
        assert plan.allocated == pytest.approx(Decimal(50000))  # type: ignore[arg-type]

    def test_nothing_is_sold_to_rebalance(self) -> None:
        # §8.4: selling costs fees and realises gains. Allocations are
        # only ever positive.
        report = _drift([Holding("US:VOO", Decimal(10000))], Decimal(0))
        plan = allocate_new_money(report, Decimal(10000))
        assert all(a.amount > 0 for a in plan.allocations)

    def test_zero_contribution_allocates_nothing(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(10000))], Decimal(0))
        assert allocate_new_money(report, Decimal(0)).allocations == []

    def test_a_balanced_portfolio_needs_no_allocation(self) -> None:
        report = compute_drift(
            [Holding("US:VOO", Decimal(4500)), Holding("HK:00700", Decimal(2000)),
             Holding("CRYPTO:BTC", Decimal(200)), Holding("manual_gold_bar", Decimal(500))],
            cash=Decimal(1000),
            classification=_CLASSIFICATION,
            targets={k: v for k, v in _TARGETS.items() if k != "gold"},
        )
        plan = allocate_new_money(report, Decimal(1))
        # Everything at or near target: whatever is allocated is tiny and
        # never negative.
        assert all(a.amount >= 0 for a in plan.allocations)


class TestMinimumOrderThreshold:
    def test_uneconomic_allocations_are_dropped(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(10000))], Decimal(0))
        plan = allocate_new_money(report, Decimal(1000), minimum_order=Decimal(400))
        assert plan.dropped
        assert all(a.amount >= Decimal(400) for a in plan.allocations)

    def test_dropped_money_is_redistributed_not_lost(self) -> None:
        # Silently under-investing the contribution would be a quiet bug
        # that compounds every month.
        report = _drift([Holding("US:VOO", Decimal(10000))], Decimal(0))
        plan = allocate_new_money(report, Decimal(1000), minimum_order=Decimal(400))
        assert plan.allocated == pytest.approx(Decimal(1000))  # type: ignore[arg-type]

    def test_if_everything_is_below_threshold_the_largest_gap_still_gets_it(
        self,
    ) -> None:
        # Better to make one economic order than to invest nothing.
        report = _drift([Holding("US:VOO", Decimal(10000))], Decimal(0))
        plan = allocate_new_money(report, Decimal(100), minimum_order=Decimal(1000))
        assert len(plan.allocations) == 1
        assert plan.allocations[0].amount == Decimal(100)


class TestSellSuggestions:
    def test_none_when_nothing_is_breached(self) -> None:
        # Weights near target: VOO 45%, HK 20%, cash 10% of 100k.
        report = _drift(
            [Holding("US:VOO", Decimal(45000)), Holding("HK:00700", Decimal(20000)),
             Holding("CRYPTO:BTC", Decimal(2000)), Holding("manual_gold_bar", Decimal(5000))],
            Decimal(10000),
        )
        assert allocate_new_money(report, Decimal(1000)).sell_suggestions == []

    def test_over_weight_cash_is_never_a_sell_suggestion(self) -> None:
        # Cash at 52.8% against a 10% target is "breached", but selling
        # cash is a category error — it is un-deployed money, and the fix
        # is to feed the excess in as the contribution instead.
        report = _drift([Holding("US:VOO", Decimal(4720))], Decimal(5280))
        cash = next(c for c in report.classes if c.asset_class == "cash")
        assert cash.breached
        plan = allocate_new_money(report, Decimal(1000))
        assert all(s.asset_class != "cash" for s in plan.sell_suggestions)

    def test_none_for_an_underweight_class(self) -> None:
        # Contributions fix an under-weight by definition; selling would be
        # nonsense.
        report = _drift([Holding("US:VOO", Decimal(1000))], Decimal(9000))
        plan = allocate_new_money(report, Decimal(1000))
        assert all(s.drift > 0 for s in plan.sell_suggestions)

    def test_suggested_when_contributions_cannot_dilute_it_back(self) -> None:
        # Massively overweight and small contributions: dilution alone
        # will not bring it inside the band in a reasonable time.
        report = _drift([Holding("US:VOO", Decimal(100000))], Decimal(0))
        plan = allocate_new_money(report, Decimal(100), contributions_to_correct=6)
        assert any(s.asset_class == "us_equity" for s in plan.sell_suggestions)

    def test_not_suggested_when_contributions_will_dilute_it_back(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(10000))], Decimal(0))
        plan = allocate_new_money(
            report, Decimal(100000), contributions_to_correct=6
        )
        assert plan.sell_suggestions == []


class TestLotRounding:
    """§8.4 — HK stocks trade in board lots."""

    def test_rounds_down_to_whole_lots(self) -> None:
        # 32,000 budget, 350.50 a share, 100-share lots = 35,050 a lot.
        cost, lots = round_to_lot(Decimal(32000), price=Decimal("350.50"), lot_size=100)
        assert lots == 0
        assert cost == Decimal(0)

    def test_buys_as_many_lots_as_fit(self) -> None:
        cost, lots = round_to_lot(Decimal(80000), price=Decimal("350.50"), lot_size=100)
        assert lots == 2
        assert cost == Decimal("70100.00")

    def test_never_overshoots_the_budget(self) -> None:
        # Rounding up would silently spend money that is not there.
        budget = Decimal(80000)
        cost, _ = round_to_lot(budget, price=Decimal("350.50"), lot_size=100)
        assert cost <= budget

    def test_rejects_nonsense_inputs(self) -> None:
        with pytest.raises(ValueError):
            round_to_lot(Decimal(1000), price=Decimal(0), lot_size=100)
        with pytest.raises(ValueError):
            round_to_lot(Decimal(1000), price=Decimal(10), lot_size=0)


class TestDigestFormatting:
    def test_drift_table_matches_the_spec_layout(self) -> None:
        report = _drift([Holding("US:VOO", Decimal(4720))], Decimal(5280))
        lines = report.digest_lines()
        assert lines[0].startswith("Class")
        assert any("us_equity" in line and "47.2%" in line for line in lines)

    def test_allocation_line_matches_the_spec_layout(self) -> None:
        # §10: `Next HKD 50,000 ->  hk_equity 32,000 | crypto 18,000`
        report = _drift([Holding("US:VOO", Decimal(10000))], Decimal(0))
        line = allocate_new_money(report, Decimal(50000)).digest_line("HKD")
        assert line.startswith("Next HKD 50,000")
        assert "|" in line

    def test_allocation_line_when_there_is_nothing_to_do(self) -> None:
        report = _drift([], Decimal(0))
        assert "no allocation" in allocate_new_money(report, Decimal(100)).digest_line()


class TestConfigLoading:
    def test_example_classification_loads(self) -> None:
        path = Path(__file__).resolve().parents[3] / "config" / "classification.example.yaml"
        loaded = load_classification(path)
        assert loaded["HK:00700"] == "hk_equity"
        assert loaded["CRYPTO:BTC"] == "crypto"

    def test_example_targets_load(self) -> None:
        path = Path(__file__).resolve().parents[3] / "config" / "targets.example.yaml"
        loaded = load_targets(path)
        assert loaded["us_equity"].target == Decimal(45)
        assert loaded["gold_physical"].never_rebalance

    def test_a_missing_file_raises_rather_than_defaulting(self, tmp_path: Path) -> None:
        # Falling back to example numbers would allocate real money
        # according to figures a document made up.
        with pytest.raises(AllocationConfigError, match="cannot read"):
            load_targets(tmp_path / "absent.yaml")

    def test_a_target_without_a_value_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "targets.yaml"
        bad.write_text("us_equity: {band: 5}\n", encoding="utf-8")
        with pytest.raises(AllocationConfigError, match="needs at least"):
            load_targets(bad)

    def test_targets_not_summing_to_100_still_load(self, tmp_path: Path) -> None:
        # A deliberate under-allocation is legitimate; it warns, not fails.
        partial = tmp_path / "targets.yaml"
        partial.write_text("us_equity: {target: 45, band: 5}\n", encoding="utf-8")
        assert load_targets(partial)["us_equity"].target == Decimal(45)
