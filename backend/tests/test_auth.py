"""Auth middleware tests using an injected fake verifier."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.main import create_app
from app.middleware.auth import (
    AuthenticatedUser,
    AuthError,
    FirebaseTokenVerifier,
    TokenVerifier,
    get_token_verifier,
    set_token_verifier,
)
from tests.conftest import FakeVerifier


def test_missing_token_rejected(client: TestClient) -> None:
    resp = client.get("/v1/whoami")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "http_401"
    assert body["request_id"]


def test_bad_token_rejected(client: TestClient) -> None:
    resp = client.get("/v1/whoami", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    assert "Invalid" in resp.json()["message"]


def test_good_token_accepted(client: TestClient) -> None:
    resp = client.get("/v1/whoami", headers={"Authorization": "Bearer good-token"})
    assert resp.status_code == 200
    assert resp.json() == {"user_id": "user-123", "email": "a@b.com"}


def test_non_bearer_scheme_rejected(client: TestClient) -> None:
    # FastAPI's HTTPBearer with auto_error=False returns None for non-Bearer schemes,
    # which our dependency treats as missing credentials -> 401.
    resp = client.get("/v1/whoami", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


def test_token_without_uid_rejected() -> None:
    settings = Settings(env="test", auth_disabled=False)
    application = create_app(settings)

    class NoUid:
        def verify(self, token: str) -> dict[str, object]:
            return {"email": "x@y.com"}

    application.dependency_overrides[get_token_verifier] = lambda: NoUid()
    with TestClient(application) as c:
        resp = c.get("/v1/whoami", headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 401
        assert "subject" in resp.json()["message"]


def test_auth_disabled_bypass() -> None:
    settings = Settings(env="test", auth_disabled=True)
    application = create_app(settings)
    with TestClient(application) as c:
        resp = c.get("/v1/whoami")
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "dev-user"


def test_auth_error_passthrough() -> None:
    settings = Settings(env="test", auth_disabled=False)
    application = create_app(settings)

    class Raiser:
        def verify(self, token: str) -> dict[str, object]:
            raise AuthError("custom-rejection")

    application.dependency_overrides[get_token_verifier] = lambda: Raiser()
    with TestClient(application) as c:
        resp = c.get("/v1/whoami", headers={"Authorization": "Bearer x"})
        assert resp.status_code == 401
        assert resp.json()["message"] == "custom-rejection"


def test_token_verifier_singleton_roundtrip() -> None:
    set_token_verifier(None)
    v1 = get_token_verifier()
    assert isinstance(v1, FirebaseTokenVerifier)
    # Subsequent calls return the same instance.
    assert get_token_verifier() is v1

    fake: TokenVerifier = FakeVerifier()
    set_token_verifier(fake)
    assert get_token_verifier() is fake
    set_token_verifier(None)


def test_authenticated_user_dataclass_frozen() -> None:
    from dataclasses import FrozenInstanceError

    u = AuthenticatedUser(user_id="x")
    with pytest.raises(FrozenInstanceError):
        u.user_id = "y"  # type: ignore[misc]
