"""Cash balance push (spec §6.2, §7.1).

Found live: every Ghostfolio account reported its positions correctly and
its cash as zero, because the activity import never touches an account's
`balance` and §6.3 excludes deposits from the push. The portfolio total
was understated by exactly the cash and nothing said so.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.domain import CashBalance
from app.services.fx import FxRate, FxRateUnavailableError
from app.services.ghostfolio.cash import (
    convert_to_account_currency,
    push_cash_balances,
)


class FakeFx:
    def __init__(self, rates: dict[tuple[str, str], str]) -> None:
        self.rates = rates

    async def get_rate(self, base: str, quote: str) -> FxRate:
        key = (base.upper(), quote.upper())
        if key not in self.rates:
            raise FxRateUnavailableError(f"no rate for {base}->{quote}")
        return FxRate(
            base=key[0], quote=key[1],
            rate=Decimal(self.rates[key]), as_of=datetime.now(UTC),
        )


class FakeClient:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts
        self.updates: list[dict] = []

    async def list_accounts(self) -> list[dict]:
        return self.accounts

    async def update_account(self, account_id, **kwargs):
        self.updates.append({"id": account_id, **kwargs})
        return {}


def _cash(currency: str, amount: str) -> CashBalance:
    return CashBalance(source="ibkr", currency=currency, amount=Decimal(amount))


@pytest.mark.asyncio
async def test_single_currency_needs_no_rate() -> None:
    result = await convert_to_account_currency(
        [_cash("USD", "1780.99")], currency="USD", fx=FakeFx({}),
    )
    assert result.balance == Decimal("1780.99")
    assert result.complete


@pytest.mark.asyncio
async def test_other_currencies_are_converted_at_the_current_rate() -> None:
    # §14 Q3: a balance is a statement about now, so it uses today's rate.
    result = await convert_to_account_currency(
        [_cash("USD", "1780.99"), _cash("HKD", "25.64")],
        currency="USD",
        fx=FakeFx({("HKD", "USD"): "0.1276"}),
    )
    assert result.balance == Decimal("1780.99") + Decimal("25.64") * Decimal("0.1276")
    assert result.components == {"USD": Decimal("1780.99"), "HKD": Decimal("25.64")}
    assert result.complete


@pytest.mark.asyncio
async def test_a_zero_balance_never_needs_a_rate() -> None:
    # IBKR reports CNH 0. Demanding a rate for it would fail the account
    # over a currency it does not actually hold.
    result = await convert_to_account_currency(
        [_cash("USD", "10"), _cash("CNH", "0")], currency="USD", fx=FakeFx({}),
    )
    assert result.complete
    assert result.balance == Decimal("10")


@pytest.mark.asyncio
async def test_a_missing_rate_is_reported_not_treated_as_zero() -> None:
    result = await convert_to_account_currency(
        [_cash("USD", "10"), _cash("JPY", "50000")],
        currency="USD",
        fx=FakeFx({}),
    )
    assert not result.complete
    assert result.unconverted == ["JPY 50000"]
    # The convertible part is still summed, but the result is not usable.
    assert result.balance == Decimal("10")


@pytest.mark.asyncio
async def test_incomplete_conversion_does_not_write_a_balance() -> None:
    """A visibly-zero balance beats a plausible-looking wrong one.

    Writing the convertible part would replace an obviously-missing
    number with one that looks right and is not.
    """
    client = FakeClient([
        {"id": "a1", "name": "IBKR", "currency": "USD", "platformId": None},
    ])
    results = await push_cash_balances(
        client=client,
        fx=FakeFx({}),
        balances_by_account={"IBKR": [_cash("USD", "10"), _cash("JPY", "5")]},
    )
    assert client.updates == []
    assert results[0].updated is False


@pytest.mark.asyncio
async def test_balance_is_written_when_everything_converts() -> None:
    client = FakeClient([
        {"id": "a1", "name": "IBKR", "currency": "USD", "platformId": None},
    ])
    await push_cash_balances(
        client=client,
        fx=FakeFx({("HKD", "USD"): "0.1276"}),
        balances_by_account={"IBKR": [_cash("USD", "100"), _cash("HKD", "100")]},
    )
    assert len(client.updates) == 1
    update = client.updates[0]
    assert update["id"] == "a1"
    # A full replacement, not a patch: name and currency must be resent.
    assert update["name"] == "IBKR"
    assert update["currency"] == "USD"
    assert update["balance"] == Decimal("100") + Decimal("12.76")


@pytest.mark.asyncio
async def test_unknown_account_is_reported_not_created() -> None:
    # Creating it here would put activities and cash in different places.
    client = FakeClient([])
    results = await push_cash_balances(
        client=client, fx=FakeFx({}),
        balances_by_account={"Nowhere": [_cash("USD", "1")]},
    )
    assert results == []
    assert client.updates == []


@pytest.mark.asyncio
async def test_dry_run_computes_without_writing() -> None:
    client = FakeClient([
        {"id": "a1", "name": "Futu", "currency": "HKD", "platformId": None},
    ])
    results = await push_cash_balances(
        client=client, fx=FakeFx({}),
        balances_by_account={"Futu": [CashBalance(
            source="futu", currency="HKD", amount=Decimal("-0.05"))]},
        dry_run=True,
    )
    assert client.updates == []
    assert results[0].balance == Decimal("-0.05")
    assert results[0].updated is False
