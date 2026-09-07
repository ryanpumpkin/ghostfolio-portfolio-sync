"""The shared reconcile path: its ordering and its refusals.

Every source runs through this. When it lived inside the Futu
tool, IBKR and LongBridge skipped basis alignment entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.domain import CashBalance, Position, Transaction, TransactionType
from app.services.ghostfolio.reconcile import reconcile_source


class FakeAdapter:
    def __init__(self, positions, transactions, balances) -> None:
        self._p, self._t, self._b = positions, transactions, balances

    async def list_positions(self):
        return self._p

    async def list_transactions(self):
        return self._t

    async def list_balances(self):
        return self._b


class FakeClient:
    """Ghostfolio, as far as the sync is concerned."""

    def __init__(self, activities: list | None = None) -> None:
        self._activities = activities or []

    async def list_activities(self):
        return self._activities


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
        "app.services.ghostfolio.reconcile.GhostfolioSync",
        lambda **kw: FakeSync(calls),
    )
    monkeypatch.setattr("app.services.ghostfolio.reconcile.load_crypto_overrides", lambda: {})
    monkeypatch.setattr(
        "app.services.ghostfolio.reconcile.OwnAccountsRegistry",
        type("R", (), {"load": staticmethod(lambda: None)}),
    )

    async def _retract(**kw):
        calls.append(("retract", kw["account_id"]))
        return 2

    monkeypatch.setattr("app.services.ghostfolio.reconcile.retract_opening_balances", _retract)
    return calls


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(wired) -> None:
    outcome = await reconcile_source(
        client=FakeClient(), ledger=object(), account_id="a1",
        source="futu", account_name="Futu",
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
    outcome = await reconcile_source(
        client=FakeClient(), ledger=object(), account_id="a1",
        source="futu", account_name="Futu",
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
    outcome = await reconcile_source(
        client=FakeClient(), ledger=object(), account_id="a1",
        source="futu", account_name="Futu",
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

    monkeypatch.setattr("app.services.ghostfolio.reconcile.push_cash_balances", _push_cash)
    await reconcile_source(
        client=FakeClient(), ledger=object(), account_id="a1",
        source="futu", account_name="Futu",
        adapter=FakeAdapter(
            [], [], [CashBalance(source="futu", currency="HKD",
                                 amount=Decimal("4.6529251259799995"))]
        ),
        fx=object(),
    )
    assert seen == [{"Futu": [seen[0]["Futu"][0]]}]
    assert seen[0]["Futu"][0].amount == Decimal("4.6529251259799995")


class FakeStored:
    """Ghostfolio with activities already in it."""

    def __init__(self, activities: list) -> None:
        self._activities = activities

    async def list_activities(self):
        return self._activities


def _stored(comment: str, price: str = "100") -> dict:
    return {"accountId": "a1", "comment": comment, "unitPrice": price}


@pytest.mark.asyncio
async def test_partial_history_leaves_opening_balances_alone(wired) -> None:
    """The gap is (held - replayed). Measuring it against fewer trades
    than Ghostfolio stores books the shortfall as missing shares.

    Live: IBKR's default Flex window returns 43 trades against 104
    already stored, and asked for 3.6742 VOO the history already had.
    """
    from app.models.domain import Position

    position = Position(
        source="ibkr", symbol="VOO", quantity=Decimal("5.4692"),
        avg_cost=Decimal("556.96"), currency="USD", exchange="ARCA",
    )
    client = FakeStored([_stored(f"ibkr:-:{n}") for n in range(10)])
    outcome = await reconcile_source(
        client=client, ledger=object(), account_id="a1",
        source="ibkr", account_name="IBKR",
        adapter=FakeAdapter([position], [_tx("VOO", "1.795", "500", "d1")], []),
    )
    assert outcome.narrower_than_stored
    assert outcome.retracted == 0
    # The gap is still REPORTED — it just is not acted on.
    assert outcome.opening
    assert [c for c in wired if c[0] == "retract"] == []


@pytest.mark.asyncio
async def test_complete_history_still_recomputes(wired) -> None:
    from app.models.domain import Position

    position = Position(
        source="ibkr", symbol="VOO", quantity=Decimal("3"),
        avg_cost=Decimal("500"), currency="USD", exchange="ARCA",
    )
    client = FakeStored([_stored("ibkr:-:d1")])
    outcome = await reconcile_source(
        client=client, ledger=object(), account_id="a1",
        source="ibkr", account_name="IBKR",
        adapter=FakeAdapter([position], [_tx("VOO", "1", "500", "d1")], []),
    )
    assert outcome.narrower_than_stored == ""
    assert outcome.retracted == 2
