"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.main import create_app
from app.middleware.auth import TokenVerifier, get_token_verifier

# Ensure the `backend` coverage target resolves to a concrete module.
sys.path.insert(0, str(Path(__file__).parent))
import backend as _coverage_backend  # noqa: F401


class FakeVerifier:
    """Deterministic stand-in for FirebaseTokenVerifier in tests."""

    def __init__(self, tokens: dict[str, dict[str, Any]] | None = None) -> None:
        self.tokens = tokens or {
            "good-token": {"uid": "user-123", "email": "a@b.com"},
        }

    def verify(self, id_token: str) -> dict[str, Any]:
        if id_token not in self.tokens:
            raise ValueError("invalid token")
        return self.tokens[id_token]


@pytest.fixture
def settings() -> Settings:
    return Settings(env="test", log_level="WARNING", auth_disabled=False)


@pytest.fixture
def fake_verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture
def app(settings: Settings, fake_verifier: FakeVerifier) -> FastAPI:
    application = create_app(settings)

    def _override() -> TokenVerifier:
        return fake_verifier

    application.dependency_overrides[get_token_verifier] = _override
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
