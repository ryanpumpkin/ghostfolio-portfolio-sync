"""API tests for portfolio/fx routes from aggregator-and-fx module."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.domain import CashBalance, PartialResult, PortfolioSnapshot, Position, Transaction
from app.services.aggregator import AggregationCredentialContext
from app.services.dependencies import get_portfolio_aggregator


def test_get_portfolio_returns_empty_snapshot(client: TestClient) -> None:
    resp = client.get(
        "/v1/portfolio",
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["base_currency"] == "USD"
    assert body["positions"] == []
    assert body["balances"] == []


def test_get_positions_balances_transactions_empty(client: TestClient) -> None:
    headers = {"Authorization": "Bearer good-token"}
    pos = client.get("/v1/positions", headers=headers)
    bal = client.get("/v1/balances", headers=headers)
    tx = client.get("/v1/transactions", headers=headers)
    assert pos.status_code == 200
    assert bal.status_code == 200
    assert tx.status_code == 200
    assert pos.json()["items"] == []
    assert bal.json()["items"] == []
    assert tx.json()["items"] == []


def test_get_fx_accepts_pairs(client: TestClient) -> None:
    resp = client.get("/v1/fx", params={"pairs": "USD:USD"})
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["base"] == "USD"
    assert body[0]["quote"] == "USD"
    assert body[0]["rate"] == "1"


def test_get_fx_rejects_invalid_pair(client: TestClient) -> None:
    resp = client.get("/v1/fx", params={"pairs": "USDUSD"})
    assert resp.status_code == 422


class _CaptureAggregator:
    def __init__(self) -> None:
        self.last_context: AggregationCredentialContext | None = None

    async def get_snapshot(
        self,
        user_id: str,
        *,
        base_currency: str = "USD",
        credential_context: AggregationCredentialContext | None = None,
    ) -> PortfolioSnapshot:
        _ = (user_id, base_currency)
        self.last_context = credential_context
        return PortfolioSnapshot(as_of=datetime.now(UTC), base_currency="USD")

    async def get_positions(
        self,
        user_id: str,
        *,
        source: str | None = None,
        credential_context: AggregationCredentialContext | None = None,
    ) -> PartialResult[Position]:
        _ = (user_id, source)
        self.last_context = credential_context
        return PartialResult(items=[], source_health=[])

    async def get_balances(
        self,
        user_id: str,
        *,
        source: str | None = None,
        credential_context: AggregationCredentialContext | None = None,
    ) -> PartialResult[CashBalance]:
        _ = (user_id, source)
        self.last_context = credential_context
        return PartialResult(items=[], source_health=[])

    async def get_transactions(
        self,
        user_id: str,
        *,
        source: str | None = None,
        since: str | None = None,
        limit: int | None = None,
        credential_context: AggregationCredentialContext | None = None,
    ) -> PartialResult[Transaction]:
        _ = (user_id, source, since, limit)
        self.last_context = credential_context
        return PartialResult(items=[], source_health=[])


def test_portfolio_routes_forward_wrapped_credential_context(app: FastAPI) -> None:
    fake = _CaptureAggregator()
    app.dependency_overrides[get_portfolio_aggregator] = lambda: fake
    token_map = {"conn-1": "tok-1", "conn-2": "tok-2"}
    map_b64 = base64.b64encode(json.dumps(token_map).encode("utf-8")).decode("ascii")
    key_b64 = base64.b64encode(bytes([7] * 32)).decode("ascii")

    with TestClient(app) as client:
        resp = client.get(
            "/v1/positions",
            headers={
                "Authorization": "Bearer good-token",
                "X-MBP-Creds": map_b64,
                "X-MBP-Creds-Key": key_b64,
            },
        )

    assert resp.status_code == 200
    assert fake.last_context is not None
    assert fake.last_context.wrapped_tokens_by_connection == token_map
    assert fake.last_context.shared_wrapped_token is None
    assert fake.last_context.unwrap_key == bytes([7] * 32)
