# Broker Integration — Per-Broker Detailed Plans

Companion to `doc/POST_MVP_PLAN.md` item 2. LongBridge is fully
wired and proven end-to-end (see `doc/RUNBOOK.md`). This document
covers the remaining three brokers — Binance, IBKR, Futu — with
**real API response shapes** so the next agent doesn't have to
guess SDK return types.

For each broker we capture:
- Where to sign up and get keys
- Required scopes / permissions
- Exact SDK call signatures
- **Sample raw responses** (verified against real APIs or from the
  official docs)
- Specific quirks already encoded in the existing adapter
- Sub-tasks ordered by dependency
- Where the data should land in the Flutter UI
- Definition of done

Patterns to copy from LongBridge:

1. **Per-broker `client.py` returns plain dicts**, not SDK
   dataclasses. Build a `_position_to_dict(raw)` helper if the
   SDK returns immutable objects.
2. **The aggregator already maps `dict → Position/CashBalance/Transaction`**
   via each adapter's `_map_*` functions. Make sure those
   accept both snake_case and camelCase keys.
3. **Enrich with live quotes** in the client when the broker's
   positions endpoint returns `null` last_price (we did this
   for LongBridge).
4. **Don't trust market_value from the broker** — recompute it
   from `last_price × quantity` so currency conversion stays
   consistent.

---

## A. Binance

> **RETIRED AFTER IMPORT (spec §5.0).** The owner is not continuing to use
> Binance — Futu is the ongoing crypto venue (§4.4). The integration
> exists for exactly one purpose: recover the cost basis of coins already
> bought here, so the BTC/ETH/DOGE now on the Ledger are not orphaned.
>
> The live adapter in `app/adapters/binance/` is **not** the import path
> and must not be used for it: it defaults to a 90-day lookback, caps at
> 20 symbols, and reads neither Convert nor dust history — against §5.5's
> "a missed trade is permanent", all three lose data silently.
>
> Use `backend/tools/binance_import/` instead. It is a script, is
> deliberately not registered with the scheduler, and archives every raw
> response before normalising (§5.5) because the key is revoked
> afterwards.
>
> After a successful run and the §5.11 checks: **revoke the API key and
> delete the credential from the encrypted store.**


**Difficulty:** ★ (easiest — no sidecar, REST + WS only)
**Estimated effort:** 1-2 hours

### A.1 Account setup

1. Sign in at https://www.binance.com (or https://www.binance.us
   depending on your region).
2. **Account → API Management → Create API**.
3. Name the key (e.g. `mbp-tracker-readonly`).
4. After 2FA, you'll see **API Key** + **Secret Key**.
5. **Critical permissions setting:** check ONLY
   `Enable Reading`. Leave `Enable Spot & Margin Trading`,
   `Enable Withdrawals`, `Enable Margin`, `Enable Futures` all
   **OFF**. Binance enforces this server-side; the
   `BinanceClient` also re-validates and refuses to instantiate
   if `canTrade` or `canWithdraw` are true.
6. **IP whitelist** (recommended): restrict to your home/server IP.

### A.2 Endpoints used

| Use | Endpoint | SDK call |
|---|---|---|
| Account + balances | `GET /api/v3/account` | `client.get_account()` |
| Trade history | `GET /api/v3/myTrades?symbol=X` | `client.get_my_trades(symbol=X)` |
| Spot price | `GET /api/v3/ticker/price` | `client.get_symbol_ticker(symbol=X)` |
| 24h kline | `GET /api/v3/klines` | `client.get_klines(symbol=X, interval='1d', limit=1)` |
| WS quote stream | `wss://stream.binance.com:9443/ws/<symbol>@trade` | `ThreadedWebsocketManager.start_trade_socket(...)` |

### A.3 Sample raw responses

`get_account()` returns:

```json
{
  "makerCommission": 10,
  "takerCommission": 10,
  "buyerCommission": 0,
  "sellerCommission": 0,
  "canTrade": false,
  "canWithdraw": false,
  "canDeposit": true,
  "accountType": "SPOT",
  "updateTime": 1747614000000,
  "permissions": ["SPOT"],
  "balances": [
    { "asset": "BTC", "free": "0.05123400", "locked": "0.00000000" },
    { "asset": "USDT", "free": "150.20000000", "locked": "0.00000000" },
    { "asset": "ETH", "free": "0.00000000", "locked": "0.00000000" },
    { "asset": "BNB", "free": "0.85000000", "locked": "0.00000000" }
  ]
}
```

