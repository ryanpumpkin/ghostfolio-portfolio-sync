"""Tests for the request-id middleware and tracing helpers."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.tracing import (
    REQUEST_ID_HEADER,
    current_request_id,
    new_request_id,
    set_request_id,
)


def test_generates_request_id_when_missing(client: TestClient) -> None:
    resp = client.get("/healthz")
    rid = resp.headers.get(REQUEST_ID_HEADER)
    assert rid
    assert len(rid) >= 16


def test_propagates_inbound_request_id(client: TestClient) -> None:
    resp = client.get("/healthz", headers={REQUEST_ID_HEADER: "abc-123"})
    assert resp.headers[REQUEST_ID_HEADER] == "abc-123"


def test_error_responses_include_request_id(client: TestClient) -> None:
    resp = client.get("/v1/whoami", headers={REQUEST_ID_HEADER: "trace-xyz"})
    assert resp.status_code == 401
    assert resp.headers[REQUEST_ID_HEADER] == "trace-xyz"
    assert resp.json()["request_id"] == "trace-xyz"


def test_tracing_helpers() -> None:
    rid = new_request_id()
    assert isinstance(rid, str) and rid
    set_request_id(rid)
    assert current_request_id() == rid
