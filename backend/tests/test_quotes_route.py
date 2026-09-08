"""API tests for /v1/quotes/stream WebSocket route."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.adapters.base import SourceAdapter
from app.models.domain import (
    CashBalance,
    Connection,
    Position,
    Quote,
    SourceHealth,
    SourceHealthStatus,
    Transaction,
)
from app.services.adapter_factory import AdapterFactory
from app.services.aggregator import InMemoryConnectionRepository
from app.services.dependencies import (
    get_adapter_factory,
    get_connection_repository,
    get_vault_service,
)
from app.services.vault import (
    CredentialVaultService,
    InMemoryConnectionVaultStore,
    InMemoryKmsProvider,
)


class _FakeQuoteAdapter(SourceAdapter):
    source = "longbridge"

    def __init__(self, *, symbol: str = "AAPL") -> None:
        self.symbol = symbol

    async def list_positions(self) -> list[Position]:
        return []

    async def list_balances(self) -> list[CashBalance]:
        return []

    async def list_transactions(
        self, *, since: str | None = None, limit: int | None = None
    ) -> list[Transaction]:
        _ = (since, limit)
        return []

    async def stream_quotes(self, symbols: list[str]) -> AsyncIterator[Quote]:
        if not symbols:
            return
        yield Quote(
            source="longbridge",
            symbol=self.symbol,
            price=Decimal("123"),
            currency="USD",
            timestamp=datetime.now(UTC),
        )
        await asyncio.sleep(3600)

    async def healthcheck(self) -> SourceHealth:
        return SourceHealth(source="longbridge", status=SourceHealthStatus.OK)


class _FakeAdapterFactory(AdapterFactory):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def for_connection(self, *, connection_kind: str, plaintext_creds: str) -> SourceAdapter:  # type: ignore[override]
        self.calls.append((connection_kind, plaintext_creds))
        return _FakeQuoteAdapter()


def _wrap_token(plaintext: str, *, key: bytes) -> str:
    nonce = bytes(range(12))
    encrypted = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    cipher_bytes = encrypted[:-16]
    mac = encrypted[-16:]
    inner = {
        "n": base64.b64encode(nonce).decode("ascii"),
        "c": base64.b64encode(cipher_bytes).decode("ascii"),
        "m": base64.b64encode(mac).decode("ascii"),
    }
    payload = {
        "v": 1,
        "expiresAt": int((datetime.now(UTC) + timedelta(minutes=2)).timestamp() * 1000),
        "ct": base64.b64encode(json.dumps(inner).encode("utf-8")).decode("ascii"),
    }
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _recv_until(
    ws: Any,
    *,
    frame_type: str,
    action: str | None = None,
) -> dict[str, Any]:
    for _ in range(8):
        frame = ws.receive_json()
        if frame.get("type") != frame_type:
            continue
        if action is not None and frame.get("action") != action:
            continue
        return frame
    raise AssertionError(f"missing frame type={frame_type!r} action={action!r}")


def test_quotes_websocket_requires_token(app: FastAPI) -> None:
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/quotes/stream"):
                pass


def test_quotes_websocket_subscribe_add_remove_and_ping(app: FastAPI) -> None:
    factory = _FakeAdapterFactory()
    repo = InMemoryConnectionRepository(
        [
            Connection(
                source="binance",
                connection_id="conn-e2e",
                display_name="BN",
                server_key_mode=False,
                enabled=True,
            )
        ]
    )

    vault = CredentialVaultService(
        store=InMemoryConnectionVaultStore(),
        kms=InMemoryKmsProvider(),
    )

    app.dependency_overrides[get_connection_repository] = lambda: repo
    app.dependency_overrides[get_adapter_factory] = lambda: factory
    app.dependency_overrides[get_vault_service] = lambda: vault

    key = bytes(range(32))
    wrapped = _wrap_token('{"apiKey":"k","apiSecret":"s"}', key=key)
    wrapped_map_b64 = base64.b64encode(
        json.dumps({"conn-e2e": wrapped}).encode("utf-8")
    ).decode("ascii")
    key_b64 = base64.b64encode(key).decode("ascii")
    query = urlencode(
        {
            "token": "good-token",
            "mbpCreds": wrapped_map_b64,
            "mbpCredsKey": key_b64,
        }
    )

    with TestClient(app) as client:
        with client.websocket_connect(f"/v1/quotes/stream?{query}") as ws:
            ws.send_json({"op": "subscribe", "symbols": ["AAPL"]})
            ack = _recv_until(ws, frame_type="ack", action="subscribe")
            assert ack["type"] == "ack"
            assert ack["action"] == "subscribe"

            quote = _recv_until(ws, frame_type="quote")
            assert quote["type"] == "quote"
            assert quote["symbol"] == "AAPL"

            ws.send_json({"op": "add_symbol", "symbol": "TSLA"})
            add_ack = _recv_until(ws, frame_type="ack", action="add_symbol")
            assert add_ack["action"] == "add_symbol"

            ws.send_json({"op": "remove_symbol", "symbols": ["AAPL"]})
            remove_ack = _recv_until(ws, frame_type="ack", action="remove_symbol")
            assert remove_ack["action"] == "remove_symbol"

            ws.send_json({"type": "ping"})
            pong = _recv_until(ws, frame_type="pong")
            assert pong["type"] == "pong"

            ws.send_json({"op": "wat"})
            err = _recv_until(ws, frame_type="error")
            assert err["type"] == "error"


def test_quotes_websocket_unwraps_wrapped_creds_from_upgrade_request(app: FastAPI) -> None:
    factory = _FakeAdapterFactory()
    repo = InMemoryConnectionRepository(
        [
            Connection(
                source="binance",
                connection_id="conn-e2e",
                display_name="Binance",
                server_key_mode=False,
                enabled=True,
            )
        ]
    )
    vault = CredentialVaultService(
        store=InMemoryConnectionVaultStore(),
        kms=InMemoryKmsProvider(),
    )

    app.dependency_overrides[get_connection_repository] = lambda: repo
    app.dependency_overrides[get_adapter_factory] = lambda: factory
    app.dependency_overrides[get_vault_service] = lambda: vault

    key = bytes(range(32))
    plaintext = '{"apiKey":"k","apiSecret":"s"}'
    wrapped = _wrap_token(plaintext, key=key)
    wrapped_map_b64 = base64.b64encode(
        json.dumps({"conn-e2e": wrapped}).encode("utf-8")
    ).decode("ascii")
    key_b64 = base64.b64encode(key).decode("ascii")

    with TestClient(app) as client:
        query = urlencode(
            {
                "token": "good-token",
                "mbpCreds": wrapped_map_b64,
                "mbpCredsKey": key_b64,
                "symbols": "AAPL",
            }
        )
        with client.websocket_connect(
            f"/v1/quotes/stream?{query}"
        ) as ws:
            frame = ws.receive_json()
            assert frame["type"] == "quote"

    assert factory.calls
    assert factory.calls[0][0] == "binance"
    assert factory.calls[0][1] == plaintext
