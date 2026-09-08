"""Tests for connection sync status event publishing and persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.connection_status import (
    CapturingConnectionStatusWriter,
    ConnectionSyncStatus,
    ConnectionSyncStatusEvent,
    FirestoreConnectionStatusWriter,
    InMemoryConnectionStatusPublisher,
    listener_from_writer,
)


@pytest.mark.asyncio
async def test_in_memory_publisher_fans_out_to_listener() -> None:
    writer = CapturingConnectionStatusWriter()
    publisher = InMemoryConnectionStatusPublisher([listener_from_writer(writer)])
    event = ConnectionSyncStatusEvent(
        connection_id="c-1",
        status=ConnectionSyncStatus.OK,
        last_sync_at=datetime.now(UTC),
    )

    await publisher.publish(user_id="u-1", event=event)

    assert writer.writes == [("u-1", event)]


class _FakeDoc:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], bool]] = []

    def set(self, payload: dict[str, Any], merge: bool = False) -> None:
        self.calls.append((payload, merge))


class _FakeCollection:
    def __init__(self, doc: _FakeDoc) -> None:
        self._doc = doc

    def document(self, _id: str) -> _FakeCollection | _FakeDoc:
        if _id == "c-1":
            return self._doc
        return self

    def collection(self, _name: str) -> _FakeCollection:
        return self


class _FakeFirestoreClient:
    def __init__(self, doc: _FakeDoc) -> None:
        self._collection = _FakeCollection(doc)

    def collection(self, _name: str) -> _FakeCollection:
        return self._collection


@pytest.mark.asyncio
async def test_firestore_writer_uses_users_connections_document_path() -> None:
    doc = _FakeDoc()
    writer = FirestoreConnectionStatusWriter(firestore_client=_FakeFirestoreClient(doc))
    event = ConnectionSyncStatusEvent(
        connection_id="c-1",
        status=ConnectionSyncStatus.ERROR,
        last_sync_at=datetime.now(UTC),
        error_message="boom",
    )

    await writer.write_status(user_id="u-1", event=event)

    assert len(doc.calls) == 1
    payload, merge = doc.calls[0]
    assert merge is True
    assert payload["status"] == "error"
    assert payload["errorMessage"] == "boom"
