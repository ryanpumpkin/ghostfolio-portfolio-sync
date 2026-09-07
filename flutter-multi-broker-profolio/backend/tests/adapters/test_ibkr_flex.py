"""Tests for the IBKR Flex Web Service adapter (spec §4.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.adapters._common import TransientError
from app.adapters.ibkr.flex import (
    FlexAuthError,
    FlexConfig,
    FlexError,
    FlexStatementNotReadyError,
    FlexWebServiceClient,
    IbkrFlexAdapter,
    parse_reference_code,
    parse_statement,
)
from app.models.domain import TransactionType
from app.services.adapter_factory import AdapterCredentialError, AdapterFactory

SEND_REQUEST_OK = """<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse timestamp="07 September, 2026 02:00 AM EDT">
  <Status>Success</Status>
  <ReferenceCode>1234567890</ReferenceCode>
  <Url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>
</FlexStatementResponse>
"""

IN_PROGRESS = """<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse timestamp="07 September, 2026 02:00 AM EDT">
  <Status>Fail</Status>
  <ErrorCode>1019</ErrorCode>
  <ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>
</FlexStatementResponse>
"""

BAD_TOKEN = """<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse timestamp="07 September, 2026 02:00 AM EDT">
  <Status>Fail</Status>
  <ErrorCode>1012</ErrorCode>
  <ErrorMessage>Token has expired.</ErrorMessage>
</FlexStatementResponse>
"""

NOT_READY_YET = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="mbp-portfolio" type="AF">
</FlexQueryResponse>
"""

STATEMENT = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="portfolio" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" fromDate="20170101" toDate="20260907"
                   whenGenerated="20260907;020000">
      <OpenPositions>
        <OpenPosition accountId="U1234567" currency="USD" symbol="VOO"
                      description="VANGUARD S&amp;P 500 ETF" conid="136155102"
                      assetCategory="STK" position="12" markPrice="512.40"
                      positionValue="6148.80" costBasisPrice="470.10"
                      costBasisMoney="5641.20" fifoPnlUnrealized="507.60"
                      listingExchange="ARCA" />
        <OpenPosition accountId="U1234567" currency="HKD" symbol="2800"
                      description="TRACKER FUND OF HONG KONG" conid="12345"
                      assetCategory="STK" position="500" markPrice="21.50"
                      positionValue="10750" costBasisPrice="20.00"
                      costBasisMoney="10000" fifoPnlUnrealized="750"
                      listingExchange="SEHK" />
        <OpenPosition accountId="U1234567" currency="USD" symbol="CLOSED"
                      position="0" markPrice="1" positionValue="0" />
      </OpenPositions>
      <CashReport>
        <CashReportCurrency accountId="U1234567" currency="BASE_SUMMARY"
                            endingCash="9999999" />
        <CashReportCurrency accountId="U1234567" currency="USD"
                            endingCash="43.64" />
        <CashReportCurrency accountId="U1234567" currency="HKD"
                            endingCash="713.23" />
      </CashReport>
      <Trades>
        <Trade accountId="U1234567" currency="USD" symbol="VOO"
               tradeID="7788990011" dateTime="20260415;103000" quantity="2"
               tradePrice="498.10" tradeMoney="996.20" ibCommission="-1.05"
               ibCommissionCurrency="USD" buySell="BUY" assetCategory="STK"
               exchange="IBKRATS" listingExchange="ARCA" />
        <Trade accountId="U1234567" currency="HKD" symbol="2800"
               tradeID="7788990012" dateTime="20260501;140000" quantity="-100"
               tradePrice="21.00" tradeMoney="-2100" ibCommission="-18"
               ibCommissionCurrency="HKD" buySell="SELL" assetCategory="STK"
               exchange="SEHKNTL" listingExchange="SEHK" />
        <Trade accountId="U1234567" currency="USD" symbol="MYSTERY"
               tradeID="7788990013" dateTime="20260502;140000" quantity="5"
               tradePrice="10" buySell="EXCH" assetCategory="STK" />
        <Trade accountId="U1234567" currency="CNH" symbol="USD.CNH"
               tradeID="7788990014" dateTime="20260503;140000" quantity="1000"
               tradePrice="7.1" tradeMoney="-7100" buySell="BUY"
               assetCategory="CASH" exchange="IDEALPRO" />
      </Trades>
      <CashTransactions>
        <CashTransaction accountId="U1234567" currency="USD" symbol="VOO"
                         transactionID="55501" type="Dividends"
                         amount="14.22" dateTime="20260620" />
        <CashTransaction accountId="U1234567" currency="USD"
                         transactionID="55502" type="Deposits/Withdrawals"
                         amount="5000" dateTime="20260101" />
        <CashTransaction accountId="U1234567" currency="USD"
                         transactionID="55503" type="Deposits/Withdrawals"
                         amount="-250" dateTime="20260201" />
        <CashTransaction accountId="U1234567" currency="USD"
                         transactionID="55504" type="Some New Thing"
                         amount="3" dateTime="20260301" />
      </CashTransactions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


