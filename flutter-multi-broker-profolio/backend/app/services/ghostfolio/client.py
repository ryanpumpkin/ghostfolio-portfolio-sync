"""HTTP client for the Ghostfolio REST API (spec §7.1).

Auth is two-legged, which is easy to get wrong:

1. The **security token** (Ghostfolio calls it ``accessToken``) is the
   long-lived credential the user gets once, when they create the account.
   There is no password and no reset — losing it loses the account.
2. ``POST /api/v1/auth/anonymous`` exchanges that security token for a
   short-lived **JWT** (``authToken``).
3. Every other call sends ``Authorization: Bearer <JWT>``.

Sending the security token as the bearer will fail with 401. The client
handles the exchange itself and refreshes on 401 exactly once, so callers
only ever supply the security token.

Verified against Ghostfolio 3.67.0. §7.1 says to confirm the endpoint and
payload shape against the running instance rather than trusting docs, so
``probe()`` exists to do precisely that.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import httpx

from app.adapters._common import PermanentError, TransientError

_LOG = logging.getLogger("mbp.ghostfolio.client")

_DEFAULT_TIMEOUT = 30.0
# Import runs push the whole history in one body; give them room.
_IMPORT_TIMEOUT = 180.0


class GhostfolioError(Exception):
    """Any Ghostfolio API failure."""


class GhostfolioAuthError(GhostfolioError):
    """The security token was rejected."""


def _redact(token: str | None) -> str:
    """First 4 chars only — §3.5 forbids logging credential material.

    Used solely to tell two tokens apart when debugging an auth failure.
    """
    if not token:
        return "<none>"
    return f"{token[:4]}…({len(token)} chars)"


def to_json_number(value: Decimal) -> float:
    """Convert a Decimal to the JSON number Ghostfolio's DTO expects.

    Everything upstream of this function is ``Decimal`` (§3.2). Ghostfolio's
    ``CreateOrderDto`` types quantity/unitPrice/fee as ``number``, so a
    conversion has to happen somewhere; this is the single place it does,
    and it is loud when it loses information.

    IEEE-754 doubles carry ~15-17 significant digits, so an 8-decimal crypto
    quantity round-trips exactly. A value that does not round-trip is a real
    problem — silently truncated cost basis is exactly the failure §3.2
    exists to prevent — so we log it rather than let it pass unseen.
    """
    as_float = float(value)
    if Decimal(repr(as_float)) != value.normalize():
        _LOG.warning(
            "precision loss converting Decimal to JSON number: %s -> %r "
            "(this will skew cost basis; investigate before trusting it)",
            value,
            as_float,
        )
    return as_float


class GhostfolioClient:
    """Async client for the subset of Ghostfolio's API we actually use."""

    def __init__(
        self,
        *,
        base_url: str,
        security_token: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if not base_url:
            raise ValueError("ghostfolio base_url is required")
        if not security_token:
            raise ValueError("ghostfolio security_token is required")
        self._base_url = base_url.rstrip("/")
        self._security_token = security_token
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._jwt: str | None = None

    async def __aenter__(self) -> GhostfolioClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ── auth ────────────────────────────────────────────────────────────────

    async def _authenticate(self) -> str:
        """Exchange the security token for a JWT."""
        url = f"{self._base_url}/api/v1/auth/anonymous"
        try:
            response = await self._client.post(
                url, json={"accessToken": self._security_token}, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise TransientError(f"ghostfolio auth request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise GhostfolioAuthError(
                f"ghostfolio rejected the security token {_redact(self._security_token)}"
            )
        if response.status_code >= 500:
            raise TransientError(f"ghostfolio auth returned {response.status_code}")
        if response.status_code >= 400:
            raise GhostfolioAuthError(
                f"ghostfolio auth returned {response.status_code}"
            )

        payload = response.json()
        token = payload.get("authToken")
        if not token:
            raise GhostfolioAuthError("ghostfolio auth response had no authToken")
        self._jwt = token
        _LOG.info("ghostfolio authenticated (jwt=%s)", _redact(token))
        return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        timeout: float | None = None,
        _retried: bool = False,
    ) -> httpx.Response:
        if self._jwt is None:
            await self._authenticate()

        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._jwt}"}
        content: bytes | None = None
        if json_body is not None:
            # Serialise ourselves so Decimals go through `to_json_number`
            # rather than httpx's encoder, which would simply reject them.
            content = json.dumps(json_body, default=_json_default).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            response = await self._client.request(
                method, url, content=content, headers=headers,
                timeout=timeout or self._timeout,
            )
        except httpx.HTTPError as exc:
            raise TransientError(f"ghostfolio {method} {path} failed: {exc}") from exc

        # A JWT expires; refresh once, then give up so a genuinely bad
        # security token cannot spin.
        if response.status_code == 401 and not _retried:
            _LOG.info("ghostfolio JWT rejected, re-authenticating once")
            self._jwt = None
            return await self._request(
                method, path, json_body=json_body, timeout=timeout, _retried=True
            )

        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response, what: str) -> None:
        if response.status_code < 400:
            return
        body = response.text[:500]
        if response.status_code in (401, 403):
            raise GhostfolioAuthError(f"{what}: {response.status_code} {body}")
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(f"{what}: {response.status_code} {body}")
        raise PermanentError(f"{what}: {response.status_code} {body}")

    # ── read ────────────────────────────────────────────────────────────────

    async def health(self) -> bool:
        """Unauthenticated liveness probe."""
        try:
            response = await self._client.get(
                f"{self._base_url}/api/v1/health", timeout=self._timeout
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def list_accounts(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/v1/account")
        self._raise_for_status(response, "list accounts")
        payload = response.json()
        accounts = payload.get("accounts", payload)
        return accounts if isinstance(accounts, list) else []

    async def list_activities(self) -> list[dict[str, Any]]:
        """Every activity Ghostfolio holds.

        The idempotency ledger (§3.3) is authoritative for what we pushed;
        this is the independent view used to detect drift between the two,
        and to answer §7.1's "read back what the UI produced" check.
        """
        response = await self._request("GET", "/api/v1/order")
        self._raise_for_status(response, "list activities")
        payload = response.json()
        activities = payload.get("activities", payload)
        return activities if isinstance(activities, list) else []

    # ── write ───────────────────────────────────────────────────────────────

    async def create_account(
        self, *, name: str, currency: str, comment: str | None = None
    ) -> dict[str, Any]:
        """Create one Ghostfolio Account (§7.1: one per source)."""
        body: dict[str, Any] = {
            "balance": 0,
            "currency": currency,
            "isExcluded": False,
            "name": name,
        }
        if comment:
            body["comment"] = comment
        response = await self._request("POST", "/api/v1/account", json_body=body)
        self._raise_for_status(response, f"create account {name!r}")
        return response.json()

    async def import_activities(
        self, activities: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """POST /api/v1/import.

        Ghostfolio validates the whole batch and rejects all of it if any
        single activity is invalid, so callers should push in per-source
        batches — one bad symbol then costs one source, not the whole sync.
        """
        if not activities:
            return {"activities": []}
        response = await self._request(
            "POST",
            "/api/v1/import",
            json_body={"activities": activities},
            timeout=_IMPORT_TIMEOUT,
        )
        self._raise_for_status(response, f"import {len(activities)} activities")
        return response.json() if response.content else {}

    # ── verification (§7.1) ─────────────────────────────────────────────────

    async def probe(self) -> dict[str, Any]:
        """Confirm the live instance matches what this client assumes.

        §7.1 says to verify the endpoint and payload shape against the
        running instance rather than trusting the docs, because Ghostfolio's
        import surface has changed across releases. Run this before the
        first bulk import and after every version bump.
        """
        info: dict[str, Any] = {"base_url": self._base_url}
        try:
            response = await self._client.get(
                f"{self._base_url}/api/v1/info", timeout=self._timeout
            )
            if response.status_code == 200:
                payload = response.json()
                info["version"] = payload.get("version")
                info["currencies"] = len(payload.get("currencies") or [])
        except httpx.HTTPError as exc:
            info["info_error"] = str(exc)

        info["healthy"] = await self.health()
        try:
            accounts = await self.list_accounts()
            info["accounts"] = [
                {"id": a.get("id"), "name": a.get("name"), "currency": a.get("currency")}
                for a in accounts
            ]
            activities = await self.list_activities()
            info["activity_count"] = len(activities)
            # The one thing §7.1 insists must be observed, not guessed:
            # which (symbol, dataSource) pairing Ghostfolio itself produces
            # for a crypto holding.
            info["observed_data_sources"] = sorted(
                {
                    f"{a.get('SymbolProfile', {}).get('dataSource')}"
                    f":{a.get('SymbolProfile', {}).get('symbol')}"
                    for a in activities
                    if a.get("SymbolProfile")
                }
            )[:25]
        except GhostfolioAuthError as exc:
            info["auth_error"] = str(exc)
        return info


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return to_json_number(value)
    raise TypeError(f"cannot serialise {type(value).__name__} to JSON")


__all__ = [
    "GhostfolioAuthError",
    "GhostfolioClient",
    "GhostfolioError",
    "to_json_number",
]
