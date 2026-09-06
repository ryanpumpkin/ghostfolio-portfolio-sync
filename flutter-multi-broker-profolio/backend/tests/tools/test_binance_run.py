"""Binance import orchestration (spec §5.4, §5.5, §5.8).

The theme of every test here is the same: an import that *looks* complete
and silently is not is the worst possible outcome, because the key is
revoked afterwards and there is no second pass (§5.0).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.models.domain import TransactionType
from tools.binance_import.client import BinanceClient, BinanceConfig
from tools.binance_import.run import (
    EarnNotEmptyError,
    assert_earn_empty,
    assets_ever_held,
    candidate_symbols,
    fetch_trades_for_symbol,
    run_import,
    windows,
)


def _make_client(routes: dict[str, object], tmp_path: Path) -> BinanceClient:
    """A client whose responses come from a path -> payload map."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        payload = routes.get(request.url.path, [])
        if callable(payload):
            payload = payload(request)
        return httpx.Response(200, json=payload)

    config = BinanceConfig(
        api_key="k", api_secret="s", raw_dir=tmp_path / "raw",
        min_interval_seconds=0.0,
    )
    client = BinanceClient(
        config, http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _s: None,
    )
    client._time_offset_ms = 0
    client.calls = calls  # type: ignore[attr-defined]
    return client


class TestWindows:
    def test_covers_the_whole_span_with_no_gaps(self) -> None:
        # A skipped chunk is permanently lost history.
        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 7, 1, tzinfo=UTC)
        chunks = list(windows(start, end, 90))
        assert chunks[0][0] == start
        assert chunks[-1][1] == end
        for earlier, later in zip(chunks, chunks[1:], strict=False):
            assert earlier[1] == later[0]

    def test_a_short_span_is_one_chunk(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 1, 5, tzinfo=UTC)
        assert list(windows(start, end, 90)) == [(start, end)]

    def test_an_empty_span_yields_nothing(self) -> None:
        moment = datetime(2024, 1, 1, tzinfo=UTC)
        assert list(windows(moment, moment, 90)) == []


class TestEarnAssumption:
    """§5.8 — verify, do not assume."""

    def test_passes_when_both_endpoints_are_empty(self, tmp_path: Path) -> None:
        client = _make_client(
            {
                "/sapi/v1/simple-earn/flexible/position": {"rows": []},
                "/sapi/v1/simple-earn/locked/position": {"rows": []},
            },
            tmp_path,
        )
        assert_earn_empty(client)

    def test_aborts_when_flexible_has_a_position(self, tmp_path: Path) -> None:
        # "If either returns a balance, stop and report it."
        client = _make_client(
            {
                "/sapi/v1/simple-earn/flexible/position": {
                    "rows": [{"asset": "BTC", "totalAmount": "0.1"}]
                },
                "/sapi/v1/simple-earn/locked/position": {"rows": []},
            },
            tmp_path,
        )
        with pytest.raises(EarnNotEmptyError, match="flexible"):
            assert_earn_empty(client)

    def test_aborts_when_locked_has_a_position(self, tmp_path: Path) -> None:
        client = _make_client(
            {
                "/sapi/v1/simple-earn/flexible/position": {"rows": []},
                "/sapi/v1/simple-earn/locked/position": {
                    "rows": [{"asset": "ETH", "amount": "1"}]
                },
            },
            tmp_path,
        )
        with pytest.raises(EarnNotEmptyError, match="locked"):
            assert_earn_empty(client)

    def test_the_check_runs_before_the_crawl(self, tmp_path: Path) -> None:
        # Failing fast matters: otherwise an hour of crawling produces a
        # history that is known-incomplete anyway.
        client = _make_client(
            {
                "/sapi/v1/simple-earn/flexible/position": {
                    "rows": [{"asset": "BTC"}]
                },
            },
            tmp_path,
        )
        with pytest.raises(EarnNotEmptyError):
            run_import(client)
        assert "/api/v3/account" not in client.calls  # type: ignore[attr-defined]


