"""Firebase ID-token verification middleware / dependencies.

Design notes:
    * The verifier is abstracted behind a `TokenVerifier` protocol so unit
      tests can substitute a fake without touching Firebase Admin.
    * The default `FirebaseTokenVerifier` lazily initialises Firebase Admin
      using the service-account JSON pointed to by settings. That branch is
      excluded from coverage because it requires real credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.settings import Settings, get_settings


class AuthError(HTTPException):
    """401 wrapper for auth failures."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


@dataclass(frozen=True)
class AuthenticatedUser:
    """The subject extracted from a verified ID token."""

    user_id: str
    email: str | None = None
    claims: dict[str, Any] | None = None


class TokenVerifier(Protocol):
    """Verifies a Firebase ID token and returns the decoded claims."""

    def verify(self, id_token: str) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


class FirebaseTokenVerifier:
    """Production verifier that delegates to firebase_admin.auth.verify_id_token."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._initialised = False

    def _ensure_initialised(self) -> None:  # pragma: no cover - requires real creds
        # Firebase Admin SDK bootstrap; not exercised in unit tests since it
        # requires a real service-account key file.
        if self._initialised:
            return
        import firebase_admin
        from firebase_admin import credentials

        if not firebase_admin._apps:
            cred_path = self._settings.firebase_credentials_path
            cred = credentials.Certificate(cred_path) if cred_path else credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred, {"projectId": self._settings.firebase_project_id})
        self._initialised = True

    def verify(self, id_token: str) -> dict[str, Any]:  # pragma: no cover - requires real creds
        self._ensure_initialised()
        from firebase_admin import auth as fb_auth

        decoded: dict[str, Any] = fb_auth.verify_id_token(id_token)
        return decoded


_verifier_singleton: TokenVerifier | None = None


def get_token_verifier() -> TokenVerifier:
    """FastAPI dependency yielding the active TokenVerifier.

    Tests should override this dependency via `app.dependency_overrides`.
    """
    global _verifier_singleton
    if _verifier_singleton is None:
        _verifier_singleton = FirebaseTokenVerifier(get_settings())
    return _verifier_singleton


def set_token_verifier(verifier: TokenVerifier | None) -> None:
    """Replace the cached verifier (useful for tests / boot)."""
    global _verifier_singleton
    _verifier_singleton = verifier


_bearer = HTTPBearer(auto_error=False)


def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthenticatedUser:
    """FastAPI dependency that resolves the bearer token to an AuthenticatedUser."""
    if settings.auth_disabled:
        user = AuthenticatedUser(user_id="dev-user")
        request.state.user = user
        return user

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthError("Missing or malformed Authorization header")

    try:
        claims = verifier.verify(credentials.credentials)
    except AuthError:
        raise
    except Exception as exc:
        raise AuthError("Invalid or expired ID token") from exc

    uid = claims.get("uid") or claims.get("sub") or claims.get("user_id")
    if not isinstance(uid, str) or not uid:
        raise AuthError("Token missing subject")

    email_raw = claims.get("email")
    email = email_raw if isinstance(email_raw, str) else None
    user = AuthenticatedUser(user_id=uid, email=email, claims=claims)
    request.state.user = user
    return user
