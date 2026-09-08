"""Normalise Binance history into the internal model (spec §5.6-5.9, §6.3).

Everything here exists because Binance's history is scattered across
endpoints that do not agree with each other:

* ``myTrades`` is the obvious one, and on its own it is **incomplete**.
* ``convert/tradeFlow`` — Convert trades do NOT appear in ``myTrades``
  (§5.4). Users often have substantial history here that is invisible if
  only ``myTrades`` is read (§5.7).
* ``asset/dribblet`` — small balances swept to BNB. Also absent from
  ``myTrades``.

All three become the same shape, because they are the same thing: a
disposal of one asset and an acquisition of another.

The rule that matters most (§6.3, "the most important rule in this
document") is that a **withdrawal is not a sale**. A coin bought here and
withdrawn to the Ledger is one BUY followed by a custody change. If that
became a SELL, the cost basis of every coin would be destroyed and every
downstream number would be wrong. There is an explicit test.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.models.domain import Transaction, TransactionType

_LOG = logging.getLogger("binance_import.normalize")

SOURCE = "binance"


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _ts(value: Any) -> datetime:
    """Binance timestamps, in either shape it actually sends.

    Most endpoints give epoch milliseconds. `withdraw/history` gives
    `applyTime` as a UTC datetime STRING — "2025-03-06 09:23:09" — and
    assuming milliseconds crashed the whole import on the first real
    withdrawal, after the crawl had already succeeded.

    No timezone is attached to that string; Binance documents it as UTC,
    and reading it as local time would move every withdrawal by hours.
    """
    if isinstance(value, str) and not value.strip().isdigit():
        text = value.strip().replace("T", " ").replace("Z", "")
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        raise ValueError(f"unrecognised Binance timestamp {value!r}")
    return datetime.fromtimestamp(int(value) / 1000.0, tz=UTC)


def _external_id(kind: str, ident: Any) -> str:
    return f"{SOURCE}:{kind}:{ident}"


def convert_fee_to_currency(
    *,
    fee: Decimal,
    fee_asset: str,
    target_currency: str,
    price_lookup: dict[tuple[str, str], Decimal] | None = None,
) -> Decimal | None:
    """Express a commission in the transaction's currency (§5.6).

    "The fee asset is frequently BNB, not the quote asset. Convert to the
    transaction currency using the price at trade time."

    Returns None when no rate is available. None means "unknown", which
    the caller must surface — silently substituting 0 would understate
    cost, and silently passing the raw number through would state a BNB
    amount as though it were USDT.
    """
    fee_asset = fee_asset.strip().upper()
    target_currency = target_currency.strip().upper()
    if fee_asset == target_currency:
        return fee
    if not price_lookup:
        return None
    rate = price_lookup.get((fee_asset, target_currency))
    if rate is None:
        inverse = price_lookup.get((target_currency, fee_asset))
        if inverse and inverse != 0:
            rate = Decimal(1) / inverse
    if rate is None:
        return None
    return fee * rate


def normalize_trade(
    raw: dict[str, Any],
    *,
    price_lookup: dict[tuple[str, str], Decimal] | None = None,
) -> Transaction:
    """A `myTrades` row -> BUY/SELL (§5.6)."""
    symbol = str(raw.get("symbol", ""))
    quantity = _dec(raw.get("qty"))
    price = _dec(raw.get("price"))
    quote_qty = _dec(raw.get("quoteQty"))
    amount = quote_qty if quote_qty is not None else (
        quantity * price if quantity is not None and price is not None else None
    )
    currency = str(raw.get("quoteAsset") or _infer_quote(symbol) or "USDT")

    fee_raw = _dec(raw.get("commission"))
    fee_asset = str(raw.get("commissionAsset") or currency).upper()
    fee = None
    if fee_raw is not None:
        fee = convert_fee_to_currency(
            fee=fee_raw,
            fee_asset=fee_asset,
            target_currency=currency,
            price_lookup=price_lookup,
        )
        if fee is None:
            _LOG.warning(
                "trade %s: commission %s %s could not be converted to %s — "
                "recording the original asset so it is not silently lost (§5.6)",
                raw.get("id"),
                fee_raw,
                fee_asset,
                currency,
            )

    return Transaction(
        source=SOURCE,
        transaction_id=str(raw.get("id") or raw.get("orderId")),
        external_id=_external_id("trade", raw.get("id") or raw.get("orderId")),
        symbol=symbol or None,
        side="buy" if raw.get("isBuyer") else "sell",
        type=TransactionType.BUY if raw.get("isBuyer") else TransactionType.SELL,
        quantity=quantity,
        price=price,
        amount=amount,
        currency=currency,
        # Retained even when unconverted: the raw asset and amount survive
        # so the gap is visible rather than rounded away (§5.6).
        fee=fee if fee is not None else fee_raw,
        fee_currency=currency if fee is not None else fee_asset,
        timestamp=_ts(raw["time"]),
    )


def normalize_convert(raw: dict[str, Any]) -> list[Transaction]:
    """A `convert/tradeFlow` row -> SELL + BUY (§5.7, §6.3b).

    An exchange "convert" realises a gain: it is a disposal and an
    acquisition, not a transfer. Both legs share a correlation id so the
    pair stays recognisable downstream (§6.3b).
    """
    quote_id = str(raw.get("quoteId") or raw.get("orderId"))
    correlation = f"{SOURCE}:convert:{quote_id}"
    when = _ts(raw.get("createTime") or raw["time"])

    from_asset = str(raw["fromAsset"]).upper()
    to_asset = str(raw["toAsset"]).upper()
    from_amount = _dec(raw["fromAmount"])
    to_amount = _dec(raw["toAmount"])

    if not from_amount or not to_amount:
        raise ValueError(f"convert {quote_id} has a zero leg")

    # Effective rate, in the disposed asset per acquired unit.
    sell_leg = Transaction(
        source=SOURCE,
        transaction_id=f"{quote_id}-out",
        external_id=f"{correlation}:out",
        symbol=from_asset,
        side="sell",
        type=TransactionType.SELL,
        quantity=from_amount,
        price=None,
        amount=to_amount,
        currency=to_asset,
        correlation_id=correlation,
        timestamp=when,
    )
    buy_leg = Transaction(
        source=SOURCE,
        transaction_id=f"{quote_id}-in",
        external_id=f"{correlation}:in",
        symbol=to_asset,
        side="buy",
        type=TransactionType.BUY,
        quantity=to_amount,
        price=from_amount / to_amount,
        amount=from_amount,
        currency=from_asset,
        correlation_id=correlation,
        timestamp=when,
    )
    return [sell_leg, buy_leg]


def normalize_dust(raw: dict[str, Any]) -> list[Transaction]:
    """A `asset/dribblet` detail row -> SELL of the dust + BUY of BNB.

    Dust conversion is a real disposal at a real rate. Skipping it leaves
    the swept assets looking as though they are still held.
    """
    tran_id = str(raw.get("transId") or raw.get("tranId"))
    correlation = f"{SOURCE}:dust:{tran_id}"
    when = _ts(raw.get("operateTime") or raw["time"])

    from_asset = str(raw["fromAsset"]).upper()
    amount = _dec(raw["amount"])
    bnb_gained = _dec(raw.get("transferedAmount") or raw.get("transferredAmount"))
    fee = _dec(raw.get("serviceChargeAmount"))

    if not amount or not bnb_gained:
        raise ValueError(f"dust {tran_id} has a zero leg")

    return [
        Transaction(
            source=SOURCE,
            transaction_id=f"{tran_id}-out",
            external_id=f"{correlation}:out",
            symbol=from_asset,
            side="sell",
            type=TransactionType.SELL,
            quantity=amount,
            amount=bnb_gained,
            currency="BNB",
            correlation_id=correlation,
            timestamp=when,
        ),
        Transaction(
            source=SOURCE,
            transaction_id=f"{tran_id}-in",
            external_id=f"{correlation}:in",
            symbol="BNB",
            side="buy",
            type=TransactionType.BUY,
            quantity=bnb_gained,
            amount=amount,
            currency=from_asset,
            # The dust service charge is a real cost, in BNB.
            fee=fee,
            fee_currency="BNB" if fee is not None else None,
            correlation_id=correlation,
            timestamp=when,
        ),
    ]


def normalize_deposit(raw: dict[str, Any]) -> Transaction:
    """A deposit -> DEPOSIT, never a BUY."""
    ident = raw.get("txId") or raw.get("id")
    return Transaction(
        source=SOURCE,
        transaction_id=str(ident),
        external_id=_external_id("deposit", ident),
        symbol=str(raw["coin"]).upper(),
        side="deposit",
        type=TransactionType.DEPOSIT,
        quantity=_dec(raw.get("amount")),
        currency=str(raw["coin"]).upper(),
        counterparty=str(raw.get("address") or "") or None,
        timestamp=_ts(raw.get("insertTime") or raw["time"]),
    )


def normalize_withdrawal(raw: dict[str, Any]) -> list[Transaction]:
    """A withdrawal -> WITHDRAWAL (+ FEE), **never a SELL** (§5.9, §6.3).

    Two separate spec rules meet here:

    * §6.3 — moving your own coins out is a custody change. Emitting a
      SELL would destroy the cost basis of everything that left.
    * §5.9 — ``transactionFee`` is a real cost, and is emitted as its own
      FEE record so it is not lost with the movement.

    The counterparty address is carried through so that
    `app.services.own_accounts` can later promote this to a TRANSFER once
    the owner's own addresses are known.
    """
    ident = raw.get("id") or raw.get("txId")
    coin = str(raw["coin"]).upper()
    when = _ts(raw.get("applyTime") or raw["time"])
    address = str(raw.get("address") or "") or None

    records = [
        Transaction(
            source=SOURCE,
            transaction_id=str(ident),
            external_id=_external_id("withdrawal", ident),
            symbol=coin,
            side="withdrawal",
            type=TransactionType.WITHDRAWAL,
            quantity=_dec(raw.get("amount")),
            currency=coin,
            counterparty=address,
            timestamp=when,
        )
    ]

    fee = _dec(raw.get("transactionFee"))
    if fee:
        records.append(
            Transaction(
                source=SOURCE,
                transaction_id=f"{ident}-fee",
                external_id=_external_id("withdrawal-fee", ident),
                symbol=coin,
                side="fee",
                type=TransactionType.FEE,
                quantity=fee,
                amount=fee,
                currency=coin,
                fee=fee,
                fee_currency=coin,
                counterparty=address,
                timestamp=when,
            )
        )
    return records


def _infer_quote(symbol: str) -> str | None:
    for quote in ("FDUSD", "USDT", "USDC", "BUSD", "TUSD", "USD", "BTC", "ETH", "BNB"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return quote
    return None


__all__ = [
    "SOURCE",
    "convert_fee_to_currency",
    "normalize_convert",
    "normalize_deposit",
    "normalize_dust",
    "normalize_trade",
    "normalize_withdrawal",
]