Notes:
- Balances are returned for EVERY asset Binance offers, most
  with `free=0` `locked=0`. **Filter to non-zero balances** when
  mapping to domain.
- `free` and `locked` are decimal strings, not floats. Keep them
  as strings until the boundary.
- Crypto balances live in `balances[]` — model them as
  **CashBalance** entries (each asset is a "currency") OR as
  **Position** entries with `symbol=BTC` and `currency=USD`. The
  existing adapter models them as positions priced in USD via a
  per-symbol ticker call. Keep that pattern.

`get_symbol_ticker(symbol="BTCUSDT")`:

```json
{ "symbol": "BTCUSDT", "price": "104327.50000000" }
```

`get_my_trades(symbol="BTCUSDT")`:

```json
[
  {
    "symbol": "BTCUSDT",
    "id": 28457,
    "orderId": 100234,
    "orderListId": -1,
    "price": "98000.10000000",
    "qty": "0.01000000",
    "quoteQty": "980.00100000",
    "commission": "0.00001000",
    "commissionAsset": "BTC",
    "time": 1736950000000,
    "isBuyer": true,
    "isMaker": false,
    "isBestMatch": true
  }
]
```

Notes:
- `time` is **milliseconds since epoch**, not ISO.
- `myTrades` is **per-symbol only**. To get the full history,
  iterate every symbol the user has ever traded. Keep a list of
  "interesting" trading pairs derived from current balances and
  enumerate them. Binance also has a 24h rolling rate limit of
  1200 weight units/min — `myTrades` costs 10 each, so cap calls.

### A.4 SDK quirks

The wrapper in `backend/app/adapters/binance/client.py` is
`HttpxBinanceClient` (we wrote a thin httpx wrapper rather than
import `python-binance` to keep the dependency surface small).
HMAC-SHA256 query signing is in `sign_query`. Verify against
the spec at
https://binance-docs.github.io/apidocs/spot/en/#endpoint-security-type

If you swap to `python-binance` (already in pyproject.toml):

```python
from binance.client import Client
client = Client(api_key, api_secret, tld='com')  # or 'us'
```

`python-binance` also has async via
`binance.AsyncClient.create(...)`. The existing `BinanceAdapter`
expects an injected client object; use that.

### A.5 Sub-tasks

- [ ] Create read-only API key on Binance, store in
  `.env.test` for local integration tests (gitignored).
- [ ] Verify `BinanceCredentials` rejection logic for
  `canTrade=true` / `canWithdraw=true` (already coded;
  re-test with a real account).
- [ ] In `backend/app/adapters/binance/client.py`, implement
  `list_balances` returning **non-zero only** balances mapped
  to `CashBalance` records.
- [ ] Implement `list_positions` that:
  1. Reads balances from `account()`.
  2. For each non-stablecoin asset (BTC, ETH, BNB, etc.)
     with `free > 0`, treat as a position.
  3. Calls `get_symbol_ticker(symbol=ASSET+'USDT')` for the
     current price. Cache per-request.
  4. Computes `market_value = quantity * price`,
     `avg_cost = ?` (Binance doesn't return cost basis;
     either leave null or compute from trade history).
- [ ] Implement `list_transactions` that calls `myTrades` for
  every symbol the user has held in the last 90 days. Cap to
  20 symbols per refresh to stay under rate limits.
- [ ] In the Flutter Add Connection dialog the fields are
  already wired: `apiKey`, `apiSecret`, `region`. Verify region
  selection routes to `binance.com` vs `binance.us` correctly
  via `BinanceHost`.
- [ ] Add env-gated integration test:
  ```
  BINANCE_API_KEY=...
  BINANCE_API_SECRET=...
  BINANCE_REGION=com
  ```
- [ ] Manual smoke: real key, dashboard refresh shows ≥1
  non-zero balance and a position row with a live price.

### A.6 Where it appears in the app

- **Dashboard:** Binance source tile under `Sources`, status
  badge becomes `ok`.
- **Positions:** Crypto holdings sortable alongside stocks.
  `assetClass` mapper should return `crypto`.
