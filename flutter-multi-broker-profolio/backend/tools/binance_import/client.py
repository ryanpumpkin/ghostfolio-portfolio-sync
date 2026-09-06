"""Signed Binance REST client for the one-off historical import (spec §5.3).

This is deliberately a **script's** client, not a production adapter. §5.0:
the owner is not continuing to use Binance; this exists to recover the
cost basis of coins already bought there so the holdings now on the
Ledger are not orphaned. Afterwards the API key is revoked.

That changes what matters. Not throughput, not incremental sync, not
caching — **completeness**. A missed trade is a permanently wrong cost
basis and there is no second pass, so this client is slow on purpose.

Four things in §5.3 that are easy to get wrong, all handled here:

* **Sign the exact query string that is sent.** Re-serialising params
  after signing is named in the spec as the most common cause of
  ``-1022 Signature for this request is not valid``. We build the string
  once, sign that string, and send that string.
* **Do not trust the container clock.** Skew produces ``-1021``. The
  offset is measured once per session against ``GET /api/v3/time``.
* **Watch the weight header.** ``X-MBX-USED-WEIGHT-1M`` is read from
  every response and the client slows down as it approaches the cap.
* **418 is fatal.** Repeated 429s escalate to a temporary IP ban. §5.3:
  "Treat 418 as fatal for the run — abort, do not retry."
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

_LOG = logging.getLogger("binance_import.client")

DEFAULT_BASE_URL = "https://api.binance.com"
#: Binance's per-minute IP weight cap. We back off well before it.
WEIGHT_LIMIT_PER_MINUTE = 6000
WEIGHT_SOFT_LIMIT = 0.75


class BinanceImportError(RuntimeError):
    """Any failure during the import."""


class BinanceBannedError(BinanceImportError):
    """HTTP 418 — the IP is temporarily banned. Fatal for the run (§5.3)."""


class BinanceAuthError(BinanceImportError):
    """Key rejected, expired, or lacking permission."""


@dataclass(slots=True)
class BinanceConfig:
    """§5.10 item 3 — the knobs the spec requires to be configurable."""

    api_key: str
    api_secret: str
    #: §5.3: "configurable — some regions need a different host. Do not
    #: hardcode."
    base_url: str = DEFAULT_BASE_URL
    recv_window: int = 5000
    #: §5.5 step 6: "Widen the quote-asset list rather than narrowing it.
    #: An extra dozen empty queries costs nothing on a one-off run; a
    #: missed trade is permanent."
    quote_assets: tuple[str, ...] = (
        "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USD",
        "BTC", "ETH", "BNB", "DAI", "EUR", "TRY",
    )
    #: Where raw responses are archived before normalisation (§5.5 step 5).
    raw_dir: Path = field(default_factory=lambda: Path("data/binance_raw"))
    #: Deliberately gentle. This runs once; slow is always better than banned.
    min_interval_seconds: float = 0.25


class BinanceClient:
    """Minimal signed client covering only what §5.4 lists."""

    def __init__(
        self,
        config: BinanceConfig,
        *,
        http: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._config = config
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=30.0)
        self._sleep = sleep
        self._time_offset_ms: int | None = None
        self._last_request_at = 0.0
        self._used_weight = 0

    @property
    def config(self) -> BinanceConfig:
        return self._config

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> BinanceClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── signing (§5.3) ──────────────────────────────────────────────────

    def sign(self, query_string: str) -> str:
        """HMAC-SHA256 of the exact query string, hex, lowercase."""
        return hmac.new(
            self._config.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _signed_query(self, params: dict[str, Any]) -> str:
        """Build and sign one query string, returning it ready to send.

        The signature covers this exact string. Callers must not rebuild
        or reorder it afterwards — that is the -1022 trap.
        """
        payload = {k: v for k, v in params.items() if v is not None}
        payload["recvWindow"] = self._config.recv_window
        payload["timestamp"] = self._timestamp_ms()
        query = urllib.parse.urlencode(payload)
        return f"{query}&signature={self.sign(query)}"

    # ── clock (§5.3) ────────────────────────────────────────────────────

    def _timestamp_ms(self) -> int:
        if self._time_offset_ms is None:
            self.sync_clock()
        return int(time.time() * 1000) + (self._time_offset_ms or 0)

    def sync_clock(self) -> int:
        """Measure our offset from Binance's clock once per session."""
        local_before = int(time.time() * 1000)
        response = self._http.get(f"{self._config.base_url}/api/v3/time")
        response.raise_for_status()
        server_ms = int(response.json()["serverTime"])
        local_after = int(time.time() * 1000)
        # Split the round trip so the offset is not skewed by latency.
        self._time_offset_ms = server_ms - (local_before + local_after) // 2
        _LOG.info("clock offset vs Binance: %+d ms", self._time_offset_ms)
        return self._time_offset_ms

    # ── rate limiting (§5.3) ────────────────────────────────────────────

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._config.min_interval_seconds:
            self._sleep(self._config.min_interval_seconds - elapsed)
        soft_cap = WEIGHT_LIMIT_PER_MINUTE * WEIGHT_SOFT_LIMIT
        if self._used_weight > soft_cap:
            # Approaching the cap: wait out the window rather than risk
            # the 429 -> 418 escalation.
            _LOG.warning(
                "used weight %d is past %.0f%% of the cap — pausing 60s",
                self._used_weight,
                WEIGHT_SOFT_LIMIT * 100,
            )
            self._sleep(60)
            self._used_weight = 0

    def _record_weight(self, response: httpx.Response) -> None:
        raw = response.headers.get("X-MBX-USED-WEIGHT-1M")
        if raw is not None:
            try:
                self._used_weight = int(raw)
            except ValueError:
                pass

    # ── requests ────────────────────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = True,
        max_attempts: int = 5,
    ) -> Any:
        url = f"{self._config.base_url}{path}"
        headers = {"X-MBX-APIKEY": self._config.api_key} if signed or params else {}
        headers["X-MBX-APIKEY"] = self._config.api_key

        for attempt in range(max_attempts):
            self._throttle()
            if signed:
                query = self._signed_query(params or {})
            else:
                query = urllib.parse.urlencode(
                    {k: v for k, v in (params or {}).items() if v is not None}
                )
            target = f"{url}?{query}" if query else url

            self._last_request_at = time.monotonic()
            response = self._http.request(method, target, headers=headers)
            self._record_weight(response)

            if response.status_code == 418:
                # §5.3: fatal. Retrying an IP ban only extends it.
                raise BinanceBannedError(
                    "HTTP 418 — IP temporarily banned by Binance. Aborting the "
                    "run; do not retry. Wait out the ban before resuming."
                )
            if response.status_code == 429:
                delay = self._retry_after(response, attempt)
                _LOG.warning("429 rate limited; sleeping %.1fs", delay)
                self._sleep(delay)
                continue
            if response.status_code in (401, 403):
                raise BinanceAuthError(
                    f"{response.status_code} from {path}: key rejected or lacks "
                    "permission. Note that a key without an IP whitelist "
                    "expires after 90 days (§5.2)."
                )
            if response.status_code >= 500:
                delay = self._backoff(attempt)
                _LOG.warning("%d from %s; retrying in %.1fs", response.status_code, path, delay)
                self._sleep(delay)
                continue
            if response.status_code >= 400:
                raise BinanceImportError(
                    f"{response.status_code} from {path}: {response.text[:300]}"
                )
            return response.json()

        raise BinanceImportError(f"{path}: exhausted {max_attempts} attempts")

    @staticmethod
    def _retry_after(response: httpx.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        return BinanceClient._backoff(attempt)

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential with full jitter."""
        base = min(2.0**attempt, 60.0)
        return base * (0.5 + random.random() / 2)

    # ── raw archive (§5.5 step 5) ───────────────────────────────────────

    def archive(self, name: str, payload: Any) -> Path:
        """Persist a raw response before it is normalised.

        §5.5: "If the normalizer has a bug, the fix should not require
        re-hitting the API. This matters more here than in a repeating
        adapter, because the key is revoked afterwards."
        """
        directory = self._config.raw_dir
        directory.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        path = directory / f"{safe}.json"
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path


__all__ = [
    "DEFAULT_BASE_URL",
    "BinanceAuthError",
    "BinanceBannedError",
    "BinanceClient",
    "BinanceConfig",
    "BinanceImportError",
]
