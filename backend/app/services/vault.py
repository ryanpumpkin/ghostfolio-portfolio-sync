"""Credential vault service for hybrid E2E + server-key credential handling."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from secrets import token_bytes
from typing import Any, Protocol, TypeVar, cast
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from prometheus_client import Counter
from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger


class CredentialMode(StrEnum):
    """How a connection credential is protected at rest."""

    E2E = "e2e"
    SERVER_KEY = "server-key"


class VaultError(Exception):
    """Base vault exception."""


class VaultValidationError(VaultError):
    """Input validation error for vault operations."""


class ConnectionNotFoundError(VaultError):
    """Raised when a connection is not found for a user."""


class VaultDecryptError(VaultError):
    """Raised when decrypting credentials fails."""


class EncryptedSecret(BaseModel):
    """Envelope-encrypted payload protected by a KMS-wrapped DEK."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key_ref: str
    ciphertext_b64: str
    ciphertext_nonce_b64: str
    wrapped_dek_b64: str


class ConnectionCredentialRecord(BaseModel):
    """Connection metadata persisted in the vault store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str
    connection_id: str
    source: str
    display_name: str
    credential_mode: CredentialMode
    e2e_encrypted_blob: str | None = None
    server_encrypted_secret: EncryptedSecret | None = None
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ConnectionMetadata(BaseModel):
    """Redacted metadata returned by API endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: str
    source: str
    display_name: str
    credential_mode: CredentialMode
    enabled: bool
    has_stored_credential: bool
    updated_at: datetime


class CreateConnectionInput(BaseModel):
    """Input contract for creating a new connection metadata record."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    connection_id: str | None = None
    credential_mode: CredentialMode = CredentialMode.E2E
    encrypted_blob: str | None = None
    plaintext_for_server_mode: str | None = None


class SwitchModeInput(BaseModel):
    """Input contract for switching credential mode of a connection."""

    model_config = ConfigDict(extra="forbid")

    credential_mode: CredentialMode
    client_token: str | None = None
    encrypted_blob: str | None = None
    plaintext_for_server_mode: str | None = None


class DeleteConnectionResult(BaseModel):
    """Return shape for connection deletion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: str
    deleted: bool


class KmsProvider(Protocol):
    """Key management provider abstraction used by server-key mode."""

    def encrypt(self, plaintext: bytes, *, aad: bytes | None = None) -> EncryptedSecret: ...

    def decrypt(self, encrypted: EncryptedSecret, *, aad: bytes | None = None) -> bytes: ...

    def rotate(self, *, previous_key_ref: str | None = None) -> str: ...


class GcpKmsClient(Protocol):
    """Minimal client API needed for a GCP KMS integration."""

    def encrypt(self, *, key_name: str, plaintext: bytes, aad: bytes | None = None) -> bytes: ...

    def decrypt(self, *, key_name: str, ciphertext: bytes, aad: bytes | None = None) -> bytes: ...

    def rotate(self, *, key_name: str) -> str: ...


class GcpKmsProvider:
    """Envelope encryption using GCP KMS as the KEK manager."""

    def __init__(self, *, key_name: str, client: GcpKmsClient) -> None:
        self._key_name = key_name
        self._client = client

    def encrypt(self, plaintext: bytes, *, aad: bytes | None = None) -> EncryptedSecret:
        dek = token_bytes(32)
        nonce = token_bytes(12)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
        wrapped_dek = self._client.encrypt(key_name=self._key_name, plaintext=dek, aad=aad)
        return EncryptedSecret(
            key_ref=self._key_name,
            ciphertext_b64=_b64e(ciphertext),
            ciphertext_nonce_b64=_b64e(nonce),
            wrapped_dek_b64=_b64e(wrapped_dek),
        )

    def decrypt(self, encrypted: EncryptedSecret, *, aad: bytes | None = None) -> bytes:
        wrapped_dek = _b64d(encrypted.wrapped_dek_b64)
        dek = self._client.decrypt(key_name=encrypted.key_ref, ciphertext=wrapped_dek, aad=aad)
        nonce = _b64d(encrypted.ciphertext_nonce_b64)
        ciphertext = _b64d(encrypted.ciphertext_b64)
        return AESGCM(dek).decrypt(nonce, ciphertext, aad)

    def rotate(self, *, previous_key_ref: str | None = None) -> str:
        key_name = previous_key_ref or self._key_name
        return self._client.rotate(key_name=key_name)