class FakeTransport:
    """Replays canned bodies in order and records the params it was given."""

    def __init__(self, *bodies: str) -> None:
        self.bodies = list(bodies)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, params: dict[str, str]) -> str:
        self.calls.append((url, params))
        return self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]


def _client(*bodies: str, **overrides) -> tuple[FlexWebServiceClient, FakeTransport]:
    transport = FakeTransport(*bodies)
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    config = FlexConfig(token="tok", query_id="q1", poll_interval=0.0, **overrides)
    client = FlexWebServiceClient(config, transport=transport, sleep=_sleep)
    return client, transport


# ── parsing ─────────────────────────────────────────────────────────────


def test_parse_reference_code() -> None:
    code, url = parse_reference_code(SEND_REQUEST_OK)
    assert code == "1234567890"
    assert url and url.endswith("/GetStatement")


def test_in_progress_is_transient_not_fatal() -> None:
    # The most common response to the first GetStatement. Treating it as an
    # error would make every sync fail on a race it is supposed to wait out.
    with pytest.raises(TransientError):
        parse_statement(IN_PROGRESS)


def test_expired_token_is_not_retried() -> None:
    with pytest.raises(FlexAuthError):
        parse_statement(BAD_TOKEN)


def test_positions_parsed_with_decimals() -> None:
    statement = parse_statement(STATEMENT)
    assert statement.account_ids == ["U1234567"]
    by_symbol = {p.symbol: p for p in statement.positions}

    # A zero-quantity row is a closed position, not a holding.
    assert "CLOSED" not in by_symbol
    assert len(by_symbol) == 2

    voo = by_symbol["VOO"]
    assert voo.quantity == Decimal("12")
    assert voo.avg_cost == Decimal("470.10")
    assert voo.market_value == Decimal("6148.80")
    assert voo.currency == "USD"
    assert voo.exchange == "ARCA"
    assert voo.custody == "ibkr"
    assert isinstance(voo.quantity, Decimal)


def test_cash_summary_row_is_excluded() -> None:
    # BASE_SUMMARY is a roll-up; including it double-counts everything.
    balances = {b.currency: b.amount for b in parse_statement(STATEMENT).balances}
    assert balances == {"USD": Decimal("43.64"), "HKD": Decimal("713.23")}


def test_trades_normalised() -> None:
    trades = [
        tx for tx in parse_statement(STATEMENT).transactions
        if tx.external_id and tx.external_id.startswith("ibkr:trade:")
    ]
    # The EXCH row is skipped rather than guessed into a buy or a sell.
    assert len(trades) == 2

    buy, sell = trades
    assert buy.type is TransactionType.BUY
    assert buy.quantity == Decimal("2")
    assert buy.price == Decimal("498.10")
    # IB reports commission negative; the cost is stored as a magnitude.
    assert buy.fee == Decimal("1.05")
    assert buy.fee_currency == "USD"
    assert buy.timestamp == datetime(2026, 4, 15, 10, 30, tzinfo=UTC)
    assert buy.external_id == "ibkr:trade:7788990011"

    # listingExchange, not the execution venue: `resolve()` needs where
    # the instrument is listed to place a bare ticker like VOO.
    assert buy.exchange == "ARCA"
    assert sell.exchange == "SEHK"

    assert sell.type is TransactionType.SELL
    # Quantity is a magnitude even though IB signs sells negative.
    assert sell.quantity == Decimal("100")
    assert sell.fee == Decimal("18")


def test_cash_transactions_typed_by_sign_and_name() -> None:
    cash = {
        tx.external_id: tx for tx in parse_statement(STATEMENT).transactions
        if tx.external_id and tx.external_id.startswith("ibkr:cash:")
    }
    # "Some New Thing" is unmapped and therefore skipped, not guessed.
    assert set(cash) == {
        "ibkr:cash:55501",
        "ibkr:cash:55502",
        "ibkr:cash:55503",
    }
    assert cash["ibkr:cash:55501"].type is TransactionType.DIVIDEND
    assert cash["ibkr:cash:55502"].type is TransactionType.DEPOSIT
    assert cash["ibkr:cash:55503"].type is TransactionType.WITHDRAWAL