class TestAssetDiscovery:
    """§5.5 step 1."""

    def test_includes_assets_with_a_current_balance(self) -> None:
        assets = assets_ever_held(
            balances=[{"asset": "BTC", "free": "0.5", "locked": "0"}],
            deposits=[], withdrawals=[],
        )
        assert assets == {"BTC"}

    def test_ignores_zero_balances(self) -> None:
        assets = assets_ever_held(
            balances=[{"asset": "XRP", "free": "0", "locked": "0"}],
            deposits=[], withdrawals=[],
        )
        assert assets == set()

    def test_finds_an_asset_that_was_fully_withdrawn(self) -> None:
        # The whole point of §5.5 step 1: a coin bought and then entirely
        # moved to the Ledger has a zero balance today but a very real
        # cost basis. Balances alone would miss it completely.
        assets = assets_ever_held(
            balances=[], deposits=[], withdrawals=[{"coin": "DOGE"}],
        )
        assert assets == {"DOGE"}

    def test_finds_an_asset_only_ever_deposited(self) -> None:
        assets = assets_ever_held(
            balances=[], deposits=[{"coin": "ETH"}], withdrawals=[],
        )
        assert assets == {"ETH"}

    def test_merges_all_four_sources(self) -> None:
        assets = assets_ever_held(
            balances=[{"asset": "BTC", "free": "1", "locked": "0"}],
            deposits=[{"coin": "ETH"}],
            withdrawals=[{"coin": "DOGE"}],
            known={"BNB"},
        )
        assert assets == {"BTC", "ETH", "DOGE", "BNB"}


class TestCandidateSymbols:
    """§5.5 step 2-3 — never brute force."""

    _INFO = {
        "symbols": [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT"},
            {"symbol": "BTCETH", "baseAsset": "BTC", "quoteAsset": "ETH"},
            {"symbol": "XRPUSDT", "baseAsset": "XRP", "quoteAsset": "USDT"},
            {"symbol": "BTCRUB", "baseAsset": "BTC", "quoteAsset": "RUB"},
        ]
    }

    def test_only_pairs_for_assets_actually_held(self) -> None:
        symbols = candidate_symbols(self._INFO, {"BTC"}, ("USDT", "ETH"))
        assert symbols == ["BTCETH", "BTCUSDT"]

    def test_skips_quote_assets_not_configured(self) -> None:
        symbols = candidate_symbols(self._INFO, {"BTC"}, ("USDT",))
        assert "BTCRUB" not in symbols

    def test_a_pair_that_does_not_exist_is_never_requested(self) -> None:
        # Filtering against exchangeInfo instead of crossing the sets
        # blindly: a nonexistent pair burns weight for no possible return
        # and pushes us toward a rate-limit ban.
        symbols = candidate_symbols(self._INFO, {"DOGE"}, ("USDT",))
        assert symbols == []

    def test_result_is_deduplicated_and_sorted(self) -> None:
        info = {"symbols": self._INFO["symbols"] + self._INFO["symbols"]}
        symbols = candidate_symbols(info, {"BTC", "XRP"}, ("USDT",))
        assert symbols == ["BTCUSDT", "XRPUSDT"]


class TestTradePagination:
    def test_stops_on_a_short_page(self, tmp_path: Path) -> None:
        client = _make_client(
            {"/api/v3/myTrades": [{"id": 1, "time": 1}, {"id": 2, "time": 2}]},
            tmp_path,
        )
        assert len(fetch_trades_for_symbol(client, "BTCUSDT")) == 2

    def test_follows_from_id_across_full_pages(self, tmp_path: Path) -> None:
        pages = [
            [{"id": i, "time": i} for i in range(1, 1001)],
            [{"id": i, "time": i} for i in range(1001, 1501)],
        ]
        state = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            page = pages[min(state["n"], len(pages) - 1)]
            state["n"] += 1
            return httpx.Response(200, json=page)

        config = BinanceConfig(
            api_key="k", api_secret="s", raw_dir=tmp_path / "raw",
            min_interval_seconds=0.0,
        )
        client = BinanceClient(
            config, http=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _s: None,
        )
        client._time_offset_ms = 0
        assert len(fetch_trades_for_symbol(client, "BTCUSDT")) == 1500

    def test_does_not_loop_forever_re_fetching_the_same_page(
        self, tmp_path: Path
    ) -> None:
        # `fromId` is inclusive. Without stepping past the last id, a full
        # page would be requested again and again.
        full_page = [{"id": i, "time": i} for i in range(1, 1001)]
        seen_from_ids: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            from_id = request.url.params.get("fromId")
            seen_from_ids.append(from_id)
            if from_id is None:
                return httpx.Response(200, json=full_page)
            return httpx.Response(200, json=[])

        config = BinanceConfig(
            api_key="k", api_secret="s", raw_dir=tmp_path / "raw",
            min_interval_seconds=0.0,
        )
        client = BinanceClient(
            config, http=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _s: None,
        )
        client._time_offset_ms = 0
        fetch_trades_for_symbol(client, "BTCUSDT")
        assert seen_from_ids[1] == "1001"