class FileBackedMasterKeyKmsProvider:
    """Fallback KMS for self-hosted setups using a local master key file.

    This is less secure than a managed KMS because key custody and rotation are
    file-based and host-dependent.
    """

    def __init__(self, *, key_file: Path, key_ref: str | None = None) -> None:
        self._key_file = key_file
        self._key_ref = key_ref or f"file://{key_file}"
        self._master_key = self._load_or_create_master_key()

    def encrypt(self, plaintext: bytes, *, aad: bytes | None = None) -> EncryptedSecret:
        dek = token_bytes(32)
        nonce = token_bytes(12)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)

        wrap_nonce = token_bytes(12)
        wrapped_dek = AESGCM(self._master_key).encrypt(wrap_nonce, dek, aad)
        wrapped_bundle = json.dumps(
            {
                "wrap_nonce_b64": _b64e(wrap_nonce),
                "wrapped_dek_b64": _b64e(wrapped_dek),
            },
            separators=(",", ":"),
        ).encode("utf-8")

        return EncryptedSecret(
            key_ref=self._key_ref,
            ciphertext_b64=_b64e(ciphertext),
            ciphertext_nonce_b64=_b64e(nonce),
            wrapped_dek_b64=_b64e(wrapped_bundle),
        )

    def decrypt(self, encrypted: EncryptedSecret, *, aad: bytes | None = None) -> bytes:
        wrapped_bundle = json.loads(_b64d(encrypted.wrapped_dek_b64).decode("utf-8"))
        wrap_nonce = _b64d(_require_str(wrapped_bundle, "wrap_nonce_b64"))
        wrapped_dek = _b64d(_require_str(wrapped_bundle, "wrapped_dek_b64"))
        dek = AESGCM(self._master_key).decrypt(wrap_nonce, wrapped_dek, aad)
        nonce = _b64d(encrypted.ciphertext_nonce_b64)
        ciphertext = _b64d(encrypted.ciphertext_b64)
        return AESGCM(dek).decrypt(nonce, ciphertext, aad)

    def rotate(self, *, previous_key_ref: str | None = None) -> str:
        _ = previous_key_ref
        self._master_key = self._load_or_create_master_key(force_rotate=True)
        return self._key_ref

    def _load_or_create_master_key(self, *, force_rotate: bool = False) -> bytes:
        if self._key_file.exists() and not force_rotate:
            raw = self._key_file.read_text(encoding="utf-8").strip()
            key = _b64d(raw)
            if len(key) != 32:
                msg = "File-backed master key must be 32 bytes"
                raise VaultValidationError(msg)
            return key

        key = token_bytes(32)
        self._key_file.parent.mkdir(parents=True, exist_ok=True)
        self._key_file.write_text(_b64e(key), encoding="utf-8")
        os.chmod(self._key_file, 0o600)
        return key


class InMemoryKmsProvider:
    """Deterministic KMS provider used by tests and local development."""

    def __init__(self, key_ref: str = "in-memory-kms") -> None:
        self._key_ref = key_ref
        self._master_key = token_bytes(32)

    def encrypt(self, plaintext: bytes, *, aad: bytes | None = None) -> EncryptedSecret:
        dek = token_bytes(32)
        nonce = token_bytes(12)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)

        wrap_nonce = token_bytes(12)
        wrapped_dek = AESGCM(self._master_key).encrypt(wrap_nonce, dek, aad)
        wrapped_bundle = json.dumps(
            {
                "wrap_nonce_b64": _b64e(wrap_nonce),
                "wrapped_dek_b64": _b64e(wrapped_dek),
            },
            separators=(",", ":"),
        ).encode("utf-8")

        return EncryptedSecret(
            key_ref=self._key_ref,
            ciphertext_b64=_b64e(ciphertext),
            ciphertext_nonce_b64=_b64e(nonce),
            wrapped_dek_b64=_b64e(wrapped_bundle),
        )

    def decrypt(self, encrypted: EncryptedSecret, *, aad: bytes | None = None) -> bytes:
        # Re-implement locally so this provider is independent from filesystem.
        wrapped_bundle = json.loads(_b64d(encrypted.wrapped_dek_b64).decode("utf-8"))
        wrap_nonce = _b64d(_require_str(wrapped_bundle, "wrap_nonce_b64"))
        wrapped_dek = _b64d(_require_str(wrapped_bundle, "wrapped_dek_b64"))
        dek = AESGCM(self._master_key).decrypt(wrap_nonce, wrapped_dek, aad)
        nonce = _b64d(encrypted.ciphertext_nonce_b64)
        ciphertext = _b64d(encrypted.ciphertext_b64)
        return AESGCM(dek).decrypt(nonce, ciphertext, aad)

    def rotate(self, *, previous_key_ref: str | None = None) -> str:
        _ = previous_key_ref
        self._master_key = token_bytes(32)
        return self._key_ref


