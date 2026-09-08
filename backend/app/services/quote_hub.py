"""Quote subscription multiplexer for client WebSocket sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.adapters.base import SourceAdapter


class QuoteSourceRegistry(Protocol):
    """Resolves a source name to an adapter with `stream_quotes`."""

    def for_source(self, source: str) -> SourceAdapter | None: ...


@dataclass(frozen=True, slots=True)
class _SubKey:
    source: str
    symbol: str


class QuoteHub:
    """Maintains upstream source subscriptions and fan-outs quote events."""

    def __init__(
        self,
        registry: QuoteSourceRegistry,
        *,
        heartbeat_interval: float = 20.0,
        reconnect_delay: float = 1.0,
    ) -> None:
        self._registry = registry
        self._heartbeat_interval = heartbeat_interval
        self._reconnect_delay = reconnect_delay

        self._client_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._client_subs: dict[str, set[_SubKey]] = {}
        self._key_clients: dict[_SubKey, set[str]] = {}
        self._source_tasks: dict[str, asyncio.Task[None]] = {}

        self._lock = asyncio.Lock()
        self._closed = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def register_client(self, client_id: str) -> asyncio.Queue[dict[str, Any]]:
        async with self._lock:
            if self._closed:
                raise RuntimeError("QuoteHub is closed")
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            self._client_queues[client_id] = queue
            self._client_subs[client_id] = set()
            self._ensure_heartbeat_locked()
            return queue

    async def unregister_client(self, client_id: str) -> None:
        async with self._lock:
            keys = list(self._client_subs.get(client_id, set()))
            for key in keys:
                self._detach_subscription_locked(client_id, key)
            self._client_subs.pop(client_id, None)
            self._client_queues.pop(client_id, None)
            if not self._client_queues:
                await self._stop_heartbeat_locked()

    async def subscribe(self, client_id: str, *, source: str, symbols: Iterable[str]) -> None:
        source_u = source.lower()
        symbol_list = [sym.strip().upper() for sym in symbols if sym.strip()]
        async with self._lock:
            subs = self._client_subs.get(client_id)
            if subs is None:
                raise KeyError(f"Unknown client: {client_id}")
            touched_sources: set[str] = set()
            for symbol in symbol_list:
                key = _SubKey(source=source_u, symbol=symbol)
                if key in subs:
                    continue
                subs.add(key)
                clients = self._key_clients.setdefault(key, set())
                clients.add(client_id)
                touched_sources.add(source_u)
            for src in touched_sources:
                self._restart_source_locked(src)

    async def unsubscribe(self, client_id: str, *, source: str, symbols: Iterable[str]) -> None:
        source_u = source.lower()
        symbol_list = [sym.strip().upper() for sym in symbols if sym.strip()]
        async with self._lock:
            touched = False
            for symbol in symbol_list:
                key = _SubKey(source=source_u, symbol=symbol)
                touched = self._detach_subscription_locked(client_id, key) or touched
            if touched:
                self._restart_source_locked(source_u)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            tasks = list(self._source_tasks.values())
            self._source_tasks.clear()
            await self._stop_heartbeat_locked()
            self._client_queues.clear()
            self._client_subs.clear()
            self._key_clients.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def ref_count(self, source: str, symbol: str) -> int:
        key = _SubKey(source=source.lower(), symbol=symbol.upper())
        return len(self._key_clients.get(key, set()))

    def active_symbols(self, source: str) -> set[str]:
        source_l = source.lower()
        return {key.symbol for key, clients in self._key_clients.items() if key.source == source_l and clients}

    def _detach_subscription_locked(self, client_id: str, key: _SubKey) -> bool:
        subs = self._client_subs.get(client_id)
        if subs is None or key not in subs:
            return False
        subs.remove(key)
        clients = self._key_clients.get(key)
        if clients is not None:
            clients.discard(client_id)
            if not clients:
                self._key_clients.pop(key, None)
        return True

    def _ensure_heartbeat_locked(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _stop_heartbeat_locked(self) -> None:
        task = self._heartbeat_task
        if task is None:
            return
        self._heartbeat_task = None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _restart_source_locked(self, source: str) -> None:
        task = self._source_tasks.pop(source, None)
        if task is not None:
            task.cancel()
        symbols = self.active_symbols(source)
        if symbols:
            self._source_tasks[source] = asyncio.create_task(self._run_source(source))

    async def _run_source(self, source: str) -> None:
        try:
            while True:
                async with self._lock:
                    if self._closed:
                        return
                    symbols = sorted(self.active_symbols(source))
                if not symbols:
                    return

                adapter = self._registry.for_source(source)
                if adapter is None:
                    await asyncio.sleep(self._reconnect_delay)
                    continue

                try:
                    async for quote in adapter.stream_quotes(symbols):
                        payload = {
                            "type": "quote",
                            "source": source,
                            "symbol": quote.symbol,
                            "quote": quote.model_dump(mode="json"),
                        }
                        await self._broadcast(source, quote.symbol.upper(), payload)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reconnect loop
                    await self._broadcast(
                        source,
                        "*",
                        {
                            "type": "upstream_error",
                            "source": source,
                            "message": str(exc),
                        },
                    )

                await asyncio.sleep(self._reconnect_delay)
        finally:
            async with self._lock:
                current = self._source_tasks.get(source)
                if current is not None and current.done():
                    self._source_tasks.pop(source, None)

    async def _broadcast(self, source: str, symbol: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            recipients: set[str] = set()
            key = _SubKey(source=source.lower(), symbol=symbol.upper())
            recipients.update(self._key_clients.get(key, set()))
            wildcard = _SubKey(source=source.lower(), symbol="*")
            recipients.update(self._key_clients.get(wildcard, set()))
            queues = [self._client_queues[cid] for cid in recipients if cid in self._client_queues]
        for queue in queues:
            queue.put_nowait(payload)

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                heartbeat = {
                    "type": "heartbeat",
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                async with self._lock:
                    queues = list(self._client_queues.values())
                for queue in queues:
                    queue.put_nowait(heartbeat)
            except asyncio.CancelledError:
                return


__all__ = ["QuoteHub", "QuoteSourceRegistry"]
