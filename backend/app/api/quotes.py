"""Quote stream WebSocket endpoint."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, cast

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.core.e2e_backend import WrappedCredentialError, unwrap_from_backend
from app.core.settings import Settings, get_settings
from app.middleware.auth import AuthenticatedUser, TokenVerifier, get_token_verifier
from app.models.domain import Connection, Quote
from app.services.adapter_factory import AdapterFactory
from app.services.aggregator import ConnectionRepository
from app.services.dependencies import (
    get_adapter_factory,
    get_connection_repository,
    get_vault_service,
)
from app.services.vault import CredentialVaultService

router = APIRouter(tags=["quotes"])


@dataclass(frozen=True, slots=True)
class _WsCredentialContext:
    shared_token: str | None
    tokens_by_connection: dict[str, str]
    unwrap_key: bytes | None

    def token_for(self, connection_id: str) -> str | None:
        return self.tokens_by_connection.get(connection_id) or self.shared_token


class _SourceAdapter(Protocol):
    def stream_quotes(self, symbols: Iterable[str]) -> AsyncIterator[Quote]: ...


class _QuoteSession:
    def __init__(
        self,
        *,
        websocket: WebSocket,
        user_id: str,
        connections: list[Connection],
        adapter_factory: AdapterFactory,
        vault: CredentialVaultService,
        creds: _WsCredentialContext,
    ) -> None:
        self._websocket = websocket
        self._user_id = user_id
        self._connections = [
            conn
            for conn in connections
            if conn.enabled and conn.source.lower() != "manual"
        ]
        self._adapter_factory = adapter_factory
        self._vault = vault
        self._creds = creds

        self._symbols: set[str] = set()
        self._streams: dict[str, asyncio.Task[None]] = {}
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._lock = asyncio.Lock()

    @property
    def queue(self) -> asyncio.Queue[dict[str, Any]]:
        return self._queue

    async def set_symbols(self, symbols: Iterable[str]) -> None:
        async with self._lock:
            self._symbols = {symbol.strip().upper() for symbol in symbols if symbol.strip()}
            await self._restart_streams_locked()

    async def add_symbols(self, symbols: Iterable[str]) -> None:
        async with self._lock:
            self._symbols.update({symbol.strip().upper() for symbol in symbols if symbol.strip()})
            await self._restart_streams_locked()

    async def remove_symbols(self, symbols: Iterable[str]) -> None:
        async with self._lock:
            for symbol in symbols:
                cleaned = symbol.strip().upper()
                if cleaned:
                    self._symbols.discard(cleaned)
            await self._restart_streams_locked()

    async def close(self) -> None:
        async with self._lock:
            tasks = list(self._streams.values())
            self._streams.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _restart_streams_locked(self) -> None:
        tasks = list(self._streams.values())
        self._streams.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if not self._symbols:
            return

        symbols = sorted(self._symbols)
        for conn in self._connections:
            task = asyncio.create_task(self._run_connection(conn, symbols))
            self._streams[conn.connection_id] = task

    async def _run_connection(self, conn: Connection, symbols: list[str]) -> None:
        try:
            adapter = await self._build_adapter(conn)
            async for quote in cast(_SourceAdapter, adapter).stream_quotes(symbols):
                payload = {
                    "type": "quote",
                    "source": conn.source,
                    "connection_id": conn.connection_id,
                    "symbol": quote.symbol,
                    "price": str(quote.price),
                    "currency": quote.currency,
                    "timestamp": quote.timestamp.isoformat(),
                }
                self._queue.put_nowait(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface upstream errors to the socket client
            self._queue.put_nowait(
                {
                    "type": "upstream_error",
                    "source": conn.source,
                    "connection_id": conn.connection_id,
                    "message": str(exc),
                }
            )

    async def _build_adapter(self, conn: Connection) -> Any:
        if conn.server_key_mode:
            plaintext = await self._vault.use_credential(
                user_id=self._user_id,
                connection_id=conn.connection_id,
                purpose="quote_stream",
                client_token=None,
                fn=lambda raw: raw,
            )
            return self._adapter_factory.for_connection(
                connection_kind=conn.source,
                plaintext_creds=plaintext,
            )

        token = self._creds.token_for(conn.connection_id)
        if token is None:
            raise WrappedCredentialError(
                f"missing wrapped credentials for connection '{conn.connection_id}'"
            )
        if self._creds.unwrap_key is None:
            raise WrappedCredentialError("missing unwrap key for wrapped credentials")

        plaintext = unwrap_from_backend(token, key=self._creds.unwrap_key)
        return self._adapter_factory.for_connection(
            connection_kind=conn.source,
            plaintext_creds=plaintext,
        )


async def _sender(websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
    while True:
        payload = await queue.get()
        await websocket.send_json(payload)


@router.websocket("/quotes/stream")
async def quote_stream(
    websocket: WebSocket,
    settings: Annotated[Settings, Depends(get_settings)],
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
    connections_repo: Annotated[ConnectionRepository, Depends(get_connection_repository)],
    adapter_factory: Annotated[AdapterFactory, Depends(get_adapter_factory)],
    vault: Annotated[CredentialVaultService, Depends(get_vault_service)],
) -> None:
    user = await _authenticate_websocket(websocket, settings=settings, verifier=verifier)
    if user is None:
        return

    await websocket.accept()

    creds = _parse_credentials_from_upgrade(websocket)
    connections = await connections_repo.list_connections(user.user_id)
    session = _QuoteSession(
        websocket=websocket,
        user_id=user.user_id,
        connections=connections,
        adapter_factory=adapter_factory,
        vault=vault,
        creds=creds,
    )
    sender_task = asyncio.create_task(_sender(websocket, session.queue))

    initial_symbols = _parse_symbol_list(websocket.query_params.get("symbols"))
    await session.set_symbols(initial_symbols)

    try:
        while True:
            frame = await websocket.receive_json()
            if not isinstance(frame, dict):
                await websocket.send_json({"type": "error", "message": "Frame must be an object"})
                continue

            op = _message_op(frame)
            symbols = _symbols_from_frame(frame)

            if op == "subscribe":
                await session.set_symbols(symbols)
                await websocket.send_json(
                    {
                        "type": "ack",
                        "action": "subscribe",
                        "symbols": sorted({sym.upper() for sym in symbols if sym.strip()}),
                    }
                )
            elif op == "add_symbol":
                await session.add_symbols(symbols)
                await websocket.send_json({"type": "ack", "action": "add_symbol"})
            elif op == "remove_symbol":
                await session.remove_symbols(symbols)
                await websocket.send_json({"type": "ack", "action": "remove_symbol"})
            elif op == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await websocket.send_json({"type": "error", "message": f"Unsupported frame type '{op}'"})
    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
        await asyncio.gather(sender_task, return_exceptions=True)
        await session.close()


async def _authenticate_websocket(
    websocket: WebSocket,
    *,
    settings: Settings,
    verifier: TokenVerifier,
) -> AuthenticatedUser | None:
    if settings.auth_disabled:
        return AuthenticatedUser(user_id="dev-user")

    token = websocket.query_params.get("token")
    if token is None:
        auth_header = websocket.headers.get("authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

    if token is None or not token.strip():
        await _close_unauthorized(websocket, "missing_token")
        return None

    try:
        claims = verifier.verify(token)
    except Exception:
        await _close_unauthorized(websocket, "invalid_token")
        return None

    uid = claims.get("uid") or claims.get("sub") or claims.get("user_id")
    if not isinstance(uid, str) or not uid:
        await _close_unauthorized(websocket, "token_missing_subject")
        return None

    email_raw = claims.get("email")
    email = email_raw if isinstance(email_raw, str) else None
    return AuthenticatedUser(user_id=uid, email=email, claims=claims)


async def _close_unauthorized(websocket: WebSocket, reason: str) -> None:
    # 4401 is the customary close code used by WS auth middleware.
    _ = reason
    await websocket.close(code=4401)
    return None


def _parse_credentials_from_upgrade(websocket: WebSocket) -> _WsCredentialContext:
    creds_raw = (
        websocket.query_params.get("mbpCreds")
        or websocket.query_params.get("mbp_creds")
        or websocket.headers.get("x-mbp-creds")
    )
    key_raw = (
        websocket.query_params.get("mbpCredsKey")
        or websocket.query_params.get("mbp_creds_key")
        or websocket.headers.get("x-mbp-creds-key")
    )

    token_map = _parse_connection_token_map(creds_raw)
    shared = None if token_map else _normalize(creds_raw)
    key_bytes = _decode_b64_ascii(key_raw)
    return _WsCredentialContext(
        shared_token=shared,
        tokens_by_connection=token_map or {},
        unwrap_key=key_bytes,
    )


def _parse_connection_token_map(raw: str | None) -> dict[str, str] | None:
    value = _normalize(raw)
    if value is None:
        return None

    payload = _try_json_object(value)
    if payload is None:
        payload = _try_b64_json_object(value)
    if payload is None:
        return None

    # Wrapped token envelopes should be treated as a shared token string.
    if {"v", "expiresAt", "ct"}.issubset(payload):
        return None

    parsed: dict[str, str] = {}
    for key, token in payload.items():
        if not isinstance(key, str) or not isinstance(token, str):
            return None
        key_u = key.strip()
        token_u = token.strip()
        if not key_u or not token_u:
            return None
        parsed[key_u] = token_u
    return parsed or None


def _parse_symbol_list(raw: str | None) -> list[str]:
    if raw is None:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _message_op(frame: dict[str, Any]) -> str:
    # Legacy client uses `op`; older backend tests use `type`.
    value = frame.get("op")
    if not isinstance(value, str) or not value.strip():
        value = frame.get("type")
    if not isinstance(value, str):
        return ""
    op = value.strip().lower()
    if op == "unsubscribe":
        return "remove_symbol"
    if op == "add":
        return "add_symbol"
    if op == "remove":
        return "remove_symbol"
    return op


def _symbols_from_frame(frame: dict[str, Any]) -> list[str]:
    raw = frame.get("symbols")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    symbol = frame.get("symbol")
    if isinstance(symbol, str):
        return [symbol]
    return []


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed if trimmed else None


def _decode_b64_ascii(value: str | None) -> bytes | None:
    raw = _normalize(value)
    if raw is None:
        return None
    try:
        return base64.b64decode(raw.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        return None


def _try_json_object(raw: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return decoded if isinstance(decoded, dict) else None


def _try_b64_json_object(raw: str) -> dict[str, Any] | None:
    try:
        decoded_raw = base64.b64decode(raw.encode("ascii"), validate=True)
        decoded = json.loads(decoded_raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return decoded if isinstance(decoded, dict) else None
