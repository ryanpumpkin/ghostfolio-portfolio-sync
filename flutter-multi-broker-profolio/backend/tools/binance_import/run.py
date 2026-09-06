"""Orchestrates the one-off Binance crawl (spec §5.4, §5.5, §5.8).

The shape of this file is dictated by three awkward facts about Binance's
history API, all called out in §5:

1. **`myTrades` requires a `symbol`, and there are thousands.** §5.5 is
   explicit that brute force is wrong. Candidates are derived from the
   assets the account has *ever* held — current balances, plus every
   asset that ever arrived or left — crossed with a wide quote-asset
   list.

2. **Two entire categories of trade are invisible to `myTrades`.**
   Convert (`convert/tradeFlow`) and dust conversion (`asset/dribblet`)
   do not appear there at all. An import that reads only `myTrades` looks
   complete and silently is not.

3. **Every history endpoint is windowed.** Deposits and withdrawals cap
   at 90 days per call, Convert at 30. Each has to be walked backwards in
   chunks, and a chunk that is skipped is simply lost.

Everything is archived raw before normalisation (§5.5 step 5), because
the API key is revoked once this has run and a normaliser bug must not
require getting it back.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.models.domain import Transaction
from tools.binance_import.client import BinanceClient, BinanceImportError
from tools.binance_import.normalize import (
    normalize_convert,
    normalize_deposit,
    normalize_dust,
    normalize_trade,
    normalize_withdrawal,
)

_LOG = logging.getLogger("binance_import.run")

#: Binance launched in July 2017; nothing can predate this.
DEFAULT_START = datetime(2017, 7, 1, tzinfo=UTC)

_DEPOSIT_WINDOW_DAYS = 90
_CONVERT_WINDOW_DAYS = 30
_MY_TRADES_PAGE = 1000


class EarnNotEmptyError(BinanceImportError):
    """§5.8 — Earn was assumed unused and is not.

    "If either returns a balance, stop and report it — that would mean the
    assumption is wrong and distribution history must be imported after
    all, or the account cannot be safely abandoned."
    """


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def windows(
    start: datetime, end: datetime, days: int
) -> Iterator[tuple[datetime, datetime]]:
    """Split a range into chunks the API will actually accept.

    Yields oldest-first. A skipped chunk is permanently lost history, so
    the ranges deliberately overlap by nothing and cover the whole span.
    """
    cursor = start
    span = timedelta(days=days)
    while cursor < end:
        stop = min(cursor + span, end)
        yield cursor, stop
        cursor = stop


# ── §5.8: Earn must be empty ────────────────────────────────────────────


def assert_earn_empty(client: BinanceClient) -> None:
    """Verify the "Earn was never used" assumption instead of trusting it.

    §5.8: skip Earn entirely, but "call the flexible and locked position
    endpoints once during the import run and assert both return empty".
    """
    for label, path in (
        ("flexible", "/sapi/v1/simple-earn/flexible/position"),
        ("locked", "/sapi/v1/simple-earn/locked/position"),
    ):
        payload = client.request("GET", path)
        client.archive(f"earn_{label}", payload)
        rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
        if rows:
            raise EarnNotEmptyError(
                f"Binance Earn ({label}) is NOT empty: {len(rows)} position(s). "
                "§5.8 assumed Earn was never used. That assumption is wrong, so "
                "distribution history must be imported too — or the account "
                "cannot be safely abandoned. Stopping rather than importing an "
                "incomplete history."
            )
    _LOG.info("Earn confirmed empty (assumption verified, not assumed)")


# ── §5.5: which symbols to even ask about ───────────────────────────────


def assets_ever_held(
    *,
    balances: list[dict[str, Any]],
    deposits: list[dict[str, Any]],
    withdrawals: list[dict[str, Any]],
    known: set[str] | None = None,
) -> set[str]:
    """§5.5 step 1 — current balances ∪ deposits ∪ withdrawals ∪ local DB.

    An asset that was bought, then entirely withdrawn, has a zero balance
    today but a very real cost basis. Deposits and withdrawals are what
    make it visible at all.
    """
    assets: set[str] = set(known or set())
    for row in balances:
        asset = str(row.get("asset", "")).upper()
        free = Decimal(str(row.get("free", "0") or "0"))
        locked = Decimal(str(row.get("locked", "0") or "0"))
        if asset and (free or locked):
            assets.add(asset)
    for row in deposits:
        if coin := str(row.get("coin", "")).upper():
            assets.add(coin)
    for row in withdrawals:
        if coin := str(row.get("coin", "")).upper():
            assets.add(coin)
    assets.discard("")
    return assets


def candidate_symbols(
    exchange_info: dict[str, Any],
    assets: set[str],
    quote_assets: tuple[str, ...],
) -> list[str]:
    """§5.5 step 2 — real symbols only, never a brute-forced cross product.

    Filtering against `exchangeInfo` matters: asking for a pair that has
    never existed wastes weight and gets us closer to a rate-limit ban for
    no possible return.
    """
    quotes = {q.upper() for q in quote_assets}
    found: list[str] = []
    for entry in exchange_info.get("symbols", []):
        base = str(entry.get("baseAsset", "")).upper()
        quote = str(entry.get("quoteAsset", "")).upper()
        if base in assets and quote in quotes:
            found.append(str(entry["symbol"]))
    return sorted(set(found))


# ── fetchers ────────────────────────────────────────────────────────────


def fetch_trades_for_symbol(client: BinanceClient, symbol: str) -> list[dict[str, Any]]:
    """Walk one symbol's entire history, paginating on `fromId` (§5.5 step 4)."""
    collected: list[dict[str, Any]] = []
    from_id: int | None = None
    while True:
        params: dict[str, Any] = {"symbol": symbol, "limit": _MY_TRADES_PAGE}
        if from_id is not None:
            params["fromId"] = from_id
        page = client.request("GET", "/api/v3/myTrades", params=params)
        if not page:
            break
        collected.extend(page)
        if len(page) < _MY_TRADES_PAGE:
            break
        # `fromId` is inclusive, so step past the last row or we loop forever
        # re-fetching the same page.
        from_id = max(int(row["id"]) for row in page) + 1
    if collected:
        client.archive(f"trades_{symbol}", collected)
        _LOG.info("%s: %d trade(s)", symbol, len(collected))
    return collected