def test_transactions_sorted_by_time() -> None:
    stamps = [tx.timestamp for tx in parse_statement(STATEMENT).transactions]
    assert stamps == sorted(stamps)


def test_empty_statement_is_an_error_not_an_empty_portfolio() -> None:
    # The dangerous failure mode: a response with no statements must not be
    # read as "the account holds nothing". It stays a FlexError even though
    # the client treats it as retryable while it still has poll budget.
    with pytest.raises(FlexError, match="no <FlexStatement>"):
        parse_statement('<FlexQueryResponse queryName="x"></FlexQueryResponse>')
    with pytest.raises(FlexStatementNotReadyError):
        parse_statement(NOT_READY_YET)


def test_garbage_is_reported_as_such() -> None:
    with pytest.raises(FlexError, match="unparseable XML"):
        parse_statement("<html>maintenance</html")


# ── protocol ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_leg_protocol_polls_until_ready() -> None:
    client, transport = _client(SEND_REQUEST_OK, IN_PROGRESS, IN_PROGRESS, STATEMENT)
    statement = await client.fetch_statement()

    assert len(statement.positions) == 2
    # SendRequest, then three GetStatements.
    assert len(transport.calls) == 4
    assert transport.calls[0][0].endswith("/SendRequest")
    assert transport.calls[0][1] == {"t": "tok", "q": "q1", "v": "3"}
    # Subsequent legs carry the reference code, not the query id.
    assert transport.calls[1][1]["q"] == "1234567890"


@pytest.mark.asyncio
async def test_polling_gives_up_instead_of_hammering() -> None:
    client, _ = _client(SEND_REQUEST_OK, IN_PROGRESS, poll_timeout=0.0)
    with pytest.raises(FlexError, match="never returned a usable statement"):
        await client.fetch_statement()


@pytest.mark.asyncio
async def test_auth_failure_stops_immediately() -> None:
    client, transport = _client(SEND_REQUEST_OK, BAD_TOKEN)
    with pytest.raises(FlexAuthError):
        await client.fetch_statement()
    assert len(transport.calls) == 2  # no polling on a permanent failure


# ── adapter ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_fetch_serves_all_three_accessors() -> None:
    client, transport = _client(SEND_REQUEST_OK, STATEMENT)
    adapter = IbkrFlexAdapter(client)

    positions = await adapter.list_positions()
    balances = await adapter.list_balances()
    transactions = await adapter.list_transactions()

    assert len(positions) == 2
    assert len(balances) == 2
    assert len(transactions) == 5
    # Two calls total — the statement is fetched once, not three times.
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_since_filters_but_cannot_widen() -> None:
    client, _ = _client(SEND_REQUEST_OK, STATEMENT)
    adapter = IbkrFlexAdapter(client)
    recent = await adapter.list_transactions(since="20260501")
    assert [tx.external_id for tx in recent] == [
        "ibkr:trade:7788990012",
        "ibkr:cash:55501",
    ]


@pytest.mark.asyncio
async def test_stream_quotes_is_empty_not_an_error() -> None:
    client, _ = _client(SEND_REQUEST_OK, STATEMENT)
    adapter = IbkrFlexAdapter(client)
    assert [q async for q in adapter.stream_quotes(["VOO"])] == []


@pytest.mark.asyncio
async def test_healthcheck_reports_failure_without_raising() -> None:
    client, _ = _client(SEND_REQUEST_OK, BAD_TOKEN)
    adapter = IbkrFlexAdapter(client)
    health = await adapter.healthcheck()
    assert health.source == "ibkr"
    assert health.status != "ok"


# ── factory wiring ──────────────────────────────────────────────────────


def test_factory_builds_flex_when_token_present() -> None:
    adapter = AdapterFactory().for_connection(
        connection_kind="ibkr",
        plaintext_creds='{"flexToken": "tok", "flexQueryId": "q1"}',
    )
    assert isinstance(adapter, IbkrFlexAdapter)


def test_factory_rejects_half_configured_flex() -> None:
    # Silently falling back to the Gateway would be the worst outcome: the
    # owner thinks they are on the read-only path and are not.
    with pytest.raises(AdapterCredentialError, match="both flexToken"):
        AdapterFactory().for_connection(
            connection_kind="ibkr",
            plaintext_creds='{"flexToken": "tok"}',
        )