class TestRawArchive:
    """§5.5 step 5 — archive before normalising."""

    def test_responses_are_written_to_disk(self, tmp_path: Path) -> None:
        client = _make_client(
            {"/api/v3/myTrades": [{"id": 1, "time": 1}]}, tmp_path
        )
        fetch_trades_for_symbol(client, "BTCUSDT")
        archived = tmp_path / "raw" / "trades_BTCUSDT.json"
        assert archived.exists()
        assert json.loads(archived.read_text(encoding="utf-8"))[0]["id"] == 1


class TestFullCrawl:
    def test_reads_convert_and_dust_not_just_my_trades(
        self, tmp_path: Path
    ) -> None:
        # The failure this guards: reading only myTrades produces an
        # import that looks complete and silently is not (§5.4, §5.7).
        client = _make_client(
            {
                "/sapi/v1/simple-earn/flexible/position": {"rows": []},
                "/sapi/v1/simple-earn/locked/position": {"rows": []},
                "/api/v3/account": {
                    "balances": [{"asset": "BTC", "free": "1", "locked": "0"}]
                },
                "/sapi/v1/capital/deposit/hisrec": [],
                "/sapi/v1/capital/withdraw/history": [],
                "/api/v3/exchangeInfo": {
                    "symbols": [
                        {"symbol": "BTCUSDT", "baseAsset": "BTC",
                         "quoteAsset": "USDT"}
                    ]
                },
                "/api/v3/myTrades": [],
                "/sapi/v1/convert/tradeFlow": {
                    "list": [
                        {
                            "quoteId": "q1", "fromAsset": "USDT",
                            "toAsset": "BTC", "fromAmount": "8000",
                            "toAmount": "0.1", "createTime": 1700000000000,
                        }
                    ]
                },
                "/sapi/v1/asset/dribblet": {"userAssetDribblets": []},
            },
            tmp_path,
        )
        records = run_import(
            client,
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 2, 1, tzinfo=UTC),
        )
        assert {r.type for r in records} == {
            TransactionType.SELL,
            TransactionType.BUY,
        }
        assert client.calls.count("/sapi/v1/convert/tradeFlow") >= 1  # type: ignore[attr-defined]

    def test_a_withdrawal_survives_the_crawl_as_a_movement_plus_fee(
        self, tmp_path: Path
    ) -> None:
        client = _make_client(
            {
                "/sapi/v1/simple-earn/flexible/position": {"rows": []},
                "/sapi/v1/simple-earn/locked/position": {"rows": []},
                "/api/v3/account": {"balances": []},
                "/sapi/v1/capital/deposit/hisrec": [],
                "/sapi/v1/capital/withdraw/history": [
                    {
                        "id": "w1", "coin": "BTC", "amount": "0.5",
                        "transactionFee": "0.0005",
                        "address": "bc1qmine", "applyTime": 1700000000000,
                    }
                ],
                "/api/v3/exchangeInfo": {"symbols": []},
                "/sapi/v1/convert/tradeFlow": {"list": []},
                "/sapi/v1/asset/dribblet": {"userAssetDribblets": []},
            },
            tmp_path,
        )
        records = run_import(
            client,
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 2, 1, tzinfo=UTC),
        )
        types = [r.type for r in records]
        assert TransactionType.SELL not in types
        assert TransactionType.WITHDRAWAL in types
        assert TransactionType.FEE in types

    def test_records_come_back_in_time_order(self, tmp_path: Path) -> None:
        client = _make_client(
            {
                "/sapi/v1/simple-earn/flexible/position": {"rows": []},
                "/sapi/v1/simple-earn/locked/position": {"rows": []},
                "/api/v3/account": {"balances": []},
                "/sapi/v1/capital/deposit/hisrec": [
                    {"txId": "d2", "coin": "BTC", "amount": "1",
                     "insertTime": 1700000900000},
                    {"txId": "d1", "coin": "BTC", "amount": "1",
                     "insertTime": 1700000000000},
                ],
                "/sapi/v1/capital/withdraw/history": [],
                "/api/v3/exchangeInfo": {"symbols": []},
                "/sapi/v1/convert/tradeFlow": {"list": []},
                "/sapi/v1/asset/dribblet": {"userAssetDribblets": []},
            },
            tmp_path,
        )
        records = run_import(
            client,
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 2, 1, tzinfo=UTC),
        )
        assert [r.timestamp for r in records] == sorted(r.timestamp for r in records)

    def test_dust_batches_are_flattened_into_individual_disposals(
        self, tmp_path: Path
    ) -> None:
        # A dribblet entry holds several per-asset conversions. Treating
        # the batch as one record would collapse several disposals.
        client = _make_client(
            {
                "/sapi/v1/simple-earn/flexible/position": {"rows": []},
                "/sapi/v1/simple-earn/locked/position": {"rows": []},
                "/api/v3/account": {"balances": []},
                "/sapi/v1/capital/deposit/hisrec": [],
                "/sapi/v1/capital/withdraw/history": [],
                "/api/v3/exchangeInfo": {"symbols": []},
                "/sapi/v1/convert/tradeFlow": {"list": []},
                "/sapi/v1/asset/dribblet": {
                    "userAssetDribblets": [
                        {
                            "operateTime": 1700000000000,
                            "userAssetDribbletDetails": [
                                {"transId": "t1", "fromAsset": "TRX",
                                 "amount": "100", "transferedAmount": "0.02",
                                 "serviceChargeAmount": "0.001"},
                                {"transId": "t2", "fromAsset": "ADA",
                                 "amount": "50", "transferedAmount": "0.01",
                                 "serviceChargeAmount": "0.0005"},
                            ],
                        }
                    ]
                },
            },
            tmp_path,
        )
        records = run_import(
            client,
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 2, 1, tzinfo=UTC),
        )
        sold = {r.symbol for r in records if r.type is TransactionType.SELL}
        assert sold == {"TRX", "ADA"}

    def test_fees_are_converted_when_a_price_lookup_is_supplied(
        self, tmp_path: Path
    ) -> None:
        client = _make_client(
            {
                "/sapi/v1/simple-earn/flexible/position": {"rows": []},
                "/sapi/v1/simple-earn/locked/position": {"rows": []},
                "/api/v3/account": {
                    "balances": [{"asset": "BTC", "free": "1", "locked": "0"}]
                },
                "/sapi/v1/capital/deposit/hisrec": [],
                "/sapi/v1/capital/withdraw/history": [],
                "/api/v3/exchangeInfo": {
                    "symbols": [
                        {"symbol": "BTCUSDT", "baseAsset": "BTC",
                         "quoteAsset": "USDT"}
                    ]
                },
                "/api/v3/myTrades": [
                    {"id": 1, "symbol": "BTCUSDT", "qty": "0.5",
                     "price": "80000", "quoteQty": "40000",
                     "commission": "0.01", "commissionAsset": "BNB",
                     "isBuyer": True, "time": 1700000000000}
                ],
                "/sapi/v1/convert/tradeFlow": {"list": []},
                "/sapi/v1/asset/dribblet": {"userAssetDribblets": []},
            },
            tmp_path,
        )
        records = run_import(
            client,
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 2, 1, tzinfo=UTC),
            price_lookup={("BNB", "USDT"): Decimal("600")},
        )
        trade = next(r for r in records if r.type is TransactionType.BUY)
        assert trade.fee == Decimal("6.00")
        assert trade.fee_currency == "USDT"
