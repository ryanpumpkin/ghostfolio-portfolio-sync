"""Tests for vault service and credential mode flows."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.vault import (
    FAILED_DECRYPT_COUNTER,
    ConnectionCredentialRecord,
    ConnectionMetadata,
    CreateConnectionInput,
    CredentialMode,
    CredentialVaultService,
    E2ECredentialCodec,
    FirestoreConnectionVaultStore,
    InMemoryConnectionVaultStore,
    InMemoryKmsProvider,
    SwitchModeInput,
    VaultValidationError,
)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


def _failed_counter_value(*, mode: str, reason: str) -> float:
    for metric in FAILED_DECRYPT_COUNTER.collect():
        for sample in metric.samples:
            if sample.name == "mbp_vault_failed_decrypt_total" and sample.labels == {
                "mode": mode,
                "reason": reason,
            }:
                return float(sample.value)
    return 0.0


@pytest.mark.asyncio
async def test_round_trip_encrypt_decrypt_for_both_modes() -> None:
    service = CredentialVaultService(
        store=InMemoryConnectionVaultStore(),
        kms=InMemoryKmsProvider(),
    )

    user_id = "user-1"
    token = "short-lived-token"
    secret = "binance-read-only-secret"
    blob = E2ECredentialCodec.encrypt(secret, client_token=token)

    meta_e2e = await service.create_connection(
        user_id,
        CreateConnectionInput(
            source="binance",
            display_name="Binance",
            connection_id="conn-e2e",
            credential_mode=CredentialMode.E2E,
            encrypted_blob=blob,
        ),
    )
    roundtrip_e2e = await service.use_credential(
        user_id=user_id,
        connection_id="conn-e2e",
        purpose="list_positions",
        client_token=token,
        fn=lambda plaintext: plaintext,
    )

    meta_server = await service.create_connection(
        user_id,
        CreateConnectionInput(
            source="ibkr",
            display_name="IBKR",
            connection_id="conn-server",
            credential_mode=CredentialMode.SERVER_KEY,
            plaintext_for_server_mode=secret,
        ),
    )
    roundtrip_server = await service.use_credential(
        user_id=user_id,
        connection_id="conn-server",
        purpose="list_positions",
        client_token=None,
        fn=lambda plaintext: plaintext,
    )

    assert isinstance(meta_e2e, ConnectionMetadata)
    assert meta_e2e.credential_mode is CredentialMode.E2E
    assert roundtrip_e2e == secret
    assert meta_server.credential_mode is CredentialMode.SERVER_KEY
    assert roundtrip_server == secret


@pytest.mark.asyncio
async def test_mode_switch_reencrypts_blob_correctly() -> None:
    service = CredentialVaultService(
        store=InMemoryConnectionVaultStore(),
        kms=InMemoryKmsProvider(),
    )

    user_id = "user-2"
    token = "token-2"
    secret = "longbridge-token"
    blob = E2ECredentialCodec.encrypt(secret, client_token=token)

    await service.create_connection(
        user_id,
        CreateConnectionInput(
            source="longbridge",
            display_name="LongBridge",
            connection_id="conn-1",
            credential_mode=CredentialMode.E2E,
            encrypted_blob=blob,
        ),
    )

    to_server = await service.switch_mode(
        user_id,
        "conn-1",
        SwitchModeInput(
            credential_mode=CredentialMode.SERVER_KEY,
            client_token=token,
        ),
    )
    assert to_server.credential_mode is CredentialMode.SERVER_KEY

    via_server = await service.use_credential(
        user_id=user_id,
        connection_id="conn-1",
        purpose="list_balances",
        client_token=None,
        fn=lambda plaintext: plaintext,
    )
    assert via_server == secret

    to_e2e = await service.switch_mode(
        user_id,
        "conn-1",
        SwitchModeInput(
            credential_mode=CredentialMode.E2E,
            client_token=token,
        ),
    )
    assert to_e2e.credential_mode is CredentialMode.E2E

    via_e2e = await service.use_credential(
        user_id=user_id,
        connection_id="conn-1",
        purpose="list_transactions",
        client_token=token,
        fn=lambda plaintext: plaintext,
    )
    assert via_e2e == secret


@pytest.mark.asyncio
async def test_negative_server_cannot_decrypt_e2e_without_token() -> None:
    service = CredentialVaultService(
        store=InMemoryConnectionVaultStore(),
        kms=InMemoryKmsProvider(),
    )

    user_id = "user-3"
    token = "token-3"
    blob = E2ECredentialCodec.encrypt("futu-secret", client_token=token)

    await service.create_connection(
        user_id,
        CreateConnectionInput(
            source="futu",
            display_name="Futu",
            connection_id="conn-1",
            credential_mode=CredentialMode.E2E,
            encrypted_blob=blob,
        ),
    )

    before = _failed_counter_value(mode="e2e", reason="missing_token")
    with pytest.raises(VaultValidationError, match="client_token"):
        await service.use_credential(
            user_id=user_id,
            connection_id="conn-1",
            purpose="list_positions",
            client_token=None,
            fn=lambda plaintext: plaintext,
        )
    after = _failed_counter_value(mode="e2e", reason="missing_token")

    assert after >= before + 1.0


@pytest.mark.asyncio
async def test_credential_use_audit_log_omits_plaintext() -> None:
    recorder = _Recorder()
    service = CredentialVaultService(
        store=InMemoryConnectionVaultStore(),
        kms=InMemoryKmsProvider(),
        logger=recorder,
    )

    user_id = "user-4"
    secret = "super-secret-value"
    await service.create_connection(
        user_id,
        CreateConnectionInput(
            source="ibkr",
            display_name="IBKR",
            connection_id="conn-1",
            credential_mode=CredentialMode.SERVER_KEY,
            plaintext_for_server_mode=secret,
        ),
    )

    _ = await service.use_credential(
        user_id=user_id,
        connection_id="conn-1",
        purpose="healthcheck",
        client_token=None,
        fn=lambda plaintext: plaintext,
    )

    assert recorder.events
    event, payload = recorder.events[-1]
    assert event == "credential_use"
    assert payload["user_id"] == user_id
    assert payload["connection_id"] == "conn-1"
    assert payload["mode"] == CredentialMode.SERVER_KEY.value
    assert payload["purpose"] == "healthcheck"
    assert secret not in json.dumps(payload)


class _FakeSnapshot:
    def __init__(self, doc_id: str, payload: dict[str, Any] | None) -> None:
        self.id = doc_id
        self._payload = payload

    @property
    def exists(self) -> bool:
        return self._payload is not None

    def to_dict(self) -> dict[str, Any]:
        return dict(self._payload or {})


class _FakeQuerySnapshot:
    def __init__(self, docs: list[_FakeSnapshot]) -> None:
        self.docs = docs


class _FakeConnectionDocRef:
    def __init__(self, store: dict[str, dict[str, Any]], connection_id: str) -> None:
        self._store = store
        self._connection_id = connection_id

    def set(self, payload: dict[str, Any], merge: bool = False) -> None:
        if merge and self._connection_id in self._store:
            self._store[self._connection_id].update(payload)
            return
        self._store[self._connection_id] = dict(payload)

    def get(self) -> _FakeSnapshot:
        payload = self._store.get(self._connection_id)
        return _FakeSnapshot(self._connection_id, payload)

    def delete(self) -> None:
        self._store.pop(self._connection_id, None)


class _FakeConnectionsCollection:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    def document(self, connection_id: str) -> _FakeConnectionDocRef:
        return _FakeConnectionDocRef(self._store, connection_id)

    def get(self) -> _FakeQuerySnapshot:
        docs = [_FakeSnapshot(doc_id, payload) for doc_id, payload in self._store.items()]
        return _FakeQuerySnapshot(docs)


class _FakeUserDocRef:
    def __init__(self, users: dict[str, dict[str, dict[str, Any]]], user_id: str) -> None:
        self._users = users
        self._user_id = user_id

    def collection(self, name: str) -> _FakeConnectionsCollection:
        if name != "connections":
            msg = f"unexpected collection: {name}"
            raise AssertionError(msg)
        store = self._users.setdefault(self._user_id, {})
        return _FakeConnectionsCollection(store)


class _FakeUsersCollection:
    def __init__(self, users: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._users = users

    def document(self, user_id: str) -> _FakeUserDocRef:
        return _FakeUserDocRef(self._users, user_id)


class _FakeFirestoreClient:
    def __init__(self, users: dict[str, dict[str, dict[str, Any]]] | None = None) -> None:
        self.users = users or {}

    def collection(self, name: str) -> _FakeUsersCollection:
        if name != "users":
            msg = f"unexpected collection: {name}"
            raise AssertionError(msg)
        return _FakeUsersCollection(self.users)


class _FailingFirestoreClient:
    def collection(self, _name: str) -> _FakeUsersCollection:
        raise RuntimeError("firestore unavailable")


@pytest.mark.asyncio
async def test_firestore_store_reads_flutter_shaped_connection_documents() -> None:
    seeded = {
        "u-1": {
            "conn-1": {
                "id": "conn-1",
                "kind": "longbridge",
                "label": "Long Bridge",
                "credentialMode": "e2e",
                "encryptedBlob": "wrapped-blob",
                "enabled": True,
                "updatedAt": "2026-05-18T10:11:12Z",
            }
        }
    }
    store = FirestoreConnectionVaultStore(firestore_client=_FakeFirestoreClient(seeded))

    rows = await store.list_for_user(user_id="u-1")

    assert len(rows) == 1
    row = rows[0]
    assert row.connection_id == "conn-1"
    assert row.source == "longbridge"
    assert row.display_name == "Long Bridge"
    assert row.credential_mode is CredentialMode.E2E
    assert row.e2e_encrypted_blob == "wrapped-blob"


@pytest.mark.asyncio
async def test_firestore_store_put_persists_connection_document_shape() -> None:
    client = _FakeFirestoreClient()
    store = FirestoreConnectionVaultStore(firestore_client=client)
    record = ConnectionCredentialRecord(
        user_id="u-2",
        connection_id="conn-2",
        source="ibkr",
        display_name="Interactive Brokers",
        credential_mode=CredentialMode.SERVER_KEY,
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    saved = await store.put(record)

    assert saved == record
    payload = client.users["u-2"]["conn-2"]
    assert payload["kind"] == "ibkr"
    assert payload["label"] == "Interactive Brokers"
    assert payload["credentialMode"] == "serverKey"
    assert payload["credential_mode"] == "server-key"
    assert payload["server_key_mode"] is True


@pytest.mark.asyncio
async def test_firestore_store_falls_back_to_in_memory_when_firestore_fails() -> None:
    fallback = InMemoryConnectionVaultStore()
    store = FirestoreConnectionVaultStore(
        firestore_client=_FailingFirestoreClient(),
        fallback=fallback,
    )
    record = ConnectionCredentialRecord(
        user_id="u-3",
        connection_id="conn-3",
        source="futu",
        display_name="Futu",
        credential_mode=CredentialMode.E2E,
        e2e_encrypted_blob="blob",
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    _ = await store.put(record)
    loaded = await store.get(user_id="u-3", connection_id="conn-3")

    assert loaded == record
