"""Connection + vault credential management endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.middleware.auth import AuthenticatedUser, current_user
from app.services.dependencies import get_vault_service
from app.services.vault import (
    ConnectionMetadata,
    ConnectionNotFoundError,
    CreateConnectionInput,
    CredentialMode,
    CredentialVaultService,
    DeleteConnectionResult,
    SwitchModeInput,
    VaultError,
    VaultValidationError,
)

router = APIRouter(tags=["connections"])


class CreateConnectionRequest(BaseModel):
    """Request body for creating a connection."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    connection_id: str | None = None
    mode: CredentialMode = CredentialMode.E2E
    encrypted_blob: str | None = None
    plaintext_for_server_mode: str | None = None


class SwitchModeRequest(BaseModel):
    """Request body for switching connection credential mode."""

    model_config = ConfigDict(extra="forbid")

    mode: CredentialMode
    client_token: str | None = None
    encrypted_blob: str | None = None
    plaintext_for_server_mode: str | None = None


@router.post(
    "/connections",
    response_model=ConnectionMetadata,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    payload: CreateConnectionRequest,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    vault: Annotated[CredentialVaultService, Depends(get_vault_service)],
) -> ConnectionMetadata:
    try:
        return await vault.create_connection(
            user.user_id,
            CreateConnectionInput(
                source=payload.source,
                display_name=payload.display_name,
                connection_id=payload.connection_id,
                credential_mode=payload.mode,
                encrypted_blob=payload.encrypted_blob,
                plaintext_for_server_mode=payload.plaintext_for_server_mode,
            ),
        )
    except VaultValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/connections/{connection_id}/mode", response_model=ConnectionMetadata)
async def switch_connection_mode(
    connection_id: str,
    payload: SwitchModeRequest,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    vault: Annotated[CredentialVaultService, Depends(get_vault_service)],
) -> ConnectionMetadata:
    try:
        return await vault.switch_mode(
            user.user_id,
            connection_id,
            SwitchModeInput(
                credential_mode=payload.mode,
                client_token=payload.client_token,
                encrypted_blob=payload.encrypted_blob,
                plaintext_for_server_mode=payload.plaintext_for_server_mode,
            ),
        )
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VaultValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except VaultError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/connections/{connection_id}", response_model=DeleteConnectionResult)
async def delete_connection(
    connection_id: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    vault: Annotated[CredentialVaultService, Depends(get_vault_service)],
) -> DeleteConnectionResult:
    try:
        return await vault.delete_connection(user.user_id, connection_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
