"""Connection sync status event models and Firestore writer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


class ConnectionSyncStatus(StrEnum):
    """Status values persisted to `users/{uid}/connections/{cid}`."""

    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass(slots=True, frozen=True)
class ConnectionSyncStatusEvent:
    """Per-connection status update emitted after adapter calls."""

    connection_id: str
    status: ConnectionSyncStatus
    last_sync_at: datetime
    error_message: str | None = None


class ConnectionStatusListener(Protocol):
    """Receives published connection status events."""

    async def __call__(self, *, user_id: str, event: ConnectionSyncStatusEvent) -> None: ...


class ConnectionStatusEventPublisher(Protocol):
    """Publishes connection sync status events."""

    async def publish(self, *, user_id: str, event: ConnectionSyncStatusEvent) -> None: ...


class NoopConnectionStatusPublisher:
    """Default publisher used when status persistence is not configured."""

    async def publish(self, *, user_id: str, event: ConnectionSyncStatusEvent) -> None:
        _ = (user_id, event)


class InMemoryConnectionStatusPublisher:
    """Simple fan-out publisher for tests/local boot."""

    def __init__(self, listeners: list[ConnectionStatusListener] | None = None) -> None:
        self._listeners: list[ConnectionStatusListener] = list(listeners or [])

    def subscribe(self, listener: ConnectionStatusListener) -> None:
        self._listeners.append(listener)

    async def publish(self, *, user_id: str, event: ConnectionSyncStatusEvent) -> None:
        tasks = [listener(user_id=user_id, event=event) for listener in self._listeners]
        if tasks:
            await asyncio.gather(*tasks)


class ConnectionStatusWriter(Protocol):
    """Durably persists connection status to storage."""

    async def write_status(self, *, user_id: str, event: ConnectionSyncStatusEvent) -> None: ...


class FirestoreConnectionStatusWriter:
    """Writes status updates to Firestore using Firebase Admin client."""

    def __init__(self, *, firestore_client: Any | None = None) -> None:
        self._firestore_client = firestore_client

    async def write_status(self, *, user_id: str, event: ConnectionSyncStatusEvent) -> None:
        client = self._firestore_client
        if client is None:
            from firebase_admin import firestore

            client = firestore.client()

        payload = {
            "status": event.status.value,
            "lastSyncAt": event.last_sync_at,
            "errorMessage": event.error_message,
        }
        doc = client.collection("users").document(user_id).collection("connections").document(
            event.connection_id
        )
        await asyncio.to_thread(doc.set, payload, merge=True)


def listener_from_writer(writer: ConnectionStatusWriter) -> ConnectionStatusListener:
    """Build a listener closure that persists every event through `writer`."""

    async def _listener(*, user_id: str, event: ConnectionSyncStatusEvent) -> None:
        await writer.write_status(user_id=user_id, event=event)

    return _listener


@dataclass(slots=True)
class CapturingConnectionStatusWriter:
    """Test helper that captures writes in memory."""

    writes: list[tuple[str, ConnectionSyncStatusEvent]] = field(default_factory=list)
    callback: Callable[[str, ConnectionSyncStatusEvent], Awaitable[None] | None] | None = None

    async def write_status(self, *, user_id: str, event: ConnectionSyncStatusEvent) -> None:
        self.writes.append((user_id, event))
        if self.callback is None:
            return
        maybe = self.callback(user_id, event)
        if maybe is not None:
            await maybe

