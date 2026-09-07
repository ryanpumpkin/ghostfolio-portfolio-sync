"""The Futu sync's ordering and its refusals (§3.3, §6.2, §6.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.domain import CashBalance, Position, Transaction, TransactionType
from tools.futu_sync.run import sync_futu


class FakeAdapter:
    def __init__(self, positions, transactions, balances) -> None:
        self._p, self._t, self._b = positions, transactions, balances

    async def list_positions(self):
        return self._p

    async def list_transactions(self):
        return self._t

    async def list_balances(self):
        return self._b


class FakeReport:
    def __init__(self, pushed: int) -> None:
        self.pushed = pushed
        self.already_pushed = 0
        self.ok = True
        self.skipped: list = []


class FakeSync:
    """Records the order of calls — that is what this module gets wrong."""

    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def push(self, records):
        self.calls.append(("push", [r.transaction_id for r in records]))
        return FakeReport(len(records))


def _tx(symbol: str, qty: str, price: str, tid: str) -> Transaction:
    return Transaction(
        source="futu", transaction_id=tid, external_id=f"futu:{tid}",
        symbol=symbol, side="buy", type=TransactionType.BUY,
        quantity=Decimal(qty), price=Decimal(price), currency="HKD",
        amount=Decimal(qty) * Decimal(price),
        timestamp=datetime(2026, 2, 7, tzinfo=UTC),
    )


@pytest.fixture
def wired(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "tools.futu_sync.run.GhostfolioSync",
        lambda **kw: FakeSync(calls),
    )
    monkeypatch.setattr("tools.futu_sync.run.load_crypto_overrides", lambda: {})
    monkeypatch.setattr(
        "tools.futu_sync.run.OwnAccountsRegistry",
        type("R", (), {"load": staticmethod(lambda: None)}),
    )

    async def _retract(**kw):
        calls.append(("retract", kw["account_id"]))
        return 2

    monkeypatch.setattr("tools.futu_sync.run.retract_opening_balances", _retract)
    return calls


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(wired) -> None:
    outcome = await sync_futu(
        client=object(), ledger=object(), account_id="a1",
        adapter=FakeAdapter([], [_tx("CC.BTCHKD", "0.002", "550102", "d1")], []),
        dry_run=True,
    )
    assert wired == []
    assert outcome.transactions == 1
    assert outcome.pushed == 0


@pytest.mark.asyncio
async def test_trades_are_pushed_before_openings_are_retracted(wired) -> None:
    """Order matters: retracting first would leave a window where the
    account is missing both the old opening row and the new trades."""
    position = Position(
        source="futu", symbol="CC.BTC", quantity=Decimal("0.00391"),
        avg_cost=Decimal("72801.11"), currency="USD", exchange="CRYPTO",
    )
    outcome = await sync_futu(
        client=object(), ledger=object(), account_id="a1",
        adapter=FakeAdapter(
            [position], [_tx("CC.BTC", "0.0039", "72801.11", "d1")], []
        ),
    )
    kinds = [c[0] for c in wired]
    assert kinds[0] == "push"
    assert kinds[1] == "retract"
    # The 0.00001 the deals cannot explain is booked, not ignored.
    assert outcome.opening and "CC.BTC" in outcome.opening[0]
    assert kinds[2] == "push"


@pytest.mark.asyncio
async def test_surplus_is_reported_never_booked(wired) -> None:
    """More implied than held is a duplicate or a missed sale. Booking a
    negative opening balance would hide it."""
    position = Position(
        source="futu", symbol="CC.BTC", quantity=Decimal("0.001"),
        avg_cost=Decimal("72801.11"), currency="USD", exchange="CRYPTO",
    )
    outcome = await sync_futu(
        client=object(), ledger=object(), account_id="a1",
        adapter=FakeAdapter(
            [position], [_tx("CC.BTC", "0.005", "72801.11", "d1")], []
        ),
    )
    assert outcome.surplus
    assert outcome.opening == []


@pytest.mark.asyncio
async def test_cash_is_pushed_when_an_fx_service_is_supplied(wired, monkeypatch) -> None:
    seen: list = []

    async def _push_cash(**kw):
        seen.append(kw["balances_by_account"])
        return []

    monkeypatch.setattr("tools.futu_sync.run.push_cash_balances", _push_cash)
    await sync_futu(
        client=object(), ledger=object(), account_id="a1", account_name="Futu",
        adapter=FakeAdapter(
            [], [], [CashBalance(source="futu", currency="HKD",
                                 amount=Decimal("4.6529251259799995"))]
        ),
        fx=object(),
    )
    assert seen == [{"Futu": [seen[0]["Futu"][0]]}]
    assert seen[0]["Futu"][0].amount == Decimal("4.6529251259799995")