def fetch_windowed(
    client: BinanceClient,
    path: str,
    *,
    start: datetime,
    end: datetime,
    days: int,
    label: str,
    extra: dict[str, Any] | None = None,
    rows_key: str | None = None,
) -> list[dict[str, Any]]:
    """Walk a windowed history endpoint from `start` to `end`."""
    collected: list[dict[str, Any]] = []
    for chunk_start, chunk_end in windows(start, end, days):
        params: dict[str, Any] = {
            "startTime": _ms(chunk_start),
            "endTime": _ms(chunk_end),
        }
        if extra:
            params.update(extra)
        payload = client.request("GET", path, params=params)
        rows = payload
        if isinstance(payload, dict):
            rows = payload.get(rows_key or "rows") or payload.get("data") or []
        if rows:
            collected.extend(rows)
    if collected:
        client.archive(label, collected)
        _LOG.info("%s: %d row(s)", label, len(collected))
    return collected


# ── orchestration ───────────────────────────────────────────────────────


def run_import(
    client: BinanceClient,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    known_assets: set[str] | None = None,
    price_lookup: dict[tuple[str, str], Decimal] | None = None,
) -> list[Transaction]:
    """Crawl everything and return normalised transactions.

    Deliberately returns records rather than pushing: the push is a
    separate, idempotent step (`app.services.ghostfolio.sync`), and
    keeping them apart means a normalisation bug can be fixed and replayed
    from the raw archive without touching the API again (§5.5).
    """
    begin = start or DEFAULT_START
    finish = end or datetime.now(UTC)

    # §5.8 first: if Earn is not empty, nothing else about this import is
    # trustworthy, so fail before spending an hour crawling.
    assert_earn_empty(client)

    account = client.request("GET", "/api/v3/account")
    client.archive("account", account)
    balances = account.get("balances", [])

    deposits = fetch_windowed(
        client, "/sapi/v1/capital/deposit/hisrec",
        start=begin, end=finish, days=_DEPOSIT_WINDOW_DAYS, label="deposits",
    )
    withdrawals = fetch_windowed(
        client, "/sapi/v1/capital/withdraw/history",
        start=begin, end=finish, days=_DEPOSIT_WINDOW_DAYS, label="withdrawals",
    )

    assets = assets_ever_held(
        balances=balances, deposits=deposits, withdrawals=withdrawals,
        known=known_assets,
    )
    _LOG.info("assets ever held: %s", ", ".join(sorted(assets)) or "(none)")

    exchange_info = client.request("GET", "/api/v3/exchangeInfo", signed=False)
    client.archive("exchange_info", exchange_info)
    symbols = candidate_symbols(exchange_info, assets, client.config.quote_assets)
    _LOG.info("candidate symbols: %d", len(symbols))

    records: list[Transaction] = []

    for symbol in symbols:
        for row in fetch_trades_for_symbol(client, symbol):
            records.append(normalize_trade(row, price_lookup=price_lookup))

    # §5.7 — invisible to myTrades, and often substantial.
    for row in fetch_windowed(
        client, "/sapi/v1/convert/tradeFlow",
        start=begin, end=finish, days=_CONVERT_WINDOW_DAYS, label="convert",
        rows_key="list",
    ):
        records.extend(normalize_convert(row))

    for row in _dust_rows(
        fetch_windowed(
            client, "/sapi/v1/asset/dribblet",
            start=begin, end=finish, days=_DEPOSIT_WINDOW_DAYS, label="dust",
            rows_key="userAssetDribblets",
        )
    ):
        records.extend(normalize_dust(row))

    records.extend(normalize_deposit(row) for row in deposits)
    for row in withdrawals:
        records.extend(normalize_withdrawal(row))

    records.sort(key=lambda tx: tx.timestamp)
    _LOG.info("normalised %d record(s)", len(records))
    return records


def _dust_rows(dribblets: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Flatten dribblet batches into their individual conversions.

    `asset/dribblet` returns one entry per sweep, each containing several
    per-asset details. Treating the batch as one record would collapse
    several disposals into one.
    """
    for batch in dribblets:
        details = batch.get("userAssetDribbletDetails") or batch.get("details") or []
        for detail in details:
            merged = dict(detail)
            merged.setdefault("operateTime", batch.get("operateTime"))
            yield merged


__all__ = [
    "DEFAULT_START",
    "EarnNotEmptyError",
    "assert_earn_empty",
    "assets_ever_held",
    "candidate_symbols",
    "fetch_trades_for_symbol",
    "fetch_windowed",
    "run_import",
    "windows",
]