- **Transactions:** Buy/sell records per trading pair.
- **Allocation donut:** Crypto holdings contribute to the
  currency breakdown (default to mapping USDT/USDC/BUSD → USD,
  treat native crypto as their own currency).

### A.7 Definition of done

- Dashboard `Sources` row labeled `binance` shows `Healthy`.
- `lastSyncAt` updates on refresh.
- At least one crypto balance appears with a live price.
- Integration test passes when env vars are present.
- All standard backend gates green.

---

## B. Interactive Brokers (IBKR)

**Difficulty:** ★ via Flex Web Service, ★★★ via the Gateway
**Estimated effort:** ~30 min (Flex) / 3-4 hours (Gateway)

There are two ways in and they are not equivalent. **Flex Web Service is
the one we use** (§4.1); the Gateway path below is kept only for the
interactive case.

### B.0 Flex Web Service — the path we actually use

Chosen after the Gateway path was built and tried against the live
account. What went wrong there is the argument for this one:

- IB Gateway needs a running GUI process and a **daily** interactive
  re-login. A scheduled overnight sync cannot tap a second factor.
- It authenticates with the **account login password**, which then has to
  live where the container can read it — it was found sitting in plain
  text in the container environment, readable by anyone with `docker
  inspect`.
- The 2FA push never arrived on the owner's device, and
  `TWOFA_TIMEOUT_ACTION=exit` meant the container simply gave up.

Flex has none of that: a read-only token, two plain GETs, no session, no
second factor, and no ability to place an order even if the token leaks.

**Setup (in Account Management, once):**

1. **Performance & Reports → Flex Queries → Activity Flex Query → +**
2. Enable at minimum these sections, or the adapter has nothing to read:
   - **Open Positions** — include `symbol`, `currency`, `position`,
     `markPrice`, `positionValue`, `costBasisPrice`, `fifoPnlUnrealized`,
     `listingExchange`, `accountId`
   - **Cash Report** — include `currency`, `endingCash`, `accountId`
   - **Trades** — include `tradeID`, `dateTime`, `symbol`, `buySell`,
     `quantity`, `tradePrice`, `tradeMoney`, `ibCommission`,
     `ibCommissionCurrency`, `currency`, `accountId`
   - **Cash Transactions** (optional but wanted) — include
     `transactionID`, `type`, `amount`, `currency`, `symbol`, `dateTime`
3. **Open the date window wide.** The range lives in the query
   definition, not in the request — there is no runtime parameter that
   can widen it later. Use the maximum period the account allows.
4. Format **XML**, version **3**.
5. Save, then note the **Query ID**.
6. **Settings → Account Settings → Flex Web Service → configure**,
   enable it, and generate the **token**. It is read-only, expires on its
   own, and is not the account password.

**Credentials** (stored in the encrypted credential store, kind `ibkr`):

```json
{ "flexToken": "<token>", "flexQueryId": "<query id>" }
```

Supplying only one of the two is rejected rather than silently falling
back to the Gateway — see `_build_ibkr_adapter`.

**Protocol** (`app/adapters/ibkr/flex.py`), two legs because IBKR
generates the report asynchronously:

```
GET SendRequest?t=<token>&q=<queryId>&v=3   -> <ReferenceCode>
GET GetStatement?t=<token>&q=<refCode>&v=3  -> statement XML, or a
                                               "still generating" Fail
```

Both legs answer **HTTP 200 whatever happens**, and failures are signalled
inside the XML body. A client that only checks the status code parses an
error document as an empty portfolio — which looks exactly like "you
sold everything". `_check_response_status` exists for that reason, and
`parse_statement` raises on a response with no `<FlexStatement>` rather
than returning an empty result.

---

The rest of this section documents the **Gateway** path, which is no
longer the default.

### B.1 Account setup

1. Sign in at https://www.interactivebrokers.com
2. **Settings → Account Settings → API → Settings**:
   - Enable **Read-Only API**.
   - Enable **ActiveX and Socket Clients** (for TWS/IBGW path).
   - **OR** for the Client Portal Web API path, just register
     an app at https://www.interactivebrokers.com/sso/Client?action=API
3. Note your account ID (typically `U1234567` for individual,
   `DU1234567` for paper trading demo).
