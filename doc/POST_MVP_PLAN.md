# Post-MVP Plan

The original `doc/prompt.md` orchestrator built the 14 base
modules, and `doc/broker-integration-prompt.md` wired the
broker SDKs through E2E credentials end-to-end. The dashboard
now shows real LongBridge positions with live last-close prices.

This document is the **next-iteration backlog**, sized for an
autonomous agent to pick up. Each item below is self-contained:
clear scope, file paths, subtasks, gates, out-of-scope.

Items are **independent** unless noted. They can run in any
order; pair them with the orchestrator pattern in
`doc/broker-integration-prompt.md` if you want a fresh agent to
execute multiple in parallel.

---

## Item 1 — Strip diagnostic logging

**Slice id:** `cleanup-diagnostic-logging`

**Why:** During the broker-integration debugging session we
added several `INFO`-level structured log lines that dumped
raw SDK responses (`stock_positions raw response repr=...`),
per-channel breakdowns, the user_id + uid + wrapped_keys for
every refresh, and the price-by-symbol map after each
quote-enrichment. They served their purpose. In normal
operation they're noise that triples the log volume and would
print credentials-adjacent data if response shapes ever change.

**Scope (do exactly this, nothing else):**

- Demote or remove the following INFO log calls. Keep them as
  `DEBUG` so they still fire under `MBP_LOG_LEVEL=DEBUG` but
  don't spam production.