@pytest.mark.asyncio
async def test_empty_document_is_polled_through() -> None:
    """Observed live: the first GetStatement can be empty but successful.

    IBKR answered a fresh reference code with a well-formed
    FlexQueryResponse containing no FlexStatement, then returned the full
    94KB report moments later. Treating the first one as fatal made every
    cold sync fail.
    """
    client, transport = _client(SEND_REQUEST_OK, NOT_READY_YET, STATEMENT)
    statement = await client.fetch_statement()
    assert len(statement.positions) == 2
    assert len(transport.calls) == 3


@pytest.mark.asyncio
async def test_persistently_empty_document_still_fails_loudly() -> None:
    # If it never fills in, the answer is an error — never an empty
    # portfolio.
    client, _ = _client(SEND_REQUEST_OK, NOT_READY_YET, poll_timeout=0.0)
    with pytest.raises(FlexError, match="never returned a usable statement"):
        await client.fetch_statement()


def test_forex_conversion_is_not_a_trade() -> None:
    """`USD.CNH` on IDEALPRO is a cash conversion, not a holding.

    Live data contained three of these. Importing one as a BUY invents a
    position in an instrument that does not exist; the cash effect is
    already carried by the Cash Report.
    """
    symbols = [tx.symbol for tx in parse_statement(STATEMENT).transactions]
    assert "USD.CNH" not in symbols


# ── multi-window backfill ───────────────────────────────────────────────

RATE_LIMITED = """<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse timestamp="07 September, 2026 02:00 AM EDT">
  <Status>Fail</Status>
  <ErrorCode>1018</ErrorCode>
  <ErrorMessage>Too many requests have been made from this token. Please try again shortly.</ErrorMessage>
</FlexStatementResponse>
"""

OLDER_STATEMENT = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="portfolio" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" fromDate="20240101" toDate="20241231">
      <Trades>
        <Trade accountId="U1234567" currency="USD" symbol="VOO"
               tradeID="1111" dateTime="20240315;100000" quantity="3"
               tradePrice="400" buySell="BUY" assetCategory="STK"
               listingExchange="ARCA" />
      </Trades>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


class ScriptedTransport:
    """Returns a canned body per call and records every request."""

    def __init__(self, bodies: list[str]) -> None:
        self.bodies = bodies
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, params: dict[str, str]) -> str:
        self.calls.append((url, params))
        return self.bodies[min(len(self.calls), len(self.bodies)) - 1]


def _windowed_client(bodies: list[str], **overrides):
    transport = ScriptedTransport(bodies)
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    config = FlexConfig(
        token="tok", query_id="q1", poll_interval=0.0,
        window_pause=0.0, rate_limit_backoff=0.0, **overrides,
    )
    return FlexWebServiceClient(config, transport=transport, sleep=_sleep), transport


@pytest.mark.asyncio
async def test_backfill_sends_fd_and_td_per_calendar_year() -> None:
    """A single request cannot span more than 366 days (error 1023).

    History older than a year is therefore invisible unless it is walked
    one window at a time — which is exactly why the derived position was
    smaller than the real one.
    """
    from datetime import date

    client, transport = _windowed_client([SEND_REQUEST_OK, STATEMENT] * 4)
    await client.fetch_history(start=date(2024, 1, 1), end=date(2026, 6, 30))

    sends = [p for url, p in transport.calls if url.endswith("/SendRequest")]
    assert len(sends) == 3  # 2026, 2025, 2024
    # Newest first, so an interruption leaves recent history complete.
    assert [p["fd"] for p in sends] == ["20260101", "20250101", "20240101"]
    assert [p["td"] for p in sends] == ["20260630", "20251231", "20241231"]


def test_calendar_windows_never_exceed_the_366_day_ceiling() -> None:
    from datetime import date

    from app.adapters.ibkr.flex import calendar_year_windows

    windows = calendar_year_windows(date(2019, 3, 4), date(2026, 9, 6))
    assert windows[0] == (date(2026, 1, 1), date(2026, 9, 6))
    assert windows[-1] == (date(2019, 3, 4), date(2019, 12, 31))
    assert all((end - start).days < 366 for start, end in windows)


@pytest.mark.asyncio
async def test_backfill_merges_and_deduplicates() -> None:
    from datetime import date

    client, _ = _windowed_client(
        [SEND_REQUEST_OK, STATEMENT, SEND_REQUEST_OK, OLDER_STATEMENT],
    )
    merged = await client.fetch_history(
        start=date(2025, 1, 1), end=date(2026, 9, 1)
    )
    ids = [tx.external_id for tx in merged.transactions]
    assert "ibkr:trade:1111" in ids
    # Adjacent windows share a boundary day; a repeat must not double.
    assert len(ids) == len(set(ids))
    # Positions are a snapshot, so only the newest window's survive —
    # older ones would report holdings since sold.
    assert len(merged.positions) == 2