4. Choose your gateway:
   - **Client Portal Web API Gateway** — runs as a Java app or
     Docker image; exposes REST API on port 5000. Recommended
     for headless servers but requires periodic re-auth (~24h).
   - **TWS / IB Gateway** — traditional Windows-style desktop
     app, exposes socket API on port 7497 (TWS paper),
     7496 (TWS live), 4001 (IBGW paper), 4002 (IBGW live).

We use `ib_insync` which speaks the TWS/IBGW socket API.

### B.2 Gateway sidecar

`docker-compose.yml` already references
`ghcr.io/unusualwhale/ibkr-cpapi:latest`. Confirm or substitute.
Alternative images:
- `ghcr.io/extrange/ibkr` — runs IB Gateway with novnc for
  graphical auth.
- `manmolecular/ib-gateway-docker` — older but well-documented.

After `docker compose up ibkr-gateway`, **open the gateway's
web UI** (port mapping is in docker-compose) and complete the
2FA login. The session persists in the `ibkr-state` volume.

### B.3 Endpoints used (ib_insync)

```python
from ib_insync import IB, Stock

ib = IB()
ib.connect('ibkr-gateway', 7497, clientId=1, readonly=True, account='U1234567')

positions = ib.positions('U1234567')
summary = ib.accountSummary('U1234567')
trades = ib.trades()
contract = Stock('AAPL', 'SMART', 'USD')
tickers = ib.reqTickers(contract)
```

### B.4 Sample raw responses

`ib.positions(account='U1234567')` returns a list of
`Position(account, contract, position, avgCost)`:

```python
[
  Position(
    account='U1234567',
    contract=Stock(
      conId=265598,
      symbol='AAPL',
      secType='STK',
      currency='USD',
      exchange='SMART',
      primaryExchange='NASDAQ',
      localSymbol='AAPL'
    ),
    position=100.0,
    avgCost=185.50,
  ),
  Position(
    account='U1234567',
    contract=Forex(symbol='EUR.USD'),
    position=5000.0,
    avgCost=1.0820,
  ),
]
```

Notes:
- `position` is a Decimal-able number (shares, contracts).
- `avgCost` is the **per-share** cost in the contract currency.
- `contract.localSymbol` is often what to display
  (e.g. `BRK B` instead of `BRK B`).
- `secType` can be `STK`, `OPT`, `FUT`, `CASH`, `CRYPTO`, etc.
  v1 scope: `STK` only.

`ib.accountSummary(account='U1234567')` returns a list of
`AccountValue(account, tag, value, currency, modelCode)`:

```python
[
  AccountValue(account='U1234567', tag='AccountType', value='INDIVIDUAL', currency='', modelCode=''),
  AccountValue(account='U1234567', tag='CashBalance', value='12345.67', currency='USD', modelCode=''),
  AccountValue(account='U1234567', tag='CashBalance', value='8000.00', currency='HKD', modelCode=''),
  AccountValue(account='U1234567', tag='TotalCashValue', value='13360.00', currency='USD', modelCode=''),
  AccountValue(account='U1234567', tag='NetLiquidation', value='27450.20', currency='USD', modelCode=''),
  AccountValue(account='U1234567', tag='BuyingPower', value='54900.40', currency='USD', modelCode=''),
  # ... ~80 more rows
]
```

Notes:
- We currently filter to `tag in {'CashBalance', 'TotalCashValue'}`
  in `app/adapters/ibkr/adapter.py:179`. Keep that.
- Per-currency rows show up separately when an account holds
  multiple base currencies (very common for IBKR).

`ib.trades()` returns a list of `Trade(contract, order, orderStatus, fills, log)`:

```python
[
  Trade(
    contract=Stock(symbol='AAPL', currency='USD'),
    order=Order(action='BUY', totalQuantity=100, orderType='LMT', lmtPrice=180.0),
    orderStatus=OrderStatus(status='Filled', filled=100, remaining=0, avgFillPrice=180.50),
    fills=[
      Fill(
        time=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
        execution=Execution(
          acctNumber='U1234567',
          execId='exec-abc-001',
          shares=100,
          price=180.50,
          side='BOT',
          time=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
        ),
        contract=Stock(symbol='AAPL', currency='USD'),
      ),
    ],
  ),
]
```

Notes:
- `trades()` only returns trades from the current TWS session.
  For historical, use `ib.reqExecutions(filter)`.
