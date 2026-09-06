"""IBKR Flex Web Service adapter — the Gateway-free path (spec §4.1).

Why this exists alongside `adapter.py`
--------------------------------------
The Client Portal / TWS Gateway adapter needs a long-lived GUI process, a
daily interactive re-login, and a second factor the owner has to tap. It
also authenticates with the account's *login password*, which then has to
live somewhere the container can read. For a scheduled, unattended sync
that is three separate single points of failure and one credential we
would rather not store at all.

Flex Web Service has none of that. Two GETs, a token and a query id, no
session, no 2FA, no order-placing capability whatsoever. §4.1 prefers it
for exactly these reasons; this module is what makes that preference
runnable.

The protocol
------------
Deliberately two-legged, because IBKR generates the report asynchronously:

1. ``SendRequest?t=<token>&q=<queryId>&v=3`` → a **reference code**.
2. ``GetStatement?t=<token>&q=<referenceCode>&v=3`` → the statement XML,
   or a ``Fail`` telling us it is still generating, in which case we wait
   and ask again.

Both legs answer with HTTP 200 whatever happens, so the status has to be
read out of the XML body. A client that only checks the status code will
happily parse an error document as an empty portfolio — which looks
exactly like "you sold everything" and is the single most dangerous
failure mode here.

Scope of one statement
----------------------
Whatever the *query* was configured to include, not whatever we ask for
at runtime — the date range and the sections live in the query definition
on IBKR's side. So the query must be set up with the window opened wide
(§4.1) and with Open Positions, Cash Report and Trades enabled; there is
no runtime parameter that can compensate for a narrow one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from xml.etree import ElementTree

import httpx

from app.adapters._common import (
    HealthTracker,
    PermanentError,
    RetryPolicy,
    TransientError,
    retry_async,
)
from app.adapters.base import SourceAdapter
from app.models.domain import (
    CashBalance,
    Position,
    Quote,
    SourceHealth,
    Transaction,
    TransactionType,
)

_LOG = logging.getLogger(__name__)

SOURCE_NAME = "ibkr"

#: IBKR's documented Flex endpoint host.
DEFAULT_BASE_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
)

#: The only version this parser has been written against.
FLEX_VERSION = "3"

#: `CashReportCurrency` emits a roll-up row alongside the per-currency ones.
#: Summing it with the real rows double-counts the entire cash balance.
_CASH_SUMMARY_ROWS = frozenset({"BASE_SUMMARY", "BASE SUMMARY"})


class FlexError(Exception):
    """Flex Web Service returned something we cannot use."""


class FlexAuthError(FlexError):
    """Token or query id rejected. Retrying will not help."""


class FlexRateLimitedError(TransientError):
    """IBKR is refusing further requests from this token for now.

    Raised as a TransientError so existing retry paths treat it as a
    wait, but typed so a backfill can slow down instead of hammering.
    """


class FlexStatementUnavailableError(FlexError):
    """IBKR has no statement for the requested window.

    Error 1003. Walking history backwards eventually asks for a period
    before the account existed, and this is the answer. It is the natural
    end of the backfill, not a failure — but it must stay distinct from a
    *successful* empty response, which would mean "you traded nothing"
    and is never something to infer from an error.
    """


class FlexStatementNotReadyError(FlexError):
    """A successful-looking response that contains no statement yet.

    Observed against the live account: the first `GetStatement` after a
    fresh `SendRequest` can come back as a well-formed `FlexQueryResponse`
    with `Status` unset and **no** `<FlexStatement>` inside, and a retry
    moments later returns the full report. So an empty document is
    ambiguous — it means either "still generating" or "this query really
    matches nothing".

    It is a `FlexError` subclass on purpose: after the poll budget is
    spent the ambiguity resolves to a hard error rather than an empty
    portfolio, because "the account holds nothing" is the one answer that
    must never be produced by accident (§4.1).
    """


@dataclass(slots=True)
class FlexConfig:
    """Everything needed to pull a statement.

    `token` is a Flex *web service* token, not the account password and not
    an API key: it is read-only by construction, scoped to the queries the
    owner enabled, and expires on its own. It cannot place an order, move
    cash, or log in to Account Management.
    """

    token: str
    query_id: str
    base_url: str = DEFAULT_BASE_URL
    #: How long to keep asking while IBKR is still generating the report.
    poll_interval: float = 5.0
    poll_timeout: float = 180.0
    request_timeout: float = 60.0
    #: One request may not span more than 366 days (error 1023), so a
    #: backfill walks the range in windows of this size. 364 rather than
    #: 366 because the ceiling is inclusive of both endpoints and a leap
    #: year silently pushes a "one year" window over it.
    window_days: int = 364
    #: IBKR rate limits per token (error 1018) and a backfill trips it
    #: quickly. This is the pause between windows.
    window_pause: float = 20.0
    #: How long to keep waiting out a rate limit before giving up.
    rate_limit_backoff: float = 60.0
    rate_limit_attempts: int = 5


class FlexTransport(Protocol):
    """Seam for tests: returns the raw XML body of a Flex GET."""

    async def get(self, url: str, params: dict[str, str]) -> str: ...


class HttpxFlexTransport:
    """Real transport. Kept trivial so the parsing can be tested without it."""

    def __init__(self, *, timeout: float = 60.0) -> None:
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> HttpxFlexTransport:
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, url: str, params: dict[str, str]) -> str:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:  # network-level: worth retrying
            raise TransientError(f"Flex request failed: {exc}") from exc
        # A non-200 from Flex is unusual enough that the body is the only
        # useful diagnostic, so carry it rather than just the status.
        if response.status_code >= 500:
            raise TransientError(
                f"Flex returned HTTP {response.status_code}: {response.text[:200]}"
            )
        if response.status_code != 200:
            raise PermanentError(
                f"Flex returned HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.text


# ── parsing ─────────────────────────────────────────────────────────────


def _dec(value: Any) -> Decimal | None:
    """Parse a Flex numeric attribute, tolerating IBKR's empty strings.

    Every amount stays Decimal from here to the ledger (§3.2). Going
    through float, even briefly, is what turns 0.1 + 0.2 into a
    reconciliation discrepancy nobody can explain.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _required_dec(value: Any, *, field_name: str) -> Decimal:
    parsed = _dec(value)
    if parsed is None:
        raise FlexError(f"missing or unparseable {field_name}: {value!r}")
    return parsed


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_moment(*candidates: Any) -> datetime:
    """Turn Flex's several date formats into an aware UTC datetime.

    Flex emits `dateTime` as ``YYYYMMDD;HHMMSS``, sometimes
    ``YYYYMMDD;HH:MM:SS``, sometimes ``YYYY-MM-DD, HH:MM:SS``, and dates
    alone as ``YYYYMMDD`` — the exact shape depends on the date-format
    option chosen when the query was defined, which we do not control.
    """
    for candidate in candidates:
        text = _text(candidate)
        if not text:
            continue
        normalised = text.replace(";", " ").replace(",", " ")
        normalised = " ".join(normalised.split())
        for pattern in (
            "%Y%m%d %H%M%S",
            "%Y%m%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y%m%d",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(normalised, pattern).replace(tzinfo=UTC)
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise FlexError(f"no parseable timestamp in {candidates!r}")


@dataclass(slots=True)
class FlexStatement:
    """One parsed statement — everything a sync cycle needs, from one fetch."""

    account_ids: list[str] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    balances: list[CashBalance] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    generated_at: datetime | None = None


def _check_response_status(root: ElementTree.Element) -> None:
    """Raise on a ``Fail`` document; return quietly on anything else.

    Flex signals failure with HTTP 200 and a ``<Status>Fail</Status>``
    body, so this check is the only thing standing between an error and a
    portfolio that appears to have been liquidated.
    """
    status = root.findtext("Status")
    if status is None or status.strip().lower() != "fail":
        return

    code = (root.findtext("ErrorCode") or "").strip()
    message = (root.findtext("ErrorMessage") or "").strip() or "no message"
    lowered = message.lower()

    # "Still generating" is the normal path, not an error: the report is
    # built asynchronously and the first GetStatement usually loses the
    # race. Retryable.
    if "progress" in lowered or "try again" in lowered:
        raise TransientError(f"Flex statement not ready yet ({code}): {message}")
    # 1018 "Too many requests have been made from this token." IBKR rate
    # limits per token, and a multi-window backfill trips it easily. It is
    # a wait, not a failure — but it must be distinguishable from "not
    # ready" so the caller can back off harder.
    if "too many requests" in lowered:
        raise FlexRateLimitedError(f"Flex rate limited ({code}): {message}")
    # Anything about the token or the query is the owner's setup, and no
    # amount of retrying fixes it — say so instead of burning the poll
    # budget.
    if any(word in lowered for word in ("token", "invalid", "expired", "not authori")):
        raise FlexAuthError(f"Flex rejected the request ({code}): {message}")
    # 1003 "Statement is not available." — asked for a period the account
    # does not cover. Only meaningful to a backfill, which stops there.
    if "not available" in lowered:
        raise FlexStatementUnavailableError(
            f"no statement for this period ({code}): {message}"
        )
    raise FlexError(f"Flex request failed ({code}): {message}")


def parse_reference_code(xml_text: str) -> tuple[str, str | None]:
    """Read the reference code (and follow-up URL) out of a SendRequest reply."""
    root = _parse_xml(xml_text)
    _check_response_status(root)
    code = _text(root.findtext("ReferenceCode"))
    if not code:
        raise FlexError("SendRequest succeeded but returned no ReferenceCode")
    return code, _text(root.findtext("Url"))


def _parse_xml(xml_text: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise FlexError(f"Flex returned unparseable XML: {exc}") from exc


def parse_statement(xml_text: str) -> FlexStatement:
    """Parse a full statement document into domain objects."""
    root = _parse_xml(xml_text)
    # A GetStatement failure uses the same envelope as a SendRequest one.
    _check_response_status(root)

    statements = root.findall(".//FlexStatement")
    if not statements:
        raise FlexStatementNotReadyError(
            "no <FlexStatement> in the response. Either IBKR has not finished "
            "generating the report, or the query really matches nothing — "
            "check that it has Open Positions, Cash Report and Trades enabled "
            "and that its date range covers the period (§4.1)."
        )

    result = FlexStatement()
    for statement in statements:
        account_id = _text(statement.get("accountId"))
        if account_id and account_id not in result.account_ids:
            result.account_ids.append(account_id)
        if result.generated_at is None:
            try:
                result.generated_at = _parse_moment(statement.get("whenGenerated"))
            except FlexError:
                result.generated_at = None

        result.positions.extend(_parse_positions(statement, account_id))
        result.balances.extend(_parse_cash(statement, account_id))
        result.transactions.extend(_parse_trades(statement, account_id))
        result.transactions.extend(_parse_cash_transactions(statement, account_id))

    result.transactions.sort(key=lambda tx: tx.timestamp)
    return result


def _parse_positions(
    statement: ElementTree.Element, account_id: str | None
) -> list[Position]:
    positions: list[Position] = []
    for node in statement.findall(".//OpenPosition"):
        quantity = _dec(node.get("position"))
        if quantity is None or quantity == 0:
            # A closed position can still be listed with quantity 0; it is
            # not a holding and would pollute reconciliation (§6.4).
            continue
        symbol = _text(node.get("symbol")) or _text(node.get("description"))
        currency = _text(node.get("currency"))
        if not symbol or not currency:
            _LOG.warning(
                "skipping OpenPosition with no symbol/currency: conid=%s",
                node.get("conid"),
            )
            continue
        positions.append(
            Position(
                source=SOURCE_NAME,
                account_id=_text(node.get("accountId")) or account_id,
                symbol=symbol,
                exchange=_text(node.get("listingExchange")),
                quantity=quantity,
                # Flex gives cost *per share* as costBasisPrice; the
                # aggregate is costBasisMoney. `avg_cost` is per-unit.
                avg_cost=_dec(node.get("costBasisPrice")),
                last_price=_dec(node.get("markPrice")),
                currency=currency,
                market_value=_dec(node.get("positionValue")),
                unrealized_pnl=_dec(node.get("fifoPnlUnrealized")),
                custody=SOURCE_NAME,
            )
        )
    return positions


def _parse_cash(
    statement: ElementTree.Element, account_id: str | None
) -> list[CashBalance]:
    balances: list[CashBalance] = []
    for node in statement.findall(".//CashReportCurrency"):
        currency = _text(node.get("currency"))
        if not currency or currency.upper() in _CASH_SUMMARY_ROWS:
            continue
        amount = _dec(node.get("endingCash"))
        if amount is None:
            continue
        balances.append(
            CashBalance(
                source=SOURCE_NAME,
                account_id=_text(node.get("accountId")) or account_id,
                currency=currency,
                amount=amount,
            )
        )
    return balances


def _parse_trades(
    statement: ElementTree.Element, account_id: str | None
) -> list[Transaction]:
    transactions: list[Transaction] = []
    for node in statement.findall(".//Trade"):
        quantity = _dec(node.get("quantity"))
        if quantity is None:
            continue
        # A forex row (`USD.CNH` on IDEALPRO) is a currency conversion, not
        # a trade in an instrument: there is no security to hold and no
        # symbol Ghostfolio could price. Its effect on the account is
        # already carried by the Cash Report, so importing it as a BUY
        # would invent a position that does not exist.
        if (_text(node.get("assetCategory")) or "").upper() == "CASH":
            _LOG.info(
                "skipping forex conversion %s (cash effect is in the Cash Report)",
                _text(node.get("symbol")),
            )
            continue

        side_raw = (_text(node.get("buySell")) or "").upper()
        if side_raw not in {"BUY", "SELL"}:
            # Corporate actions and assignments come through Trades with
            # other codes; leaving `type` to be derived from a code we do
            # not understand would silently invent a buy or a sell.
            _LOG.info("skipping Trade with unhandled buySell=%r", side_raw)
            continue

        # tradeID is IBKR-assigned and stable, which is exactly what the
        # idempotency ledger needs (§3.3) — never derive this from
        # anything we compute ourselves.
        external_id = (
            _text(node.get("tradeID"))
            or _text(node.get("transactionID"))
            or _text(node.get("ibOrderID"))
        )
        if not external_id:
            raise FlexError(
                "Trade row has no tradeID/transactionID; refusing to import "
                "it because there is no stable id to deduplicate on (§3.3)."
            )

        # IB reports commission as a negative number (money leaving the
        # account). The domain model wants the magnitude of the cost.
        commission = _dec(node.get("ibCommission"))
        fee = abs(commission) if commission is not None else None

        transactions.append(
            Transaction(
                source=SOURCE_NAME,
                account_id=_text(node.get("accountId")) or account_id,
                transaction_id=external_id,
                external_id=f"ibkr:trade:{external_id}",
                symbol=_text(node.get("symbol")),
                # `listingExchange`, not `exchange`: the latter is where the
                # order executed (IBKRATS, IEX), which says nothing about
                # where the instrument is listed.
                exchange=_text(node.get("listingExchange")),
                side=side_raw.lower(),
                type=(
                    TransactionType.BUY if side_raw == "BUY" else TransactionType.SELL
                ),
                quantity=abs(quantity),
                price=_dec(node.get("tradePrice")),
                currency=_text(node.get("currency")),
                amount=_dec(node.get("tradeMoney")),
                fee=fee,
                fee_currency=_text(node.get("ibCommissionCurrency")),
                timestamp=_parse_moment(
                    node.get("dateTime"),
                    node.get("tradeDate"),
                    node.get("reportDate"),
                ),
            )
        )
    return transactions


#: Flex `CashTransaction/@type` → normalised meaning (§6.3).
#:
#: Anything not listed is deliberately skipped rather than guessed: an
#: unrecognised cash movement mapped to the wrong type corrupts cost basis
#: in a way that is very hard to notice later.
_CASH_TX_TYPES: dict[str, TransactionType] = {
    "dividends": TransactionType.DIVIDEND,
    "payment in lieu of dividends": TransactionType.DIVIDEND,
    "broker interest received": TransactionType.INTEREST,
    "broker interest paid": TransactionType.INTEREST,
    "bond interest received": TransactionType.INTEREST,
    "withholding tax": TransactionType.FEE,
    "other fees": TransactionType.FEE,
    "commission adjustments": TransactionType.FEE,
}


def _parse_cash_transactions(
    statement: ElementTree.Element, account_id: str | None
) -> list[Transaction]:
    transactions: list[Transaction] = []
    for node in statement.findall(".//CashTransaction"):
        raw_type = (_text(node.get("type")) or "").lower()
        amount = _dec(node.get("amount"))
        if amount is None:
            continue

        if raw_type in {"deposits/withdrawals", "deposits & withdrawals"}:
            # Direction comes from the sign; both are excluded from the
            # Ghostfolio push (§6.3) but kept so the ledger is complete.
            tx_type = (
                TransactionType.DEPOSIT if amount > 0 else TransactionType.WITHDRAWAL
            )
        else:
            resolved = _CASH_TX_TYPES.get(raw_type)
            if resolved is None:
                _LOG.info("skipping CashTransaction with unmapped type=%r", raw_type)
                continue
            tx_type = resolved

        external_id = _text(node.get("transactionID"))
        if not external_id:
            _LOG.warning("skipping CashTransaction with no transactionID (type=%r)", raw_type)
            continue

        transactions.append(
            Transaction(
                source=SOURCE_NAME,
                account_id=_text(node.get("accountId")) or account_id,
                transaction_id=external_id,
                external_id=f"ibkr:cash:{external_id}",
                symbol=_text(node.get("symbol")),
                exchange=_text(node.get("listingExchange")),
                side=raw_type or None,
                type=tx_type,
                currency=_text(node.get("currency")),
                amount=amount,
                timestamp=_parse_moment(
                    node.get("dateTime"),
                    node.get("settleDate"),
                    node.get("reportDate"),
                ),
            )
        )
    return transactions


# ── client ──────────────────────────────────────────────────────────────


class FlexWebServiceClient:
    """Runs the two-leg Flex protocol and hands back parsed statements."""

    def __init__(
        self,
        config: FlexConfig,
        *,
        transport: FlexTransport | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._config = config
        self._transport = transport or HttpxFlexTransport(
            timeout=config.request_timeout
        )
        self._sleep = sleep

    async def aclose(self) -> None:
        closer = getattr(self._transport, "aclose", None)
        if closer is not None:
            await closer()

    async def request_reference_code(
        self, *, window: tuple[date, date] | None = None
    ) -> tuple[str, str | None]:
        """Ask IBKR to generate the report, optionally over a given window.

        `fd`/`td` override the period baked into the query definition —
        verified against the live service, which answers error 1023
        naming both parameters and a 366-day ceiling. That ceiling is why
        history older than a year has to be walked one window at a time
        rather than requested in one go.
        """
        params = {
            "t": self._config.token,
            "q": self._config.query_id,
            "v": FLEX_VERSION,
        }
        if window is not None:
            start, end = window
            params["fd"] = start.strftime("%Y%m%d")
            params["td"] = end.strftime("%Y%m%d")
        xml_text = await self._transport.get(
            f"{self._config.base_url}/SendRequest", params
        )
        return parse_reference_code(xml_text)

    async def fetch_statement(
        self, *, window: tuple[date, date] | None = None
    ) -> FlexStatement:
        """Request, wait for generation, and parse — the whole protocol.

        Polling is bounded: IBKR rate-limits Flex requests, so hammering
        it turns a slow report into a blocked token.
        """
        reference_code, url = await self.request_reference_code(window=window)
        # Follow the URL the server hands back rather than assuming the one
        # we sent to: SendRequest against ndcdyn answers with a *gdcdyn*
        # GetStatement URL, and hardcoding either host breaks the moment
        # IBKR moves the report tier.
        endpoint = url or f"{self._config.base_url}/GetStatement"

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._config.poll_timeout
        attempt = 0
        while True:
            attempt += 1
            xml_text = await self._transport.get(
                endpoint,
                {
                    "t": self._config.token,
                    "q": reference_code,
                    "v": FLEX_VERSION,
                },
            )
            try:
                return parse_statement(xml_text)
            except (TransientError, FlexStatementNotReadyError) as exc:
                if loop.time() >= deadline:
                    # Out of budget: surface the ambiguity as a hard error.
                    # An empty statement must never be allowed to look like
                    # a portfolio that holds nothing.
                    raise FlexError(
                        "IBKR never returned a usable statement after "
                        f"{self._config.poll_timeout:.0f}s ({attempt} attempts). "
                        f"Last response: {exc}"
                    ) from exc
                _LOG.info(
                    "Flex statement still generating; retrying in %.0fs",
                    self._config.poll_interval,
                )
                await self._sleep(self._config.poll_interval)


    async def fetch_history(
        self, *, start: date, end: date | None = None
    ) -> FlexStatement:
        """Walk the whole range in <=366-day windows and merge the results.

        Brokers stop reporting eventually, but IBKR's limit is per
        *request*, not per account: asking for an older window returns
        older trades. Without this, everything bought more than a year ago
        is invisible, Ghostfolio replays a smaller position than is really
        held, and every figure derived from it is wrong in the same
        direction (§6.4).

        Windows are walked newest-first so a rate limit or an outage
        part-way through still leaves the most recent history complete.
        """
        finish = end or datetime.now(UTC).date()
        merged = FlexStatement()
        seen_transactions: set[str] = set()
        span = timedelta(days=self._config.window_days)

        cursor_end = finish
        window_index = 0
        while cursor_end >= start:
            cursor_start = max(cursor_end - span, start)
            if window_index:
                await self._sleep(self._config.window_pause)
            window_index += 1

            try:
                statement = await self._fetch_window((cursor_start, cursor_end))
            except FlexStatementUnavailableError as exc:
                # Walked past the account's own history. Stop here rather
                # than grinding through every earlier window; what we
                # already have is complete back to this point.
                _LOG.info(
                    "history ends before %s (%s); stopping backfill",
                    cursor_start, exc,
                )
                break
            _LOG.info(
                "flex window %s..%s: %d position(s), %d transaction(s)",
                cursor_start, cursor_end,
                len(statement.positions), len(statement.transactions),
            )

            # Positions are a snapshot, not a period: only the newest
            # window's are current. Older windows would report holdings
            # that have since been sold.
            if window_index == 1:
                merged.positions = statement.positions
                merged.balances = statement.balances
                merged.generated_at = statement.generated_at
            for account_id in statement.account_ids:
                if account_id not in merged.account_ids:
                    merged.account_ids.append(account_id)

            for transaction in statement.transactions:
                key = transaction.external_id or transaction.transaction_id
                if key in seen_transactions:
                    # Adjacent windows share their boundary day.
                    continue
                seen_transactions.add(key)
                merged.transactions.append(transaction)

            if cursor_start <= start:
                break
            cursor_end = cursor_start - timedelta(days=1)

        merged.transactions.sort(key=lambda tx: tx.timestamp)
        _LOG.info(
            "flex backfill %s..%s: %d transaction(s) over %d window(s)",
            start, finish, len(merged.transactions), window_index,
        )
        return merged

    async def _fetch_window(self, window: tuple[date, date]) -> FlexStatement:
        """One window, waiting out rate limits rather than failing on them."""
        for attempt in range(1, self._config.rate_limit_attempts + 1):
            try:
                return await self.fetch_statement(window=window)
            except FlexRateLimitedError as exc:
                if attempt == self._config.rate_limit_attempts:
                    raise
                pause = self._config.rate_limit_backoff * attempt
                _LOG.warning(
                    "%s — waiting %.0fs before retrying window %s..%s",
                    exc, pause, window[0], window[1],
                )
                await self._sleep(pause)
        raise FlexError("unreachable")  # pragma: no cover


# ── adapter ─────────────────────────────────────────────────────────────


class IbkrFlexAdapter(SourceAdapter):
    """`SourceAdapter` over Flex Web Service.

    One statement contains positions, cash and trades, so all three
    accessors share a single cached fetch. Without the cache a portfolio
    refresh would trigger three separate report generations — slow, and a
    good way to hit the Flex rate limit.
    """

    source = SOURCE_NAME

    def __init__(
        self,
        client: FlexWebServiceClient,
        *,
        retry: RetryPolicy | None = None,
        health: HealthTracker | None = None,
        cache_ttl: float = 300.0,
    ) -> None:
        self._client = client
        # One attempt by default: the client already polls internally, and
        # a retry here would restart the whole two-leg protocol.
        self._retry = retry or RetryPolicy(max_attempts=1)
        self._health = health or HealthTracker(source=SOURCE_NAME)
        self._cache_ttl = cache_ttl
        self._cached: FlexStatement | None = None
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _statement(self) -> FlexStatement:
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            if self._cached is not None and now - self._cached_at < self._cache_ttl:
                return self._cached
            try:
                statement = await retry_async(
                    self._client.fetch_statement, policy=self._retry
                )
            except Exception as exc:
                self._health.record_failure(str(exc))
                raise
            self._health.record_success()
            self._cached = statement
            self._cached_at = now
            return statement

    def invalidate(self) -> None:
        """Drop the cached statement so the next read refetches."""
        self._cached = None
        self._cached_at = 0.0

    async def list_positions(self) -> list[Position]:
        return list((await self._statement()).positions)

    async def list_balances(self) -> list[CashBalance]:
        return list((await self._statement()).balances)

    async def list_transactions(
        self,
        *,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[Transaction]:
        """Return trades and cash movements from the statement.

        `since` filters what the query already returned; it cannot widen
        the window, because the range is fixed in the query definition on
        IBKR's side. That is a property of Flex, not an oversight — hence
        §4.1's instruction to define the query with a wide range.
        """
        transactions = (await self._statement()).transactions
        if since:
            cutoff = _parse_moment(since)
            transactions = [tx for tx in transactions if tx.timestamp >= cutoff]
        if limit is not None and limit >= 0:
            transactions = transactions[-limit:] if limit else []
        return list(transactions)

    async def stream_quotes(self, symbols: Iterable[str]) -> AsyncIterator[Quote]:
        """Flex is a reporting service; it has no market data.

        Prices come from Ghostfolio's own data provider (§7). This yields
        nothing rather than raising so a caller that fans out over every
        adapter is not broken by IBKR being report-only.
        """
        _ = symbols
        return
        yield  # pragma: no cover — makes this an async generator

    async def healthcheck(self) -> SourceHealth:
        try:
            await self._statement()
        except Exception as exc:  # noqa: BLE001 — health must not raise
            _LOG.warning("IBKR Flex healthcheck failed: %s", exc)
        return self._health.snapshot()


__all__ = [
    "DEFAULT_BASE_URL",
    "FlexAuthError",
    "FlexConfig",
    "FlexError",
    "FlexRateLimitedError",
    "FlexStatementNotReadyError",
    "FlexStatementUnavailableError",
    "FlexStatement",
    "FlexTransport",
    "FlexWebServiceClient",
    "HttpxFlexTransport",
    "IbkrFlexAdapter",
    "parse_reference_code",
    "parse_statement",
]