class ConnectionVaultStore(Protocol):
    """Storage backend for per-user connection credential metadata."""

    async def put(self, record: ConnectionCredentialRecord) -> ConnectionCredentialRecord: ...

    async def get(self, *, user_id: str, connection_id: str) -> ConnectionCredentialRecord | None: ...

    async def list_for_user(self, *, user_id: str) -> list[ConnectionCredentialRecord]: ...

    async def delete(self, *, user_id: str, connection_id: str) -> ConnectionCredentialRecord | None: ...


class InMemoryConnectionVaultStore:
    """In-memory storage implementation used by tests and bootstrap."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ConnectionCredentialRecord] = {}

    async def put(self, record: ConnectionCredentialRecord) -> ConnectionCredentialRecord:
        self._rows[(record.user_id, record.connection_id)] = record
        return record

    async def get(self, *, user_id: str, connection_id: str) -> ConnectionCredentialRecord | None:
        return self._rows.get((user_id, connection_id))

    async def list_for_user(self, *, user_id: str) -> list[ConnectionCredentialRecord]:
        return [record for (uid, _), record in self._rows.items() if uid == user_id]

    async def delete(self, *, user_id: str, connection_id: str) -> ConnectionCredentialRecord | None:
        return self._rows.pop((user_id, connection_id), None)


class FirestoreConnectionVaultStore:
    """Firestore-backed connection vault store with safe in-memory fallback.

    The fallback preserves local-dev/test behavior when Firebase Admin is not
    configured, while production deployments read and write the canonical
    `users/{uid}/connections/{cid}` documents.
    """

    def __init__(
        self,
        *,
        firestore_client: Any | None = None,
        fallback: ConnectionVaultStore | None = None,
    ) -> None:
        self._firestore_client = firestore_client
        self._fallback = fallback or InMemoryConnectionVaultStore()
        self._log = logging.getLogger("mbp.vault")

    async def put(self, record: ConnectionCredentialRecord) -> ConnectionCredentialRecord:
        client = self._client()
        if client is None:
            return await self._fallback.put(record)

        payload = self._to_firestore_payload(record)
        try:
            doc = self._connection_doc(client, user_id=record.user_id, connection_id=record.connection_id)
            await _run_blocking(doc.set, payload, merge=True)
            return record
        except Exception:
            return await self._fallback.put(record)

    async def get(self, *, user_id: str, connection_id: str) -> ConnectionCredentialRecord | None:
        client = self._client()
        if client is None:
            return await self._fallback.get(user_id=user_id, connection_id=connection_id)

        try:
            doc = self._connection_doc(client, user_id=user_id, connection_id=connection_id)
            snap = await _run_blocking(doc.get)
            if not getattr(snap, "exists", False):
                return None
            data = _safe_to_dict(snap)
            return self._from_firestore_doc(
                user_id=user_id,
                connection_id=connection_id,
                payload=data,
            )
        except Exception:
            return await self._fallback.get(user_id=user_id, connection_id=connection_id)

    async def list_for_user(self, *, user_id: str) -> list[ConnectionCredentialRecord]:
        client = self._client()
        if client is None:
            self._log.warning(
                "list_for_user: Firestore client is None — falling back to in-memory store",
            )
            return await self._fallback.list_for_user(user_id=user_id)

        try:
            col = self._connections_collection(client, user_id=user_id)
            snap = await _run_blocking(col.get)
            # Modern google-cloud-firestore returns a `QueryResultsList`
            # from `CollectionReference.get()` — a list subclass whose
            # iteration yields the documents directly. Older
            # `QuerySnapshot` shape exposes documents via `.docs`. Try the
            # iterable shape first, then fall back to `.docs` for safety.
            docs: list[Any]
            try:
                docs = list(snap)
            except TypeError:
                docs = list(getattr(snap, "docs", []) or [])
            if self._log.isEnabledFor(logging.DEBUG):
                self._log.debug(
                    "list_for_user uid=%s firestore_docs=%d",
                    user_id,
                    len(docs),
                )
            out: list[ConnectionCredentialRecord] = []
            for doc in docs:
                payload = _safe_to_dict(doc)
                connection_id: str | None
                try:
                    connection_id = doc.id
                except AttributeError:
                    connection_id = None
                out.append(
                    self._from_firestore_doc(
                        user_id=user_id,
                        connection_id=connection_id,
                        payload=payload,
                    )
                )
            return out
        except Exception as exc:
            import traceback
            self._log.error(
                "list_for_user uid=%s firestore_read_failed: %s\n%s",
                user_id,
                exc,
                traceback.format_exc(),
            )
            return await self._fallback.list_for_user(user_id=user_id)

    async def delete(self, *, user_id: str, connection_id: str) -> ConnectionCredentialRecord | None:
        existing = await self.get(user_id=user_id, connection_id=connection_id)
        if existing is None:
            return None

        client = self._client()
        if client is None:
            await self._fallback.delete(user_id=user_id, connection_id=connection_id)
            return existing

        try:
            doc = self._connection_doc(client, user_id=user_id, connection_id=connection_id)
            await _run_blocking(doc.delete)
            return existing
        except Exception:
            await self._fallback.delete(user_id=user_id, connection_id=connection_id)
            return existing

    def _client(self) -> Any | None:
        if self._firestore_client is not None:
            return self._firestore_client
        try:
            from firebase_admin import firestore

            return firestore.client()
        except Exception:
            return None

    @staticmethod
    def _connections_collection(client: Any, *, user_id: str) -> Any:
        return client.collection("users").document(user_id).collection("connections")

    @classmethod
    def _connection_doc(cls, client: Any, *, user_id: str, connection_id: str) -> Any:
        return cls._connections_collection(client, user_id=user_id).document(connection_id)

    @staticmethod
    def _to_firestore_payload(record: ConnectionCredentialRecord) -> dict[str, Any]:
        mode = (
            "serverKey"
            if record.credential_mode is CredentialMode.SERVER_KEY
            else "e2e"
        )
        payload: dict[str, Any] = {
            "id": record.connection_id,
            "kind": record.source,
            "source": record.source,
            "label": record.display_name,
            "display_name": record.display_name,
            "credentialMode": mode,
            "credential_mode": record.credential_mode.value,
            "enabled": record.enabled,
            "server_key_mode": record.credential_mode is CredentialMode.SERVER_KEY,
            "encryptedBlob": record.e2e_encrypted_blob,
            "e2e_encrypted_blob": record.e2e_encrypted_blob,
            "server_encrypted_secret": (
                record.server_encrypted_secret.model_dump()
                if record.server_encrypted_secret is not None
                else None
            ),
            "createdAt": record.created_at,
            "updatedAt": record.updated_at,
        }
        return payload

    @staticmethod
    def _from_firestore_doc(
        *,
        user_id: str,
        connection_id: str | None,
        payload: dict[str, Any],
    ) -> ConnectionCredentialRecord:
        cid = (
            _first_str(
                connection_id,
                payload.get("id"),
                payload.get("connection_id"),
            )
            or ""
        )
        source = _first_str(payload.get("source"), payload.get("kind")) or "manual"
        display_name = (
            _first_str(
                payload.get("display_name"),
                payload.get("displayName"),
                payload.get("label"),
            )
            or cid
        )
        enabled = bool(payload.get("enabled", True))

        mode = _parse_credential_mode(
            payload.get("credential_mode"),
            payload.get("credentialMode"),
            payload.get("mode"),
            payload.get("server_key_mode"),
        )

        e2e_blob = _first_str(
            payload.get("e2e_encrypted_blob"),
            payload.get("encrypted_blob"),
            payload.get("encryptedBlob"),
        )

        server_secret = _parse_server_secret(payload.get("server_encrypted_secret"))
        created_at = _coerce_datetime(payload.get("created_at") or payload.get("createdAt"))
        updated_at = _coerce_datetime(payload.get("updated_at") or payload.get("updatedAt"))

        return ConnectionCredentialRecord(
            user_id=user_id,
            connection_id=cid,
            source=source,
            display_name=display_name,
            credential_mode=mode,
            e2e_encrypted_blob=e2e_blob,
            server_encrypted_secret=server_secret,
            enabled=enabled,
            created_at=created_at,
            updated_at=updated_at,
        )


class E2ECredentialCodec:
    """Codec for token-protected E2E credential blobs."""

    @staticmethod
    def encrypt(plaintext: str, *, client_token: str) -> str:
        if not client_token:
            msg = "client_token is required"
            raise VaultValidationError(msg)
        salt = token_bytes(16)
        nonce = token_bytes(12)
        key = _derive_token_key(client_token, salt)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
        blob = {
            "salt_b64": _b64e(salt),
            "nonce_b64": _b64e(nonce),
            "ciphertext_b64": _b64e(ciphertext),
            "alg": "aes-256-gcm+pbkdf2-sha256",
        }
        return json.dumps(blob, separators=(",", ":"))

    @staticmethod
    def decrypt(blob: str, *, client_token: str) -> str:
        if not client_token:
            msg = "client_token is required"
            raise VaultValidationError(msg)
        try:
            payload = json.loads(blob)
            salt = _b64d(_require_str(payload, "salt_b64"))
            nonce = _b64d(_require_str(payload, "nonce_b64"))
            ciphertext = _b64d(_require_str(payload, "ciphertext_b64"))
        except Exception as exc:
            raise VaultDecryptError("Malformed E2E blob") from exc

        try:
            key = _derive_token_key(client_token, salt)
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
            return plaintext.decode("utf-8")
        except Exception as exc:
            raise VaultDecryptError("E2E decrypt failed") from exc


FAILED_DECRYPT_COUNTER = Counter(
    "mbp_vault_failed_decrypt_total",
    "Failed decrypt attempts in vault flows.",
    labelnames=("mode", "reason"),
)

T = TypeVar("T")
CredentialUseFn = Callable[[str], T | Awaitable[T]]


class CredentialVaultService:
    """Facade for connection credential CRUD + in-memory credential use."""

    def __init__(
        self,
        *,
        store: ConnectionVaultStore,
        kms: KmsProvider,
        logger: Any | None = None,
    ) -> None:
        self._store = store
        self._kms = kms
        self._log = logger or get_logger(__name__)

    async def create_connection(self, user_id: str, data: CreateConnectionInput) -> ConnectionMetadata:
        connection_id = data.connection_id or str(uuid4())
        mode = data.credential_mode

        if mode is CredentialMode.E2E:
            if data.plaintext_for_server_mode:
                msg = "plaintext_for_server_mode is not allowed for e2e mode"
                raise VaultValidationError(msg)
            if not data.encrypted_blob:
                msg = "encrypted_blob is required for e2e mode"
                raise VaultValidationError(msg)
            record = ConnectionCredentialRecord(
                user_id=user_id,
                connection_id=connection_id,
                source=data.source,
                display_name=data.display_name,
                credential_mode=mode,
                e2e_encrypted_blob=data.encrypted_blob,
            )
        else:
            plaintext = data.plaintext_for_server_mode
            if not plaintext:
                msg = "plaintext_for_server_mode is required for server-key mode"
                raise VaultValidationError(msg)
            if data.encrypted_blob:
                msg = "encrypted_blob is not allowed for server-key mode"
                raise VaultValidationError(msg)
            secret = self._kms.encrypt(
                plaintext.encode("utf-8"),
                aad=self._aad(user_id=user_id, connection_id=connection_id),
            )
            record = ConnectionCredentialRecord(
                user_id=user_id,
                connection_id=connection_id,
                source=data.source,
                display_name=data.display_name,
                credential_mode=mode,
                server_encrypted_secret=secret,
            )

        stored = await self._store.put(record)
        return _to_metadata(stored)

    async def switch_mode(
        self,
        user_id: str,
        connection_id: str,
        data: SwitchModeInput,
    ) -> ConnectionMetadata:
        existing = await self._get_required(user_id=user_id, connection_id=connection_id)
        if existing.credential_mode is data.credential_mode:
            return _to_metadata(existing)

        plaintext = self._resolve_switch_plaintext(user_id=user_id, existing=existing, data=data)

        now = datetime.now(UTC)
        if data.credential_mode is CredentialMode.E2E:
            if data.encrypted_blob:
                e2e_blob = data.encrypted_blob
            else:
                token = data.client_token
                if not token:
                    msg = "client_token is required to switch into e2e mode"
                    raise VaultValidationError(msg)
                e2e_blob = E2ECredentialCodec.encrypt(plaintext, client_token=token)

            updated = existing.model_copy(
                update={
                    "credential_mode": CredentialMode.E2E,
                    "e2e_encrypted_blob": e2e_blob,
                    "server_encrypted_secret": None,
                    "updated_at": now,
                }
            )
        else:
            secret = self._kms.encrypt(
                plaintext.encode("utf-8"),
                aad=self._aad(user_id=user_id, connection_id=connection_id),
            )
            updated = existing.model_copy(
                update={
                    "credential_mode": CredentialMode.SERVER_KEY,
                    "e2e_encrypted_blob": None,
                    "server_encrypted_secret": secret,
                    "updated_at": now,
                }
            )

        saved = await self._store.put(updated)
        return _to_metadata(saved)

    async def delete_connection(self, user_id: str, connection_id: str) -> DeleteConnectionResult:
        existing = await self._get_required(user_id=user_id, connection_id=connection_id)

        if existing.server_encrypted_secret is not None:
            self._kms.rotate(previous_key_ref=existing.server_encrypted_secret.key_ref)

        deleted = await self._store.delete(user_id=user_id, connection_id=connection_id)
        return DeleteConnectionResult(connection_id=connection_id, deleted=deleted is not None)

    async def use_credential(
        self,
        *,
        user_id: str,
        connection_id: str,
        purpose: str,
        client_token: str | None,
        fn: CredentialUseFn[T],
    ) -> T:
        existing = await self._get_required(user_id=user_id, connection_id=connection_id)
        plaintext = self._decrypt_for_use(
            user_id=user_id,
            connection_id=connection_id,
            mode=existing.credential_mode,
            e2e_blob=existing.e2e_encrypted_blob,
            server_secret=existing.server_encrypted_secret,
            client_token=client_token,
        )

        self._log.info(
            "credential_use",
            user_id=user_id,
            connection_id=connection_id,
            mode=existing.credential_mode.value,
            purpose=purpose,
        )

        try:
            result = fn(plaintext)
            if inspect.isawaitable(result):
                awaited = cast(Awaitable[T], result)
                return await awaited
            return result
        finally:
            plaintext = ""

    async def get_connection_metadata(self, user_id: str, connection_id: str) -> ConnectionMetadata:
        existing = await self._get_required(user_id=user_id, connection_id=connection_id)
        return _to_metadata(existing)

    async def _get_required(self, *, user_id: str, connection_id: str) -> ConnectionCredentialRecord:
        row = await self._store.get(user_id=user_id, connection_id=connection_id)
        if row is None:
            msg = "connection not found"
            raise ConnectionNotFoundError(msg)
        return row

    def _resolve_switch_plaintext(
        self,
        *,
        user_id: str,
        existing: ConnectionCredentialRecord,
        data: SwitchModeInput,
    ) -> str:
        if data.plaintext_for_server_mode:
            return data.plaintext_for_server_mode

        return self._decrypt_for_use(
            user_id=user_id,
            connection_id=existing.connection_id,
            mode=existing.credential_mode,
            e2e_blob=existing.e2e_encrypted_blob,
            server_secret=existing.server_encrypted_secret,
            client_token=data.client_token,
        )

    def _decrypt_for_use(
        self,
        *,
        user_id: str,
        connection_id: str,
        mode: CredentialMode,
        e2e_blob: str | None,
        server_secret: EncryptedSecret | None,
        client_token: str | None,
    ) -> str:
        if mode is CredentialMode.E2E:
            if not client_token:
                FAILED_DECRYPT_COUNTER.labels(mode=mode.value, reason="missing_token").inc()
                msg = "client_token is required for e2e credential access"
                raise VaultValidationError(msg)
            if not e2e_blob:
                FAILED_DECRYPT_COUNTER.labels(mode=mode.value, reason="missing_blob").inc()
                msg = "missing e2e encrypted blob"
                raise VaultDecryptError(msg)
            try:
                return E2ECredentialCodec.decrypt(e2e_blob, client_token=client_token)
            except VaultError:
                FAILED_DECRYPT_COUNTER.labels(mode=mode.value, reason="decrypt_error").inc()
                raise

        if server_secret is None:
            FAILED_DECRYPT_COUNTER.labels(mode=mode.value, reason="missing_blob").inc()
            msg = "missing server-key encrypted blob"
            raise VaultDecryptError(msg)

        try:
            plaintext = self._kms.decrypt(
                server_secret,
                aad=self._aad(user_id=user_id, connection_id=connection_id),
            )
            return plaintext.decode("utf-8")
        except Exception as exc:
            FAILED_DECRYPT_COUNTER.labels(mode=mode.value, reason="decrypt_error").inc()
            raise VaultDecryptError("server-key decrypt failed") from exc

    @staticmethod
    def _aad(*, user_id: str, connection_id: str) -> bytes:
        return f"{user_id}:{connection_id}".encode()


def build_kms_provider(*, provider_name: str | None, key_id: str | None) -> KmsProvider:
    """Factory for runtime KMS provider selection."""

    provider = (provider_name or "file").strip().lower()
    if provider in {"gcp", "gcp-kms"}:
        msg = "GcpKmsProvider requires an injected GcpKmsClient; wiring pending"
        raise NotImplementedError(msg)

    key_file = Path(key_id or ".secrets/mbp-master.key")
    return FileBackedMasterKeyKmsProvider(key_file=key_file)


def _derive_token_key(client_token: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        client_token.encode("utf-8"),
        salt,
        210_000,
        dklen=32,
    )


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(raw: str) -> bytes:
    return base64.b64decode(raw.encode("ascii"))


async def _run_blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    if inspect.iscoroutinefunction(fn):
        async_fn = cast(Callable[..., Awaitable[T]], fn)
        return await async_fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


def _safe_to_dict(snapshot: Any) -> dict[str, Any]:
    to_dict = getattr(snapshot, "to_dict", None)
    if not callable(to_dict):
        return {}
    payload = to_dict()
    if isinstance(payload, dict):
        return cast(dict[str, Any], payload)
    return {}


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _parse_credential_mode(*values: Any) -> CredentialMode:
    for value in values:
        if isinstance(value, bool):
            return CredentialMode.SERVER_KEY if value else CredentialMode.E2E
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"server-key", "serverkey", "server", "kms"}:
            return CredentialMode.SERVER_KEY
        if normalized in {"e2e", "client"}:
            return CredentialMode.E2E
    return CredentialMode.E2E


def _parse_server_secret(raw: Any) -> EncryptedSecret | None:
    if raw is None:
        return None
    if isinstance(raw, EncryptedSecret):
        return raw
    if isinstance(raw, dict):
        try:
            return EncryptedSecret.model_validate(raw)
        except Exception:
            return None
    return None


def _coerce_datetime(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=UTC)
        return raw.astimezone(UTC)

    if isinstance(raw, str) and raw.strip():
        candidate = raw.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _to_metadata(record: ConnectionCredentialRecord) -> ConnectionMetadata:
    has_credential = bool(record.e2e_encrypted_blob or record.server_encrypted_secret)
    return ConnectionMetadata(
        connection_id=record.connection_id,
        source=record.source,
        display_name=record.display_name,
        credential_mode=record.credential_mode,
        enabled=record.enabled,
        has_stored_credential=has_credential,
        updated_at=record.updated_at,
    )


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        msg = f"Missing or non-string field: {key}"
        raise VaultValidationError(msg)
    return value