| File | Logger | Line context | Action |
|---|---|---|---|
| `backend/app/services/aggregator.py` | `mbp.aggregator` | `get_snapshot user_id=...` | demote INFO → DEBUG |
| `backend/app/services/vault.py` | `mbp.vault` | `list_for_user uid=... firestore_docs=N` | demote INFO → DEBUG |
| `backend/app/services/vault.py` | `mbp.vault` | `Firestore client is None — falling back…` | keep WARNING (it's an operational signal) |
| `backend/app/services/vault.py` | `mbp.vault` | `list_for_user uid=... firestore_read_failed:` | keep ERROR |
| `backend/app/adapters/longbridge/client.py` | `mbp.longbridge.client` | `stock_positions raw response type=...` | **remove entirely** (raw response could leak data) |
| `backend/app/adapters/longbridge/client.py` | `mbp.longbridge.client` | `stock_positions channel_count=N` | demote INFO → DEBUG |
| `backend/app/adapters/longbridge/client.py` | `mbp.longbridge.client` | `stock_positions channel[i] name=...` | demote INFO → DEBUG |
| `backend/app/adapters/longbridge/client.py` | `mbp.longbridge.client` | `quote-enrich: symbols=...` | demote INFO → DEBUG |
| `backend/app/adapters/longbridge/client.py` | `mbp.longbridge.client` | `quote-enrich: prices_resolved=...` | demote INFO → DEBUG |
| `backend/app/adapters/longbridge/client.py` | `mbp.longbridge.client` | `quote-enrich failed:` | keep WARNING |

- For each demoted log line, also wrap it in `log.isEnabledFor(logging.DEBUG)`
  before constructing expensive `repr()` strings so the cost
  goes away entirely at INFO/WARNING levels.

- Update `backend/app/core/logging.py` if needed so the
  hierarchical loggers `mbp.aggregator`, `mbp.vault`,
  `mbp.longbridge.client` inherit from the root config.

- Confirm `quote-enrich-failed`, `firestore_read_failed`, and
  the access-log JSON line still produce visible output at the
  default `INFO` level.

**Out of scope:**

- Don't change the access-log middleware (`AccessLogMiddleware`).
- Don't touch Flutter `AppLogger`.
- Don't refactor the logger names themselves.

**Gates:**

- `cd backend && .venv/bin/pytest --cov=app --cov-fail-under=80 -q` clean.
- `ruff check .` clean.
- `mypy --strict app` clean.
- Manual: `docker compose up backend -d` + one
  `/v1/portfolio?base_currency=USD` request →
  `docker compose logs backend` shows the access-log line and
  nothing else for that request at INFO level.

**Estimated effort:** 30 min.

---

## Item 2 — Wire the other three brokers end-to-end

**Slice id:** `broker-integration-binance`,
`broker-integration-ibkr`, `broker-integration-futu`

LongBridge is fully wired. The other three brokers have working
SDK clients and adapters, but no one has driven a real
end-to-end refresh through them. Each broker is independent —
spawn them in parallel.

### 2A — Binance (easiest; no sidecar)

**Why first:** No sidecar required, no special infrastructure,
just an API key. Confirms the multi-broker shape works.

**Subtasks:**

- [!] User creates a read-only API key at binance.com or (blocked: real user key required)
  binance.us:
  - Tick **only** "Enable Reading" — leave Trade, Withdraw,
    Margin, Futures **off**. Binance enforces this server-side.
  - Restrict by IP if possible (paranoid mode).
- [x] In the Flutter Add Connection dialog, pick `binance`,
  enter `apiKey`, `apiSecret`, and `region` (`com` or `us`).
- [!] Refresh the dashboard. Backend should: (blocked: real broker creds required)
  1. Build the wrapped credentials.
  2. Instantiate `BinanceAdapter` via the factory.
  3. Call `client.account()` and pull spot balances.
  4. Call `client.myTrades()` for recent trades (skip per-symbol,
     `myTrades` only returns trades for a given symbol — see
     Open Items below).
- [!] **Likely fix needed:** the existing `HttpxBinanceClient` (blocked: requires real-key verification)
  uses HMAC-SHA256 signing per the Binance docs. Verify against
  `https://api.binance.com/api/v3/account` with a real key. If
  the signed query string format is wrong, fix per
  https://binance-docs.github.io/apidocs/spot/en/#endpoint-security-type
- [x] Map response to `Position` + `CashBalance`. Binance returns
  asset balances, not equities — represent crypto holdings as
  positions with `currency` = the quote-asset symbol (e.g.
  `USDT`) and `symbol` = the base asset (e.g. `BTC`).
- [x] Spot-only. Skip futures + margin + options.

**Gates:**

- All standard backend gates (pytest / ruff / mypy / coverage ≥80%).
- Manual smoke: with a real read-only key in Firestore, dashboard
  refresh shows status `ok` and at least one crypto balance row.

**Estimated effort:** 1-2 hours.

### 2B — Interactive Brokers (IBKR)

**Why this is more involved:** IBKR requires a running **Client
Portal Gateway** sidecar (or TWS/IBGW) on the same host. The
gateway needs interactive login on first run.

**Subtasks:**

- [!] Decide between Client Portal Web API gateway (blocked: deployment/operator choice)
  (recommended for headless server) vs IB Gateway / TWS
  (graphical, interactive login).
- [!] Start the sidecar via `docker compose up ibkr-gateway`. (blocked: private image / operator setup)
  Verify the gateway is reachable from the backend container at
  `ibkr-gateway:5000`. The current compose file references
  `ghcr.io/unusualwhale/ibkr-cpapi:latest` — confirm that image
  exists or substitute one.
- [!] **Authenticate once.** Most CP-Gateway images require a (blocked: interactive 2FA in operator session)
  one-time interactive auth at `https://<host>:5000/`. After
  successful auth the session lives in the gateway's state
  volume (`ibkr-state`) until the periodic re-auth expires
  (typically 24h).
- [x] Wire the keep-alive tickle: `IbkrAdapter.start_keepalive`
  exists but isn't called anywhere. Either:
  - Start it on backend boot (one task per active IBKR
    connection), or
  - Call `client.tickle()` opportunistically on each
    request.
- [!] In the Flutter Add Connection dialog, pick `ibkr`, enter (blocked: real account required)
  the optional `accountId` (e.g. `U12345`). The gateway handles
  the actual login.
- [!] Refresh the dashboard. Backend should: (blocked: live gateway + account required)
  1. Build the wrapped credentials (mostly metadata since
     the real login is at the gateway).
  2. Instantiate `IbkrAdapter` via the factory.
  3. Call `client.fetch_positions()`, `client.fetch_account_summary()`.
  4. Map to domain.
- [x] Add an integration test gated on env vars `IBKR_GATEWAY_HOST`
  / `IBKR_GATEWAY_PORT` that, when set, hits a real running
  gateway and asserts at least one position row.

**Gates:**

- Standard backend gates.
- Manual smoke: with a real IBKR account + running gateway,
  dashboard refresh shows at least one IBKR position.

**Estimated effort:** 3-4 hours, mostly gateway plumbing.

### 2C — Futu / moomoo

**Why this is more involved:** Futu requires a running **OpenD**
gateway sidecar. Trade context requires an unlock password per
session that the user must supply.

**Subtasks:**

- [!] Start the sidecar via `docker compose up futu-opend`. (blocked: sidecar image/account setup required)
  Verify reachable at `futu-opend:11111`. Current compose file
  references `ghcr.io/futu-sg/futunng-opend:latest` — confirm or
  substitute.
- [!] OpenD handles the account login at startup using env vars (blocked: real login secrets required)
  (`FUTU_OPEND_LOGIN_ACCOUNT`, `FUTU_OPEND_LOGIN_PASSWORD_MD5`).
  Once logged in, OpenD exposes a localhost API.
- [x] In the Flutter Add Connection dialog, pick `futu`. Add a
  **trade unlock password** field — this is captured per request
  and never persisted. The credentials dict goes through the
  same E2E wrap as everything else; the backend's
  `FutuAdapter._unlocked` context manager unlocks the trade
  context, runs the query, then re-locks.
- [x] Currently the existing `FutuAdapter` already has the
  unlock pattern wired (`get_request_trade_password`). Verify
  the `unlock_password_provider` callback resolves correctly
  from the per-request credential context.
- [!] Refresh the dashboard. Backend should: (blocked: live OpenD + account required)
  1. Build wrapped credentials including the trade password.
  2. Instantiate `FutuAdapter`.
  3. Call `client.fetch_positions()`, `client.fetch_accounts()`.
  4. Map to domain.

**Out of scope:**

- HK F1 derivatives / options.
- Margin / futures contexts.

**Gates:**

- Standard backend gates.
- Manual smoke: with a real Futu account + running OpenD,
  dashboard refresh shows at least one Futu position.

**Estimated effort:** 3-4 hours.

---

## Item 3 — Historical transaction sync

**Slice id:** `transactions-history`

**Why:** Today only "today's" executions are returned. The
Transactions screen is wired but empty. Each broker has a
historical executions endpoint we aren't using:

| Broker | Method |
|---|---|
| LongBridge | `TradeContext.history_executions(symbol=None, start_at, end_at)` |
| Binance | `client.myTrades(symbol, startTime, endTime)` (per-symbol) |
| IBKR | `client.executions(filter)` or `client.trades()` |
| Futu | `trade_ctx.history_deal_list_query(start, end)` |

**Subtasks per broker:**

- [x] In each `<broker>/client.py`, add a `list_transactions`
  flow that accepts `since: datetime | None` and `limit: int |
  None`, and calls the broker's history endpoint with a sensible
  default window (last 90 days when `since` is None).
- [x] In each `<broker>/adapter.py`, ensure `list_transactions`
  passes `since` + `limit` through to the client.
- [x] In Flutter, the transactions screen already calls
  `transactionsRepository.list({sourceId, range})`. Verify the
  range default is "last 30 days" — extend if needed.
- [x] Add a paging mechanism if any broker enforces a max
  result count (Binance is 1000/call, LongBridge unknown).
- [x] Cache hits to Drift so re-opens are instant.
- [x] Update the `transactionsCache` schema only if needed —
  the existing columns cover what brokers return.

**Out of scope:**

- Server-side cron polling. The transactions list is on-demand.
- Cross-broker dedup. Each broker's transactions are independent.
- Tax-lot accounting. The proposal explicitly excludes this.

**Gates:**

- All standard gates.
- Per-broker integration test (env-gated) that returns at
  least one transaction when historical creds are present.

**Estimated effort:** 1 hour per broker.

---

## Item 4 — Live quote streaming via WebSocket

**Slice id:** `live-quote-streaming`

**Why:** The proposal §6 calls for real-time quotes via
WebSocket. The infrastructure is scaffolded (`QuotesStream` on
Flutter, `stream_quotes` on every `SourceAdapter`,
`/v1/quotes/stream` endpoint declared) but nothing actually
connects.

**Subtasks:**

- [x] **Backend `/v1/quotes/stream` WebSocket endpoint.** Accepts
  a Firebase ID token + wrapped credentials in the upgrade
  request. On connect:
  - Authenticate the token.
  - Subscribe each connection's adapter's `stream_quotes(symbols)`.
  - Multiplex all source streams into one outbound stream of
    `{source, symbol, price, currency, timestamp}` messages.
  - On `add_symbol` / `remove_symbol` client messages, mutate
    the active subscription set.
- [x] **LongBridge `stream_quotes`** uses
  `QuoteContext.subscribe(symbols, SubType.Quote)` and yields
  pushed quotes via the SDK's callback. Currently we poll
  `quote()` every second; switch to push-based subscription.
- [x] **Binance `stream_quotes`** uses Binance's `wss://stream`
  endpoint with the `<symbol>@trade` topic.
- [x] **IBKR `stream_quotes`** uses `reqMktData` via ib_insync
  (already drafted). The keep-alive must continue to fire for
  the gateway session.
- [x] **Futu `stream_quotes`** uses OpenD's
  `quote_ctx.subscribe(symbols, [SubType.QUOTE])` and the
  push handler.
- [x] **Flutter `QuotesRepositoryImpl`** already exists. Verify
  it reconnects with exp backoff after a server drop (the
  existing `QuotesStream` has this — sanity check it).
- [x] **Live quote UI binding.** In the Positions screen, swap
  `position.currentPrice` for a `ref.watch(quotesProvider(symbol))`
  so the row updates as quotes arrive.

**Out of scope:**

- Order book depth.
- Trade history streams.
- Custom indicators.

**Gates:**

- Standard backend gates.
- Manual smoke: open the Positions screen during market hours
  with at least one broker connection; price cells should tick
  in real time.

**Estimated effort:** 6-8 hours.

---

## Item 5 — Final report + handoff

**Slice id:** `final-report`

**Why:** Wrap up.

**Subtasks:**

- [x] Update `doc/tasks/FINAL_REPORT.md` to reflect the actual
  shipping state, including all broker-integration follow-ups
  that landed.
- [x] Add a section listing every commit on `main` since the
  original orchestrator finished, grouped by theme (auth,
  encryption, broker wiring, FX, etc.).
- [!] Capture a screenshot of the working dashboard with real (blocked: live account data + interactive UI capture required)
  data into `doc/screenshots/` and reference it from the README.
- [x] Update the README at the repo root with:
  - The one-paragraph "what this is".
  - A pointer to `doc/RUNBOOK.md` for setup.
  - A pointer to `doc/ARCHITECTURE_NOTES.md` for the decisions
    that aren't in the original spec.
- [x] Optional: tag the commit, e.g.
  `git tag -a v0.1-personal-mvp -m "Real LongBridge data
  flowing end-to-end"`.

**Gates:**

- None functional. Just doc + housekeeping.

**Estimated effort:** 30-60 min.

---

## Orchestrator briefing template

If you fire this through an orchestrator instead of doing items
by hand, use this template per slice (adapted from
`doc/broker-integration-prompt.md`):

```
You are implementing exactly one slice of the Multi-Broker
Portfolio Tracker post-MVP plan: <SLICE_ID>.

CONTEXT YOU MUST READ BEFORE WRITING CODE:
- doc/proposal.md
- doc/detailed-design.md
- doc/RUNBOOK.md
- doc/ARCHITECTURE_NOTES.md
- doc/POST_MVP_PLAN.md (find your slice; it is the authoritative checklist)

SCOPE:
- Implement every checkbox under your slice.
- Stay inside the file paths the slice specifies.
- Match the architectural decisions in ARCHITECTURE_NOTES.md.

QUALITY GATES (all must pass before commit):
- Backend: pytest, ruff, mypy --strict, coverage ≥ 80%.
- Flutter: flutter analyze, flutter test, coverage ≥ 80% on
  hand-written code.
- For integration-test slices: env-gated tests that hit real
  external services and skip without credentials.

COMMIT POLICY:
- One commit per slice on `main`.
- Message format:
    feat(post-mvp/<SLICE>): <one-line>

    Tests: <n> passing · Coverage: <pct>% · Lint: ok · Type-check: ok.
- Do not skip git hooks. Do not amend. Do not force-push.
- Do not push to the remote (the human pushes).

HARD CONSTRAINTS:
- Never log, print, or commit broker credentials in plaintext.
- backend/.secrets/ is read-only opaque storage; go through
  vault.py and kms/*.
- Don't modify doc/proposal.md or doc/detailed-design.md.
- Don't invent new slices.

Report back when done.
```

Spin up agents in parallel for independent slices (e.g. 2A, 2B,
2C, 3 all at once). Item 1 should land first to clean up the
logs before others extend them.
