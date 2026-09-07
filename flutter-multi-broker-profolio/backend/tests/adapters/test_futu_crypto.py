"""Futu crypto: a separate account, and a code shape that lies (§6.2, §7.1).

Every fixture here is a verbatim row from real OpenD 10.6.6608 on
2026-09-07. Two things about them cost real money if mishandled:

* The deal code carries the QUOTE CURRENCY and no `currency` field comes
  with it. `CC.BTCHKD` at 550,102 read as USD is a 7.8x overstatement —
  the same failure HK equities had, wearing a different disguise.
* `CC.BTCHKD` and `CC.BTCUSD` are ONE coin. Mapped as written they fork
  Bitcoin into two holdings, which this codebase has already paid for
  once with the VOO/fee split.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.futu.adapter import _deal_currency, _map_position, _map_transaction
from app.services.symbols import (
    SymbolResolutionError,
    resolve,
    split_futu_crypto,
)

# Real position row (the BTC holding that was missing from the total).
POSITION = {
    "code": "CC.BTC", "currency": "USD", "qty": "0.00391",
    "cost_price": "72801.11", "nominal_price": "79073.61",
    "market_val": "309.1778151", "pl_val": "24.525451144",
    "position_market": "CRYPTO", "stock_name": "Bitcoin",
}

# Real deal rows: same coin, two quote currencies, neither naming one.
DEAL_HKD = {
    "code": "CC.BTCHKD", "deal_market": "CRYPTO", "trd_side": "BUY",
    "price": "550102.0", "qty": "0.00198", "deal_id": "1534182175051635216",
    "order_id": "FTHC2970253454796635392",
    "create_time": "2026-02-07 04:17:01.842",
}
DEAL_USD = {
    "code": "CC.BTCUSD", "deal_market": "CRYPTO", "trd_side": "BUY",
    "price": "100358.11", "qty": "0.0003", "deal_id": "3661278568735033715",
    "order_id": "OEHK20241213D00501171",
    "create_time": "2024-12-13 22:08:49.527",
}


class TestCodeSplitting:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("CC.BTCHKD", ("BTC", "HKD")),
            ("CC.BTCUSD", ("BTC", "USD")),
            ("CC.BTC", ("BTC", None)),
            ("CC.ETHUSDT", ("ETH", "USDT")),
        ],
    )
    def test_splits_quote_off_the_base(self, code, expected) -> None:
        assert split_futu_crypto(code) == expected

    def test_longest_quote_wins(self) -> None:
        # USDT must not be read as USD with a stray T left on the base.
        assert split_futu_crypto("CC.BTCUSDT") == ("BTC", "USDT")

    def test_equity_codes_are_not_crypto(self) -> None:
        assert split_futu_crypto("HK.00823") is None
        assert split_futu_crypto("US.VOO") is None


class TestCurrency:
    def test_hkd_deal_is_not_read_as_usd(self) -> None:
        assert _deal_currency(DEAL_HKD) == "HKD"

    def test_usd_deal_is_usd(self) -> None:
        assert _deal_currency(DEAL_USD) == "USD"

    def test_transaction_carries_the_derived_currency(self) -> None:
        tx = _map_transaction(DEAL_HKD)
        assert tx.currency == "HKD"
        assert tx.price == Decimal("550102.0")
        # deal_id, not order_id: all five fills share one order.
        assert tx.transaction_id == "1534182175051635216"


class TestOneCoinNotTwo:
    def test_both_quote_currencies_resolve_to_one_instrument(self) -> None:
        hkd = resolve("CC.BTCHKD")
        usd = resolve("CC.BTCUSD")
        assert hkd.canonical_id == usd.canonical_id
        assert hkd.code == "BTC"

    def test_position_code_agrees_with_the_deals(self) -> None:
        assert resolve("CC.BTC").canonical_id == resolve("CC.BTCHKD").canonical_id

    def test_unknown_coin_is_refused_not_invented(self) -> None:
        # `CC.` says crypto, so the equity rules must never claim it —
        # that would mint a Yahoo ticker priced at nothing forever.
        with pytest.raises(SymbolResolutionError, match="does not know"):
            resolve("CC.WIFHKD")


class TestPosition:
    def test_crypto_position_maps(self) -> None:
        position = _map_position(POSITION)
        assert position.symbol == "CC.BTC"
        assert position.quantity == Decimal("0.00391")
        assert position.currency == "USD"
        assert position.avg_cost == Decimal("72801.11")

    def test_market_comes_from_position_market(self) -> None:
        # Equities say `trd_market`; crypto says `position_market`. An
        # instrument with no venue is dropped by the mapper.
        assert _map_position(POSITION).exchange == "CRYPTO"


class TestTimeoutBudget:
    """The budget has to cover BOTH accounts and BOTH handshakes.

    A three-year deal walk is ~300 s per account and each cold context
    costs ~120 s to open. `max(300, 240)` covers neither, and a budget
    that cannot succeed is not a timeout — it is a guaranteed failure
    that looks like a broker problem.
    """

    @staticmethod
    def _client(monkeypatch, *, crypto: bool):
        from app.adapters.futu import client as mod

        monkeypatch.setattr(mod, "_TRADE_CTX_CACHE", {})
        c = mod.FutuOpenDClient(host="h", port=1)
        c._enable_crypto = crypto
        return c

    def test_cold_two_accounts_covers_walk_and_handshakes(self, monkeypatch) -> None:
        c = self._client(monkeypatch, crypto=True)
        # 300 s of walking per account, plus 120 s of handshake per cold
        # context.
        assert c._budget(300.0) == 300.0 * 2 + 120.0 * 2

    def test_cold_one_account_is_unchanged(self, monkeypatch) -> None:
        c = self._client(monkeypatch, crypto=False)
        assert c._budget(30.0) == 30.0 + 120.0

    def test_warm_single_account_keeps_the_tight_budget(self, monkeypatch) -> None:
        from app.adapters.futu import client as mod

        c = self._client(monkeypatch, crypto=False)
        mod._TRADE_CTX_CACHE[c._cache_key(mod._SEC)] = object()
        # Fast failure when OpenD is dead is the whole point of the
        # tight budget; adding the handshake here would undo it.
        assert c._budget(30.0) == 30.0

    def test_crypto_disabled_opens_only_the_securities_context(
        self, monkeypatch
    ) -> None:
        from app.adapters.futu import client as mod

        assert self._client(monkeypatch, crypto=False)._kinds() == [mod._SEC]
        assert self._client(monkeypatch, crypto=True)._kinds() == [
            mod._SEC, mod._CRYPTO
        ]


class TestReconciliation:
    """The deal feed and the position feed must name the same thing.

    Reconciliation compares symbols as the SOURCE reports them, so a
    mismatch here is not cosmetic: it makes real trades count for
    nothing and books an opening balance on top of them.
    """

    def test_deal_symbol_matches_the_position_symbol(self) -> None:
        assert _map_transaction(DEAL_HKD).symbol == POSITION["code"]
        assert _map_transaction(DEAL_USD).symbol == POSITION["code"]

    def test_normalising_the_symbol_does_not_lose_the_currency(self) -> None:
        assert _map_transaction(DEAL_HKD).currency == "HKD"
        assert _map_transaction(DEAL_USD).currency == "USD"

    def test_equity_symbols_are_untouched(self) -> None:
        deal = dict(DEAL_HKD, code="HK.00823", deal_market="HK")
        assert _map_transaction(deal).symbol == "HK.00823"

    def test_trades_explain_the_holding(self) -> None:
        """The real numbers: six fills totalling 0.0039 against a held
        0.00391. Before the fix this reported the whole position missing."""
        from app.services.ghostfolio.opening import compute_gaps

        deals = [
            dict(DEAL_USD, qty="0.0003", deal_id="d0"),
            dict(DEAL_HKD, qty="0.00021", deal_id="d1"),
            dict(DEAL_HKD, qty="0.00047", deal_id="d2"),
            dict(DEAL_HKD, qty="0.00198", deal_id="d3"),
            dict(DEAL_HKD, qty="0.00047", deal_id="d4"),
            dict(DEAL_HKD, qty="0.00047", deal_id="d5"),
        ]
        report = compute_gaps(
            positions=[_map_position(POSITION)],
            transactions=[_map_transaction(d) for d in deals],
        )
        assert len(report.gaps) == 1
        gap = report.gaps[0]
        assert gap.derived == Decimal("0.0039")
        # Only the rounding crumb is unexplained, not the whole holding.
        assert gap.missing == Decimal("0.00001")
        assert report.surplus == []
