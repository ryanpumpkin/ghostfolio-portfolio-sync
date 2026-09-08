"""Tests for the global exception handlers."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_404_envelope(client: TestClient) -> None:
    resp = client.get("/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "http_404"
    assert body["message"]


def test_validation_envelope(app: FastAPI) -> None:
    from pydantic import BaseModel

    class In(BaseModel):
        n: int

    @app.post("/echo")
    def echo(payload: In) -> dict[str, int]:
        return {"n": payload.n}

    with TestClient(app) as c:
        resp = c.post("/echo", json={"n": "not-an-int"})
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "validation_error"
        assert body["details"] and "errors" in body["details"]


def test_internal_error_envelope(app: FastAPI) -> None:
    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    # raise_server_exceptions=False so the handler runs.
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/boom")
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == "internal_error"
