"""Tests for wrapped credential header parsing."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from app.api.credential_context import (
    WrappedCredentialsContext,
    _parse_connection_token_map,
    parse_wrapped_credentials_header,
)


def test_parse_connection_token_map_accepts_json_map() -> None:
    parsed = _parse_connection_token_map('{"c1":"tok-1","c2":"tok-2"}')
    assert parsed == {"c1": "tok-1", "c2": "tok-2"}


def test_parse_connection_token_map_accepts_b64_json_map() -> None:
    encoded = base64.b64encode(b'{"c1":"tok-1"}').decode("ascii")
    parsed = _parse_connection_token_map(encoded)
    assert parsed == {"c1": "tok-1"}


def test_parse_connection_token_map_ignores_single_wrap_envelope() -> None:
    envelope = {"v": 1, "expiresAt": 1, "ct": "abc"}
    encoded = base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")
    parsed = _parse_connection_token_map(encoded)
    assert parsed is None


async def test_dependency_sets_request_state_for_absent_header() -> None:
    fake_request = SimpleNamespace(state=SimpleNamespace())
    context = await parse_wrapped_credentials_header(fake_request)  # type: ignore[arg-type]
    assert isinstance(context, WrappedCredentialsContext)
    assert context.shared_token is None
    assert context.tokens_by_connection == {}
    assert fake_request.state.wrapped_credentials is context