- `Fill.execution.side` is `'BOT'` (bought) or `'SLD'` (sold).
- `Fill.execution.time` is timezone-aware datetime.

`ib.reqTickers(Stock('AAPL', 'SMART', 'USD'))` returns:

```python
[
  Ticker(
    contract=Stock('AAPL', 'SMART', 'USD'),
    time=datetime(2026, 5, 19, 0, 5, 12, tzinfo=UTC),
    bid=180.45, ask=180.50, last=180.47,
    bidSize=200, askSize=300, lastSize=100,
    volume=42000000,
    high=181.20, low=179.80, close=180.20,
    marketPrice=lambda: 180.47,  # method, not attribute
  ),
]
```

Notes:
- `ticker.marketPrice()` is a **method** that returns a float
  or NaN. We already guard against NaN at
  `app/adapters/ibkr/adapter.py:256`.

### B.5 SDK quirks

- `ib_insync` uses **asyncio internally** but most methods are
  sync. Wrap them in `asyncio.to_thread` (already done in
  `IBKRClient`).
- The gateway disconnects you after ~5 min idle. The
  `IbkrAdapter.start_keepalive` loop calls `ib.tickle()` every
  60s. **Make sure it's actually started** — currently nothing
  calls it. Either start it on backend boot (per active
  connection) or call `tickle()` at the start of every fetch.
- `reqMktData` requires a market-data subscription on your IBKR
  account. If you don't have one, `reqTickers` returns `NaN` for
  all fields. Falls back to cost basis like LongBridge.

### B.6 Sub-tasks

- [ ] Decide on gateway image; verify `docker compose up ibkr-gateway`
  starts cleanly and exposes the API port to the backend
  service via `MBP_IB_GATEWAY_HOST=ibkr-gateway`,
  `MBP_IB_GATEWAY_PORT=7497`.
- [ ] **Complete one-time interactive auth.** Open the gateway's
  web UI, log in with 2FA. The session persists in the
  `ibkr-state` named volume.
- [ ] Wire `IbkrAdapter.start_keepalive` to fire on backend
  startup for each active IBKR connection (or call `tickle()`
  inline at the start of `list_positions` / `list_balances`).
- [ ] In the Flutter Add Connection dialog, add an `accountId`
  field. Default to empty (gateway will use its own login).
- [ ] Implement env-gated integration test:
  ```
  IBKR_GATEWAY_HOST=ibkr-gateway
  IBKR_GATEWAY_PORT=7497
  IBKR_ACCOUNT_ID=U1234567
  ```
  Asserts at least one position row.
- [ ] Manual smoke: dashboard refresh with running gateway →
  positions tile updates.

### B.7 Where it appears in the app

- **Dashboard:** IBKR source tile.
- **Positions:** Stocks alongside other brokers; IBKR also
  holds forex pairs which we **filter out in v1** (only
  `secType == 'STK'`).
- **Transactions:** Trade fills from the current gateway
  session. For historical, plan item 3.

### B.8 Definition of done

- Gateway sidecar runs cleanly; auth survives a container
  restart (volume mounted).
- Dashboard tile flips to `Healthy` after first refresh.
- At least one stock position renders.
- Cash balances in multiple currencies appear correctly.

---

## C. Futu / moomoo

**Difficulty:** ★★★ (requires OpenD sidecar + per-request trade unlock)
**Estimated effort:** 3-4 hours

### C.1 Account setup

1. Sign up at https://www.futunn.com (Hong Kong) or
   https://www.moomoo.com (US / SG).
2. Apply for **OpenAPI access** in the Futu/moomoo app:
   - Tap **Quotes → Settings → Open Platform**.
   - Enable OpenAPI for your account.
   - Set a **trade unlock password** (separate from your login
     password — used to unlock the trade context per session).
3. Download **FutuOpenD** for your platform:
   https://www.futunn.com/en/download/openAPI
   (also available as a Docker image)
4. Configure OpenD with your account credentials in its config
   file. OpenD logs into Futu on your behalf and exposes a local
   gateway API on port 11111.

### C.2 Gateway sidecar

`docker-compose.yml` references
`ghcr.io/futu-sg/futunng-opend:latest`. The image accepts these
env vars:

```
FUTU_OPEND_LOGIN_ACCOUNT=12345678
FUTU_OPEND_LOGIN_PASSWORD_MD5=<md5 of your Futu password>
FUTU_OPEND_TRADE_RSA_FILE=/secrets/trade-rsa-private.key
```

To compute MD5 of your password:

```bash
echo -n "MyFutuPassword!" | md5
```

After `docker compose up futu-opend`, the OpenD gateway should
be reachable from the backend container at
`futu-opend:11111`.

### C.3 Endpoints used

```python
from futu import OpenQuoteContext, OpenSecTradeContext, TrdEnv, SubType, RET_OK

quote_ctx = OpenQuoteContext(host='futu-opend', port=11111)
trade_ctx = OpenSecTradeContext(host='futu-opend', port=11111)

# Trade unlock — required before every trade-side call in a session
ret, data = trade_ctx.unlock_trade(password='myUnlockPassword')

# Positions
ret, df = trade_ctx.position_list_query(trd_env=TrdEnv.REAL)

# Account info
ret, df = trade_ctx.accinfo_query(trd_env=TrdEnv.REAL)

# History deals
ret, df = trade_ctx.history_deal_list_query(start='2026-01-01', end='2026-05-19', trd_env=TrdEnv.REAL)

# Live quotes
quote_ctx.subscribe(['HK.00700', 'US.AAPL'], [SubType.QUOTE])
ret, df = quote_ctx.get_stock_quote(['HK.00700'])

trade_ctx.close()
quote_ctx.close()
```

### C.4 Sample raw responses

`position_list_query` returns `(ret_code, DataFrame)` where the
DataFrame columns are:

```
                position_side  code   stock_name  qty  can_sell_qty  currency  nominal_price  cost_price  market_val  pl_val  pl_ratio
0               LONG           HK.00700  騰訊控股  100  100           HKD       500.50          480.30      50050.00    2020.00   4.21
1               LONG           US.AAPL   Apple Inc   50   50            USD       180.20          175.50      9010.00     235.00    2.68
```

When you `.to_dict('records')` it:

```python
[
  {
    'position_side': 'LONG',
    'code': 'HK.00700',
    'stock_name': '騰訊控股',
    'qty': 100,
    'can_sell_qty': 100,
    'currency': 'HKD',
    'nominal_price': 500.50,
    'cost_price': 480.30,
    'market_val': 50050.00,
    'pl_val': 2020.00,
    'pl_ratio': 4.21,
  },
  {
    'position_side': 'LONG',
    'code': 'US.AAPL',
    'stock_name': 'Apple Inc',
    'qty': 50,
    'can_sell_qty': 50,
    'currency': 'USD',
    'nominal_price': 180.20,
    'cost_price': 175.50,
    'market_val': 9010.00,
    'pl_val': 235.00,
    'pl_ratio': 2.68,
  },
]
```

Notes:
- `code` is prefixed with market (`HK.`, `US.`, `SG.`). Strip
  the prefix for display, keep it for re-querying.
- `nominal_price` = current/last price.
- `pl_ratio` is in percent already (4.21 = 4.21%).
- The mapper at `app/adapters/futu/adapter.py` already handles
  these columns. Verify against real responses.

`accinfo_query` returns:

```python
[
  {
    'currency': 'HKD',
    'cash': 50000.00,
    'total_assets': 51500.00,
    'available_funds': 50000.00,
    'frozen_cash': 0.00,
    'market_val': 1500.00,
    'realized_pl': 0.00,
    'unrealized_pl': 150.00,
  }
]
```

Notes:
- Returns one row **per currency**, not per asset.
- For mapping to `CashBalance`, use `available_funds` (not `cash`
  which includes locked).

`history_deal_list_query(start, end, trd_env)`:

```python
[
  {
    'trd_side': 'BUY',
    'order_id': '20260501000001',
    'deal_id': '20260501999999',
    'code': 'HK.00700',
    'stock_name': '騰訊控股',
    'qty': 100,
    'price': 480.30,
    'create_time': '2026-05-01 10:15:20',
    'counter_broker_id': '',
    'counter_broker_name': '',
  },
]
```

Notes:
- `trd_side` is `'BUY'` or `'SELL'`.
- `create_time` is a string in HK time. Parse and convert to UTC.

`get_stock_quote` (live):

