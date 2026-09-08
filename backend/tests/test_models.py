"""Tests for the domain models and error envelope."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models import (
    CashBalance,
    Connection,
    ErrorEnvelope,
    FxRate,
    PartialResult,
    PortfolioSnapshot,
    Position,
    Quote,
    SourceHealth,
    SourceHealthStatus,
    Transaction,
)


def test_position_roundtrip() -> None:
    p = Position(
        source="ibkr",
        symbol="AAPL",
        quantity=Decimal("10"),
        avg_cost=Decimal("150"),
        last_price=Decimal("200"),
        currency="USD",
    )
    dumped = p.model_dump()
    assert dumped["symbol"] == "AAPL"
    # frozen=True -> immutable
    with pytest.raises(ValidationError):
        p.model_copy(update={"unknown": 1}).model_validate({"unknown": 1})


def test_source_health_enum() -> None:
    h = SourceHealth(source="binance", status=SourceHealthStatus.OK)
    assert h.status is SourceHealthStatus.OK


def test_portfolio_snapshot_defaults() -> None:
    snap = PortfolioSnapshot(as_of=datetime.now(UTC), base_currency="USD")
    assert snap.positions == []
    assert snap.balances == []
    assert snap.source_health == []


def test_partial_result_generic() -> None:
    pr: PartialResult[Position] = PartialResult(
        items=[
            Position(source="binance", symbol="BTC", quantity=Decimal("1"), currency="USD"),
        ],
        source_health=[SourceHealth(source="binance", status=SourceHealthStatus.OK)],
    )
    assert pr.items[0].symbol == "BTC"
    assert pr.source_health[0].status is SourceHealthStatus.OK


def test_other_models_construct() -> None:
    now = datetime.now(UTC)
    CashBalance(source="ibkr", currency="USD", amount=Decimal("100"))
    Transaction(source="ibkr", transaction_id="t1", timestamp=now)
    Quote(source="binance", symbol="BTC", price=Decimal("1"), currency="USD", timestamp=now)
    FxRate(base="USD", quote="HKD", rate=Decimal("7.8"), as_of=now)
    Connection(source="ibkr", connection_id="c1", display_name="My IBKR")


def test_error_envelope() -> None:
    env = ErrorEnvelope(code="x", message="y", request_id="z")
    assert env.code == "x"
    assert env.request_id == "z"


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        Position(  # type: ignore[call-arg]
            source="x",
            symbol="A",
            quantity=Decimal("1"),
            currency="USD",
            bogus=1,
        )
