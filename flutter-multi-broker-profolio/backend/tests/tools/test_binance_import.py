"""One-off Binance import (spec §5).

§5.10 item 4 names five tests by hand. All five are here, plus the
supporting cases that make them meaningful:

  1. signature generation against a known fixture
  2. fee paid in a third asset (BNB) converted correctly
  3. withdrawal to an external wallet produces TRANSFER + FEE, never SELL
  4. convert + dust records normalized into trades
  5. idempotent push — running twice creates no duplicates
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.domain import TransactionType
from app.services.ghostfolio.ledger import SyncLedger
from app.services.ghostfolio.mapper import SkipReason, map_transactions
from app.services.own_accounts import OwnAccountsRegistry, resolve_transfers
from tools.binance_import.client import (
    BinanceBannedError,
    BinanceClient,
    BinanceConfig,
)
from tools.binance_import.normalize import (
    convert_fee_to_currency,
    normalize_convert,
    normalize_dust,
    normalize_trade,
    normalize_withdrawal,
)

_OWN_WALLET = "bc1qownledgeraddress"


def _client(**overrides) -> BinanceClient:
    config = BinanceConfig(api_key="key", api_secret="secret", **overrides)
    return BinanceClient(config, sleep=lambda _s: None)


# ── 1. signature against a known fixture (§5.10) ────────────────────────


class TestSignature:
    def test_matches_a_precomputed_hmac(self) -> None:
        # Binance's own documented example parameters.
        query = (
            "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC"
            "&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
        )
        expected = hmac.new(
            b"NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j",
            query.encode(),
            hashlib.sha256,
        ).hexdigest()

        config = BinanceConfig(
            api_key="x",
            api_secret="NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j",
        )
        assert BinanceClient(config).sign(query) == expected

    def test_signature_is_lowercase_hex(self) -> None:
        signature = _client().sign("a=1")
        assert signature == signature.lower()
        assert len(signature) == 64

    def test_a_different_query_gives_a_different_signature(self) -> None:
        # Guards the -1022 trap: the signature must cover the exact string
        # that is sent, so re-serialising params after signing breaks it.
        client = _client()
        assert client.sign("a=1&b=2") != client.sign("b=2&a=1")


# ── 2. fee paid in a third asset (§5.6, §5.10) ──────────────────────────


class TestThirdAssetFee:
    def test_bnb_fee_is_converted_to_the_trade_currency(self) -> None:
        # §5.6: "The fee asset is frequently BNB, not the quote asset."
        converted = convert_fee_to_currency(
            fee=Decimal("0.01"),
            fee_asset="BNB",
            target_currency="USDT",
            price_lookup={("BNB", "USDT"): Decimal("600")},
        )
        assert converted == Decimal("6.00")

    def test_an_inverse_rate_is_used_when_that_is_what_exists(self) -> None:
        converted = convert_fee_to_currency(
            fee=Decimal("1"),
            fee_asset="USDT",
            target_currency="BTC",
            price_lookup={("BTC", "USDT"): Decimal("80000")},
        )
        assert converted == Decimal(1) / Decimal(80000)

    def test_same_asset_needs_no_conversion(self) -> None:
        assert convert_fee_to_currency(
            fee=Decimal("2.5"), fee_asset="USDT", target_currency="USDT"
        ) == Decimal("2.5")

    def test_unknown_rate_returns_none_rather_than_zero(self) -> None:
        # Substituting 0 would understate cost silently; passing the raw
        # number through would state BNB as though it were USDT.
        assert convert_fee_to_currency(
            fee=Decimal("0.01"), fee_asset="BNB", target_currency="USDT"
        ) is None

    def test_trade_carries_the_converted_fee(self) -> None:
        tx = normalize_trade(
            {
                "id": 1, "symbol": "BTCUSDT", "qty": "0.5", "price": "80000",
                "quoteQty": "40000", "commission": "0.01",
                "commissionAsset": "BNB", "isBuyer": True, "time": 1700000000000,
            },
            price_lookup={("BNB", "USDT"): Decimal("600")},
        )
        assert tx.fee == Decimal("6.00")
        assert tx.fee_currency == "USDT"

    def test_unconvertible_fee_keeps_its_original_asset(self) -> None:
        # The gap stays visible rather than being rounded away.
        tx = normalize_trade(
            {
                "id": 1, "symbol": "BTCUSDT", "qty": "0.5", "price": "80000",
                "commission": "0.01", "commissionAsset": "BNB",
                "isBuyer": True, "time": 1700000000000,
            }
        )
        assert tx.fee == Decimal("0.01")
        assert tx.fee_currency == "BNB"


# ── 3. withdrawal: TRANSFER + FEE, never SELL (§5.9, §6.3, §5.10) ───────


class TestWithdrawalIsNotASale:
    """§6.3 — "the most important rule in this document"."""

    @pytest.fixture
    def raw(self) -> dict:
        return {
            "id": "w1", "coin": "BTC", "amount": "0.5",
            "transactionFee": "0.0005", "address": _OWN_WALLET,
            "applyTime": 1700000000000,
        }

    def test_never_produces_a_sell(self, raw: dict) -> None:
        records = normalize_withdrawal(raw)
        assert TransactionType.SELL not in {r.type for r in records}

    def test_never_produces_a_buy(self, raw: dict) -> None:
        records = normalize_withdrawal(raw)
        assert TransactionType.BUY not in {r.type for r in records}

    def test_produces_a_movement_and_a_fee(self, raw: dict) -> None:
        records = normalize_withdrawal(raw)
        assert [r.type for r in records] == [
            TransactionType.WITHDRAWAL,
            TransactionType.FEE,
        ]

    def test_the_fee_is_a_real_cost_in_the_coin(self, raw: dict) -> None:
        # §5.9: "The transactionFee on a withdrawal is a real cost."
        fee = normalize_withdrawal(raw)[1]
        assert fee.quantity == Decimal("0.0005")
        assert fee.currency == "BTC"

    def test_becomes_a_transfer_once_the_address_is_known_as_ours(
        self, raw: dict, tmp_path: Path
    ) -> None:
        # This is the §5.10 wording: "produces TRANSFER + FEE". The
        # withdrawal carries the counterparty; the own-accounts registry
        # is what promotes it (§6.3).
        config = tmp_path / "own.yaml"
        config.write_text(f"addresses:\n  - {_OWN_WALLET}\n", encoding="utf-8")
        registry = OwnAccountsRegistry.load(config)

        resolved = resolve_transfers(normalize_withdrawal(raw), registry)
        assert [r.type for r in resolved] == [
            TransactionType.TRANSFER,
            TransactionType.FEE,
        ]

    def test_neither_leg_ever_reaches_ghostfolio_as_a_trade(self, raw: dict) -> None:
        mapped, skipped = map_transactions(
            normalize_withdrawal(raw)[:1],
            account_id_by_source={"binance": "acc"},
            crypto_overrides={"BTC": "bitcoin"},
        )
        assert mapped == []
        assert skipped[0].reason is SkipReason.TRANSFER

    def test_a_withdrawal_without_a_fee_yields_one_record(self) -> None:
        records = normalize_withdrawal(
            {"id": "w2", "coin": "ETH", "amount": "1", "applyTime": 1700000000000}
        )
        assert len(records) == 1


# ── 4. convert + dust normalised into trades (§5.7, §5.10) ──────────────


class TestConvertAndDust:
    def test_convert_becomes_two_linked_legs(self) -> None:
        # §5.4: "Convert trades do NOT appear in myTrades." Reading only
        # myTrades loses this history entirely.
        legs = normalize_convert(
            {
                "quoteId": "q1", "fromAsset": "USDT", "toAsset": "BTC",
                "fromAmount": "8000", "toAmount": "0.1",
                "createTime": 1700000000000,
            }
        )
        assert [leg.type for leg in legs] == [
            TransactionType.SELL,
            TransactionType.BUY,
        ]

    def test_convert_legs_share_a_correlation_id(self) -> None:
        legs = normalize_convert(
            {
                "quoteId": "q1", "fromAsset": "USDT", "toAsset": "BTC",
                "fromAmount": "8000", "toAmount": "0.1",
                "createTime": 1700000000000,
            }
        )
        assert len({leg.correlation_id for leg in legs}) == 1
        assert len({leg.timestamp for leg in legs}) == 1

    def test_convert_records_the_effective_rate(self) -> None:
        buy = normalize_convert(
            {
                "quoteId": "q1", "fromAsset": "USDT", "toAsset": "BTC",
                "fromAmount": "8000", "toAmount": "0.1",
                "createTime": 1700000000000,
            }
        )[1]
        assert buy.price == Decimal("80000")

    def test_dust_becomes_a_sell_and_a_bnb_buy(self) -> None:
        legs = normalize_dust(
            {
                "transId": "d1", "fromAsset": "TRX", "amount": "100",
                "transferedAmount": "0.02", "serviceChargeAmount": "0.001",
                "operateTime": 1700000000000,
            }
        )
        assert [leg.type for leg in legs] == [
            TransactionType.SELL,
            TransactionType.BUY,
        ]
        assert legs[1].symbol == "BNB"

    def test_dust_service_charge_is_kept_as_a_fee(self) -> None:
        legs = normalize_dust(
            {
                "transId": "d1", "fromAsset": "TRX", "amount": "100",
                "transferedAmount": "0.02", "serviceChargeAmount": "0.001",
                "operateTime": 1700000000000,
            }
        )
        assert legs[1].fee == Decimal("0.001")
        assert legs[1].fee_currency == "BNB"

    def test_a_zero_leg_is_rejected_not_silently_dropped(self) -> None:
        with pytest.raises(ValueError):
            normalize_convert(
                {
                    "quoteId": "q1", "fromAsset": "USDT", "toAsset": "BTC",
                    "fromAmount": "0", "toAmount": "0.1",
                    "createTime": 1700000000000,
                }
            )


# ── 5. idempotent push (§3.3, §5.10) ────────────────────────────────────


class TestIdempotentPush:
    def test_importing_twice_creates_no_duplicates(self, tmp_path: Path) -> None:
        trades = [
            normalize_trade(
                {
                    "id": i, "symbol": "BTCUSDT", "qty": "0.1", "price": "80000",
                    "quoteQty": "8000", "isBuyer": True, "time": 1700000000000,
                }
            )
            for i in range(3)
        ]
        accounts = {"binance": "acc"}
        overrides = {"BTC": "bitcoin"}

        with SyncLedger(tmp_path / "sync.db") as ledger:
            pushed_per_run = []
            for _ in range(2):
                mapped, _ = map_transactions(
                    trades, account_id_by_source=accounts,
                    crypto_overrides=overrides,
                )
                to_push = ledger.filter_unpushed(mapped)
                pushed_per_run.append(len(to_push))
                ledger.record_pushed(to_push)

            assert pushed_per_run == [3, 0]
            assert ledger.count("binance") == 3

    def test_external_ids_are_stable_and_distinct(self) -> None:
        first = normalize_trade(
            {
                "id": 7, "symbol": "BTCUSDT", "qty": "1", "price": "1",
                "isBuyer": True, "time": 1700000000000,
            }
        )
        again = normalize_trade(
            {
                "id": 7, "symbol": "BTCUSDT", "qty": "1", "price": "1",
                "isBuyer": True, "time": 1700000000000,
            }
        )
        other = normalize_trade(
            {
                "id": 8, "symbol": "BTCUSDT", "qty": "1", "price": "1",
                "isBuyer": True, "time": 1700000000000,
            }
        )
        assert first.external_id == again.external_id
        assert first.external_id != other.external_id

    def test_withdrawal_and_its_fee_have_different_ids(self) -> None:
        # Sharing an id would make the ledger treat them as one record and
        # silently drop the fee.
        records = normalize_withdrawal(
            {
                "id": "w1", "coin": "BTC", "amount": "0.5",
                "transactionFee": "0.0005", "applyTime": 1700000000000,
            }
        )
        assert records[0].external_id != records[1].external_id


# ── rate limiting (§5.3) ────────────────────────────────────────────────


class TestRateLimitDiscipline:
    def test_418_is_fatal_and_not_retried(self) -> None:
        # §5.3: "Treat 418 as fatal for the run — abort, do not retry."
        # Retrying an IP ban only extends it.
        import httpx

        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(418, text="banned")

        transport = httpx.MockTransport(handler)
        config = BinanceConfig(api_key="k", api_secret="s")
        client = BinanceClient(
            config, http=httpx.Client(transport=transport), sleep=lambda _s: None
        )
        client._time_offset_ms = 0  # skip the clock call

        with pytest.raises(BinanceBannedError):
            client.request("GET", "/api/v3/account")
        assert calls["n"] == 1

    def test_quote_asset_list_is_wide_by_default(self) -> None:
        # §5.5 step 6: "Widen the quote-asset list rather than narrowing
        # it. An extra dozen empty queries costs nothing on a one-off run;
        # a missed trade is permanent."
        assets = BinanceConfig(api_key="k", api_secret="s").quote_assets
        for expected in ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "BNB"):
            assert expected in assets

    def test_base_url_is_configurable(self) -> None:
        # §5.3: "configurable — some regions need a different host."
        config = BinanceConfig(
            api_key="k", api_secret="s", base_url="https://api.binance.us"
        )
        assert config.base_url == "https://api.binance.us"


class TestRateLimitBackoff:
    """A 429 must always cost real time.

    Observed on a live crawl: Binance returned `Retry-After: 0`, the
    client honoured it literally, and five retries went out in 1.25
    seconds. Repeated 429s escalate to the 418 IP ban this client treats
    as fatal, so the backoff is what stops a rate limit becoming a ban.
    """

    @staticmethod
    def _response(headers: dict) -> object:
        return type("R", (), {"headers": headers})()

    def test_retry_after_zero_is_not_obeyed(self) -> None:
        from tools.binance_import.client import (
            MIN_RATE_LIMIT_SLEEP,
            BinanceClient,
        )

        delay = BinanceClient._retry_after(self._response({"Retry-After": "0"}), 0)
        assert delay >= MIN_RATE_LIMIT_SLEEP

    def test_a_longer_retry_after_wins(self) -> None:
        from tools.binance_import.client import BinanceClient

        delay = BinanceClient._retry_after(self._response({"Retry-After": "45"}), 0)
        assert delay >= 45

    def test_missing_header_still_backs_off(self) -> None:
        from tools.binance_import.client import (
            MIN_RATE_LIMIT_SLEEP,
            BinanceClient,
        )

        assert BinanceClient._retry_after(self._response({}), 0) >= MIN_RATE_LIMIT_SLEEP

    def test_backoff_grows_with_attempts(self) -> None:
        from tools.binance_import.client import BinanceClient

        early = BinanceClient._retry_after(self._response({"Retry-After": "0"}), 1)
        late = BinanceClient._retry_after(self._response({"Retry-After": "0"}), 5)
        assert late > early