@pytest.mark.asyncio
async def test_rate_limit_is_waited_out_not_treated_as_failure() -> None:
    from datetime import date

    client, transport = _windowed_client(
        [SEND_REQUEST_OK, RATE_LIMITED, SEND_REQUEST_OK, STATEMENT],
    )
    merged = await client.fetch_history(
        start=date(2026, 1, 1), end=date(2026, 6, 1)
    )
    assert merged.positions
    assert len(transport.calls) >= 3


UNAVAILABLE = """<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse timestamp="07 September, 2026 02:00 AM EDT">
  <Status>Fail</Status>
  <ErrorCode>1003</ErrorCode>
  <ErrorMessage>Statement is not available.</ErrorMessage>
</FlexStatementResponse>
"""


@pytest.mark.asyncio
async def test_backfill_survives_an_unavailable_window() -> None:
    """Error 1003 means "no statement for this period", not a failure.

    It must not abort the run and discard everything already collected.
    """
    from datetime import date

    client, transport = _windowed_client(
        [SEND_REQUEST_OK, STATEMENT, UNAVAILABLE],
    )
    merged = await client.fetch_history(
        start=date(2015, 1, 1), end=date(2026, 9, 1)
    )
    assert merged.transactions
    # And it stopped rather than grinding through ten more empty years.
    assert len(transport.calls) < 8


@pytest.mark.asyncio
async def test_one_unavailable_window_does_not_end_the_backfill() -> None:
    """A refused window can sit between two that hold data.

    Live: a 364-day window spanning a year boundary was refused with 1003
    while both calendar years it covered returned trades. Stopping on the
    first refusal silently truncated two years of real history and made
    every derived position too small.
    """
    from datetime import date

    class Sequence:
        def __init__(self, bodies):
            self.bodies = list(bodies)
            self.calls = []

        async def get(self, url, params):
            self.calls.append((url, params))
            return self.bodies.pop(0) if self.bodies else UNAVAILABLE

    transport = Sequence([
        SEND_REQUEST_OK, STATEMENT,        # 2026
        UNAVAILABLE,                        # 2025 refused
        SEND_REQUEST_OK, OLDER_STATEMENT,   # 2024 has data
    ])

    async def _sleep(_seconds: float) -> None:
        return None

    client = FlexWebServiceClient(
        FlexConfig(token="t", query_id="q", poll_interval=0.0,
                   window_pause=0.0, rate_limit_backoff=0.0),
        transport=transport, sleep=_sleep,
    )
    merged = await client.fetch_history(
        start=date(2024, 1, 1), end=date(2026, 9, 1)
    )
    ids = [tx.external_id for tx in merged.transactions]
    assert "ibkr:trade:1111" in ids  # the 2024 window survived the 2025 gap


def test_unavailable_is_not_read_as_an_empty_portfolio() -> None:
    with pytest.raises(FlexError, match="no statement for this period"):
        parse_statement(UNAVAILABLE)


@pytest.mark.asyncio
async def test_a_failed_newest_window_does_not_leave_the_portfolio_empty() -> None:
    """Positions come from the first window that *returned*.

    Live near-miss: the 2026 window failed, positions were keyed on being
    first in the walk rather than on succeeding, and the gap analysis ran
    against an empty position list — reporting "nothing missing" for a
    portfolio it could not see at all.
    """
    from datetime import date

    class Sequence:
        def __init__(self, bodies):
            self.bodies = list(bodies)

        async def get(self, url, params):
            return self.bodies.pop(0) if self.bodies else UNAVAILABLE

    async def _sleep(_s: float) -> None:
        return None

    client = FlexWebServiceClient(
        FlexConfig(token="t", query_id="q", poll_interval=0.0,
                   window_pause=0.0, rate_limit_backoff=0.0),
        transport=Sequence([UNAVAILABLE, SEND_REQUEST_OK, STATEMENT]),
        sleep=_sleep,
    )
    merged = await client.fetch_history(
        start=date(2025, 1, 1), end=date(2026, 9, 1)
    )
    assert len(merged.positions) == 2
    # And the hole is declared, not hidden.
    assert not merged.complete
    assert merged.missing_windows


@pytest.mark.asyncio
async def test_a_complete_backfill_says_so() -> None:
    from datetime import date

    client, _ = _windowed_client([SEND_REQUEST_OK, STATEMENT] * 3)
    merged = await client.fetch_history(
        start=date(2026, 1, 1), end=date(2026, 6, 1)
    )
    assert merged.complete
