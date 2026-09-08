"""Request-scoped wrapped credential header parsing."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Header, Request

MBP_CREDS_HEADER = "X-MBP-Creds"
MBP_CREDS_KEY_HEADER = "X-MBP-Creds-Key"


@dataclass(slots=True)
class WrappedCredentialsContext:
    """Wrapped credential header payload extracted for one request."""

    shared_token: str | None = None
    tokens_by_connection: dict[str, str] = field(default_factory=dict)
    key_b64: str | None = None

    def token_for(self, connection_id: str) -> str | None:
        return self.tokens_by_connection.get(connection_id) or self.shared_token

    def key_bytes(self) -> bytes | None:
        if self.key_b64 is None:
            return None
        try:
            return base64.b64decode(self.key_b64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error):
            return None


async def parse_wrapped_credentials_header(
    request: Request,
    x_mbp_creds: Annotated[str | None, Header(alias=MBP_CREDS_HEADER)] = None,
    x_mbp_creds_key: Annotated[str | None, Header(alias=MBP_CREDS_KEY_HEADER)] = None,
) -> WrappedCredentialsContext:
    """Parse wrapped-credential headers and stash them on request.state.

    Header formats supported for `X-MBP-Creds`:
    1. Single wrapped token (base64 envelope).
    2. JSON object mapping connection ids to wrapped tokens.
    3. Base64-encoded JSON object mapping connection ids to wrapped tokens.
    """
    parsed_map = _parse_connection_token_map(x_mbp_creds)
    context = WrappedCredentialsContext(
        shared_token=None if parsed_map is not None else _normalize_header_value(x_mbp_creds),
        tokens_by_connection=parsed_map or {},
        key_b64=_normalize_header_value(x_mbp_creds_key),
    )
    request.state.wrapped_credentials = context
    return context


def _normalize_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed if trimmed else None


def _parse_connection_token_map(value: str | None) -> dict[str, str] | None:
    raw = _normalize_header_value(value)
    if raw is None:
        return None

    payload = _try_load_json_object(raw)
    if payload is None:
        payload = _try_load_b64_json_object(raw)
    if payload is None:
        return None

    # Guard: wrapped-envelope objects are not connection maps.
    if {"v", "expiresAt", "ct"}.issubset(payload):
        return None

    tokens: dict[str, str] = {}
    for key, token in payload.items():
        if not isinstance(key, str) or not isinstance(token, str):
            return None
        key_clean = key.strip()
        token_clean = token.strip()
        if not key_clean or not token_clean:
            return None
        tokens[key_clean] = token_clean
    return tokens if tokens else None


def _try_load_json_object(raw: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _try_load_b64_json_object(raw: str) -> dict[str, Any] | None:
    try:
        decoded_raw = base64.b64decode(raw.encode("ascii"), validate=True)
        decoded = json.loads(decoded_raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded

