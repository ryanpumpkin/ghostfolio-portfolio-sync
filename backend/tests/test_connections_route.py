"""Route tests for connection/vault API endpoints."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.dependencies import get_vault_service
from app.services.vault import (
    CredentialVaultService,
    E2ECredentialCodec,
    InMemoryConnectionVaultStore,
    InMemoryKmsProvider,
)


def _build_client(app: FastAPI) -> TestClient:
    vault = CredentialVaultService(
        store=InMemoryConnectionVaultStore(),
        kms=InMemoryKmsProvider(),
    )
    app.dependency_overrides[get_vault_service] = lambda: vault
    return TestClient(app)


def test_post_connections_accepts_e2e_blob(app: FastAPI) -> None:
    with _build_client(app) as client:
        token = "request-token"
        resp = client.post(
            "/v1/connections",
            headers={"Authorization": "Bearer good-token"},
            json={
                "source": "binance",
                "display_name": "Binance",
                "connection_id": "conn-1",
                "mode": "e2e",
                "encrypted_blob": E2ECredentialCodec.encrypt("secret", client_token=token),
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["connection_id"] == "conn-1"
    assert body["credential_mode"] == "e2e"
    assert body["has_stored_credential"] is True


def test_post_connections_accepts_server_key_plaintext(app: FastAPI) -> None:
    with _build_client(app) as client:
        resp = client.post(
            "/v1/connections",
            headers={"Authorization": "Bearer good-token"},
            json={
                "source": "ibkr",
                "display_name": "IBKR",
                "connection_id": "conn-2",
                "mode": "server-key",
                "plaintext_for_server_mode": "ibkr-secret",
            },
        )

    assert resp.status_code == 201
    assert resp.json()["credential_mode"] == "server-key"


def test_post_connections_rejects_mode_mismatch_payload(app: FastAPI) -> None:
    with _build_client(app) as client:
        resp = client.post(
            "/v1/connections",
            headers={"Authorization": "Bearer good-token"},
            json={
                "source": "futu",
                "display_name": "Futu",
                "connection_id": "conn-3",
                "mode": "e2e",
                "plaintext_for_server_mode": "not-allowed",
            },
        )

    assert resp.status_code == 400
    assert "not allowed" in resp.json()["message"]


def test_patch_connection_mode_reencrypts(app: FastAPI) -> None:
    token = "short-lived"
    with _build_client(app) as client:
        create = client.post(
            "/v1/connections",
            headers={"Authorization": "Bearer good-token"},
            json={
                "source": "longbridge",
                "display_name": "LongBridge",
                "connection_id": "conn-4",
                "mode": "e2e",
                "encrypted_blob": E2ECredentialCodec.encrypt("lb-secret", client_token=token),
            },
        )
        assert create.status_code == 201

        patch = client.patch(
            "/v1/connections/conn-4/mode",
            headers={"Authorization": "Bearer good-token"},
            json={
                "mode": "server-key",
                "client_token": token,
            },
        )

    assert patch.status_code == 200
    assert patch.json()["credential_mode"] == "server-key"


def test_patch_connection_mode_requires_mode_appropriate_inputs(app: FastAPI) -> None:
    token = "short-lived"
    with _build_client(app) as client:
        create = client.post(
            "/v1/connections",
            headers={"Authorization": "Bearer good-token"},
            json={
                "source": "binance",
                "display_name": "Binance",
                "connection_id": "conn-5",
                "mode": "e2e",
                "encrypted_blob": E2ECredentialCodec.encrypt("b-secret", client_token=token),
            },
        )
        assert create.status_code == 201

        patch = client.patch(
            "/v1/connections/conn-5/mode",
            headers={"Authorization": "Bearer good-token"},
            json={
                "mode": "server-key",
            },
        )

    assert patch.status_code == 400
    assert "client_token" in patch.json()["message"]


def test_delete_connection_wipes_blob_and_revokes_refs(app: FastAPI) -> None:
    with _build_client(app) as client:
        create = client.post(
            "/v1/connections",
            headers={"Authorization": "Bearer good-token"},
            json={
                "source": "ibkr",
                "display_name": "IBKR",
                "connection_id": "conn-6",
                "mode": "server-key",
                "plaintext_for_server_mode": "secret",
            },
        )
        assert create.status_code == 201

        delete = client.delete(
            "/v1/connections/conn-6",
            headers={"Authorization": "Bearer good-token"},
        )
        assert delete.status_code == 200
        assert delete.json() == {"connection_id": "conn-6", "deleted": True}

        missing = client.patch(
            "/v1/connections/conn-6/mode",
            headers={"Authorization": "Bearer good-token"},
            json={"mode": "e2e", "encrypted_blob": "abc"},
        )

    assert missing.status_code == 404
