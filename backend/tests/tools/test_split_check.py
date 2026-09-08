"""The split scan and the repair it drives (§6.4)."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from tools.split_check.detect import nearest_price, scan
from tools.split_check.run import (
    repair,
    restated_payload,
    retract_derived,
    tradeable_symbols,
)


def _activity(
    activity_id: str, symbol: str, day: str, quantity: str, price: str,
    kind: str = "BUY", data_source: str = "YAHOO",
) -> dict:
    return {
        "id": activity_id,
        "comment": f"ibkr:trade:{activity_id}",
        "accountId": "acct",
        "currency": "USD",
        "date": f"{day}T00:00:00.000Z",
        "fee": 2.01,
        "quantity": Decimal(quantity),
        "unitPrice": Decimal(price),
        "type": kind,
        "SymbolProfile": {"symbol": symbol, "dataSource": data_source},
    }


#: Yahoo's series for SQQQ *today*, restated by the 1-for-25 reverse
#: split — the same days on which Futu charged about $8.
_SQQQ = {"2024-09-03": Decimal("223.75"), "2024-09-13": Decimal("204.25")}
_HEALTHY = {"2024-09-10": Decimal("232.90")}


class TestScan:
    def test_a_split_is_found_across_every_activity(self) -> None:
        findings = scan(
            [
                _activity("1", "SQQQ", "2024-09-03", "8", "8.7777"),
                _activity("2", "SQQQ", "2024-09-13", "9", "8.18"),
            ],
            {"SQQQ": _SQQQ},
        )
        assert len(findings) == 1
        assert findings[0].repairable
        assert findings[0].factor == Fraction(25)

    def test_a_symbol_that_agrees_is_healthy(self) -> None:
        findings = scan(
            [_activity("1", "GLD", "2024-09-10", "2", "232.43")], {"GLD": _HEALTHY}
        )
        assert findings[0].healthy
        assert not findings[0].repairable

    def test_activities_on_both_sides_are_flagged_not_repaired(self) -> None:
        """The dangerous case: one factor would move real money."""
        findings = scan(
            [
                _activity("1", "SQQQ", "2024-09-03", "8", "8.7777"),
                # Bought after the split, at the restated price.
                _activity("2", "SQQQ", "2024-09-13", "1", "204.25"),
            ],
            {"SQQQ": _SQQQ},
        )
        assert findings[0].straddles
        assert not findings[0].repairable
        assert "STRADDLE" in findings[0].describe()

    def test_a_holiday_trade_uses_the_last_close(self) -> None:
        findings = scan(
            [_activity("1", "SQQQ", "2024-09-14", "9", "8.18")], {"SQQQ": _SQQQ}
        )
        assert findings[0].factor == Fraction(25)

    def test_fees_and_dividends_say_nothing_about_splits(self) -> None:
        # A dividend's "price" is a cash amount and a fee's is zero.
        findings = scan(
            [
                _activity("1", "SQQQ", "2024-09-13", "1", "3.20", kind="DIVIDEND"),
                _activity("2", "SQQQ", "2024-09-13", "0", "0", kind="FEE"),
            ],
            {"SQQQ": _SQQQ},
        )
        assert findings == []

    def test_a_symbol_with_no_history_is_skipped_not_guessed(self) -> None:
        findings = scan([_activity("1", "SQQQ", "2024-09-13", "9", "8.18")], {})
        assert findings == []

    def test_the_oldest_activity_uses_the_nearest_close_after_it(self) -> None:
        """Ghostfolio gathers prices only from a symbol's first activity.

        The derived opening balance is dated the day before everything
        else, so nothing precedes it. Reaching forward a day is what
        keeps the symbol fully checked — and a symbol only partly checked
        is refused outright, which would leave it neither correct nor
        repairable.
        """
        assert nearest_price(_SQQQ, "2024-09-02") == Decimal("223.75")

    def test_a_date_far_from_any_close_stays_blind(self) -> None:
        assert nearest_price(_SQQQ, "2023-01-01") is None

    def test_a_blind_activity_blocks_the_whole_symbol(self) -> None:
        findings = scan(
            [
                _activity("1", "SQQQ", "2024-09-03", "8", "8.7777"),
                _activity("2", "SQQQ", "2020-01-02", "8", "8.00"),
            ],
            {"SQQQ": _SQQQ},
        )
        assert findings[0].blind == 1
        assert findings[0].incomplete
        assert not findings[0].repairable
        assert "no provider price" in findings[0].describe()


class TestSymbolSelection:
    def test_placeholders_and_manual_assets_are_not_asked_about(self) -> None:
        activities = [
            _activity("1", "SQQQ", "2024-09-13", "9", "8.18"),
            _activity("2", "GF_USD", "2024-09-13", "0", "0",
                      kind="FEE", data_source="MANUAL"),
        ]
        assert tradeable_symbols(activities) == ["SQQQ"]


class _FakeClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.imported: list[dict] = []

    async def delete_activity(self, activity_id: str) -> None:
        self.deleted.append(activity_id)

    async def import_activities(self, activities: list[dict]) -> dict:
        self.imported.extend(activities)
        return {"activities": activities}


class TestRepair:
    def test_the_payload_preserves_the_money_and_the_external_id(self) -> None:
        activity = _activity("2", "SQQQ", "2024-09-13", "9", "8.18")
        finding = scan([activity], {"SQQQ": _SQQQ})[0]
        payload = restated_payload(activity, finding)
        assert payload["quantity"] * payload["unitPrice"] == Decimal("9") * Decimal("8.18")
        # The ledger keys on this; changing it would orphan the record.
        assert payload["comment"] == "ibkr:trade:2"
        # A split does not refund the commission.
        assert payload["fee"] == 2.01
        assert payload["symbol"] == "SQQQ"

    @pytest.mark.asyncio
    async def test_repair_replaces_every_activity_of_a_split_symbol(self) -> None:
        activities = [
            _activity("1", "SQQQ", "2024-09-03", "8", "8.7777"),
            _activity("2", "SQQQ", "2024-09-13", "9", "8.18"),
        ]
        findings = scan(activities, {"SQQQ": _SQQQ})
        client = _FakeClient()
        assert await repair(client, findings, activities) == 2
        assert sorted(client.deleted) == ["1", "2"]
        assert len(client.imported) == 2

    @pytest.mark.asyncio
    async def test_a_straddling_symbol_is_left_alone(self) -> None:
        activities = [
            _activity("1", "SQQQ", "2024-09-03", "8", "8.7777"),
            _activity("2", "SQQQ", "2024-09-13", "1", "204.25"),
        ]
        findings = scan(activities, {"SQQQ": _SQQQ})
        client = _FakeClient()
        assert await repair(client, findings, activities) == 0
        assert client.deleted == []
        assert client.imported == []

    @pytest.mark.asyncio
    async def test_a_healthy_portfolio_changes_nothing(self) -> None:
        activities = [_activity("1", "GLD", "2024-09-10", "2", "232.43")]
        findings = scan(activities, {"GLD": _HEALTHY})
        client = _FakeClient()
        assert await repair(client, findings, activities) == 0
        assert client.deleted == []


class TestDerivedRows:
    """An opening balance is carried, never compared (§6.4).

    Its price is the broker's average cost or the earliest disposal
    price, stamped on a date chosen to sit before the known window.
    Compared against that day's close, SQQQ's read as a factor of 30
    against its real 25 and made the symbol look as though it straddled
    two splits — blocking the repair of a portfolio that had one clean
    split.
    """

    def _opening(self) -> dict:
        activity = _activity("0", "SQQQ", "2024-09-02", "8", "7.83")
        activity["comment"] = "futu:opening:US.SQQQ"
        return activity

    def test_a_derived_row_does_not_vote_on_the_factor(self) -> None:
        findings = scan(
            [
                self._opening(),
                _activity("1", "SQQQ", "2024-09-03", "8", "8.7777"),
                _activity("2", "SQQQ", "2024-09-13", "9", "8.18"),
            ],
            {"SQQQ": _SQQQ},
        )
        assert findings[0].repairable
        assert findings[0].factor == Fraction(25)
        assert len(findings[0].carried) == 1

    @pytest.mark.asyncio
    async def test_a_derived_row_is_still_restated(self) -> None:
        """It carries quantity, so leaving it alone unbalances the position."""
        activities = [
            self._opening(),
            _activity("1", "SQQQ", "2024-09-03", "8", "8.7777"),
        ]
        findings = scan(activities, {"SQQQ": _SQQQ})
        client = _FakeClient()
        assert await repair(client, findings, activities) == 2
        assert sorted(client.deleted) == ["0", "1"]
        restated = {a["comment"]: a for a in client.imported}
        assert restated["futu:opening:US.SQQQ"]["quantity"] == Decimal("8") / 25


class TestStaleDerivedRows:
    @pytest.mark.asyncio
    async def test_an_opening_balance_goes_when_its_symbol_changed(self) -> None:
        opening = _activity("0", "TQQQ", "2024-09-02", "3", "79.15")
        opening["comment"] = "futu:opening:US.TQQQ"
        other = _activity("1", "SOFI", "2024-09-02", "3", "11.10")
        other["comment"] = "futu:opening:US.SOFI"
        client = _FakeClient()
        removed = await retract_derived(client, [opening, other], {"TQQQ"})
        assert removed == 1
        assert client.deleted == ["0"]

    @pytest.mark.asyncio
    async def test_reported_activities_are_never_retracted(self) -> None:
        client = _FakeClient()
        activities = [_activity("1", "TQQQ", "2025-01-13", "3", "74.07")]
        assert await retract_derived(client, activities, {"TQQQ"}) == 0
        assert client.deleted == []