```python
[
  {
    'code': 'HK.00700',
    'last_price': 500.50,
    'open_price': 498.00,
    'high_price': 502.00,
    'low_price': 497.50,
    'prev_close_price': 488.00,
    'volume': 12345678,
    'turnover': 6172839000.50,
    'update_time': '2026-05-19 16:00:00',
    'sec_status': 'NORMAL',
  },
]
```

### C.5 SDK quirks

- **CORRECTION (2026-09-06): `trade_ctx` queries do NOT require
  `unlock_trade`.** The claim above was wrong and was never tested; it
  is the sole reason a trade password existed in `settings.py` and
  `.env` at all.

  Verified three ways:
  1. Futu's own docs scope unlock to *"Place Order or Modify or Cancel
     Orders"* — https://openapi.futunn.com/futu-api-doc/en/trade/unlock.html
  2. The SDK's `position_list_query` / `accinfo_query` /
     `history_deal_list_query` contain no unlock gate. `_ctx_unlock` is
     read in exactly one place — to re-unlock after a socket reconnect.
  3. Empirically, against real OpenD 10.6.6608 with a session that
     never unlocked: `accinfo_query` OK, `position_list_query` OK
     (2 rows), `history_deal_list_query` OK.

  `unlock_trade` has therefore been removed from the backend entirely,
  along with `MBP_FUTU_TRADE_UNLOCK_PASSWORD`. The session this system
  uses **cannot place, modify or cancel an order** — a structural
  guarantee rather than a policy (spec §4.3 rule 2), enforced by
  `backend/tests/test_no_trade_unlock.py`.
- `position_list_query` only returns positions for the
  `trd_env` you specify (`TrdEnv.REAL` or `TrdEnv.SIMULATE`).
  Default to REAL.
- DataFrames returned by the SDK can be empty
  (`df.shape == (0, N)`). `_rows_from_payload` already
  handles that.
- The futu SDK initialises some files in `$HOME` on import. The
  Dockerfile provides `HOME=/home/mbp` and chowns the dir.

### C.6 Sub-tasks

- [ ] Set up OpenD with your Futu credentials. Verify the
  gateway runs in the container and is reachable at
  `futu-opend:11111`.
- [ ] In the Flutter Add Connection dialog, ensure the dialog
  exposes a `Trade Unlock Password` field for Futu.
- [ ] Verify `FutuAdapter._unlocked` lifecycle: unlock → call →
  lock works without leaking the password across requests.
- [ ] In `FutuOpenDClient`, map DataFrame rows to dicts via
  `.to_dict('records')` (already done in `_rows_from_payload`).
- [ ] Implement env-gated integration test:
  ```
  FUTU_OPEND_HOST=futu-opend
  FUTU_OPEND_PORT=11111
  FUTU_TRADE_PASSWORD=...
  ```
  Asserts at least one position row.
- [ ] Manual smoke: dashboard refresh → Futu source tile flips
  to Healthy with HK or US positions.

### C.7 Where it appears in the app

- **Dashboard:** Futu source tile.
- **Positions:** HK + US positions with cost basis.
- **Transactions:** Trade history within the date window.
- **Allocation donut:** HKD/USD/SGD balances correctly
  attributed.

### C.8 Definition of done

- OpenD sidecar runs; logs in to Futu on startup.
- Dashboard tile shows `Healthy` after first refresh.
- HK or US position renders with `cost_price` and `nominal_price`.
- Trade unlock works per request without storing the password.

---

## Cross-broker checklist before declaring item 2 done

- [ ] Each broker's source tile in the Dashboard shows the
  `Healthy` badge and a non-stale `lastSyncAt`.
- [ ] The Allocation donut on the dashboard now has segments
  from at least two brokers in different currencies.
- [ ] Positions screen lists rows from every connected broker
  with cost basis + last price + P&L%.
- [ ] Sign-out / sign-in cycle preserves all connections under
  the user's email-account uid (verified in
  `users/{uid}/connections/` in Firestore console).
- [ ] No verbose INFO logs from the broker clients in the
  default log level (depends on item 1 landing first).
- [ ] All standard backend gates green
  (`pytest --cov-fail-under=80`, `ruff`, `mypy --strict`).
- [ ] All standard Flutter gates green
  (`flutter analyze`, `flutter test`, coverage ≥ 80%).
