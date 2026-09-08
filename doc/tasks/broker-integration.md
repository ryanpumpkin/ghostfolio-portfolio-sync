# broker-integration

Wire the four broker adapters end-to-end so the dashboard shows real
holdings instead of "unknown" tiles.

**Current state (2026-05-18):** Connection metadata + AES-GCM encrypted
credentials flow through to Firestore, but the backend never actually
calls a broker. Adapters exist as Protocol wrappers with the SDK client
dependency-injected — nothing wires real SDK clients in. This module
closes that gap.

**Scope per broker:** LongBridge, IBKR, Futu, Binance. Each is
independent and can be worked in parallel.

## Architectural decisions (fixed — do not redesign)

1. **E2E mode flow (default):** Client decrypts its own credential blob
   on demand, calls `E2eCrypto.wrapForBackend(plaintextCreds, key)` to
   produce a short-lived (2 min) wrapped token, sends it in the
   `X-MBP-Creds` header on each REST call. Backend decrypts via
   `E2eCrypto.unwrapFromBackend(...)`, instantiates the SDK client for
   that request only, drops it after. Never persisted server-side.
2. **Server-key mode (opt-in):** Backend keeps a KMS-decrypted blob in
   the credential vault (already implemented in `backend-vault`).
   Adapter pulls from vault on each call.
3. **Adapter registry:** Per-request, not per-process. The backend
   resolves `(userId, connectionId) → adapter instance` on each REST
   call; lifetime = one request.
4. **Status update:** After every adapter call, write
   `users/{uid}/connections/{cid}/status` and `lastSyncAt` to Firestore.
   Status enum is already defined: `unknown | ok | error | disabled`.
5. **Refresh trigger:** dashboard pull-to-refresh and 5-minute foreground
   poll (already in `flutter-state`). No new "Test Connection" button —
   refresh is the validation per proposal §6.

## Subtasks

### Backend — shared

- [x] Add `X-MBP-Creds` header parsing middleware that extracts the
  wrapped E2E token (base64 of the JSON envelope produced by
  `E2eCrypto.wrapForBackend`) into the request context. Skip if header
  absent (server-key mode falls through to vault).
- [x] Implement `unwrapFromBackend` in Python that matches the Dart
  implementation byte-for-byte (same AES-GCM nonce/MAC layout, same
  JSON envelope `{v, expiresAt, ct: {nonce, cipherBytes, mac}}`,
  reject expired). Unit tests round-trip against fixtures generated
  by the Dart code.
- [x] Add `app/services/adapter_factory.py` that resolves
  `(connection_kind, plaintext_creds) → SourceAdapter` for the request.
- [x] After every successful adapter call in the aggregator, publish a
  `(connectionId, status, lastSyncAt, errorMessage?)` event. Listener
  writes to Firestore via the existing `firestore` admin client.
- [x] Update `/v1/portfolio`, `/v1/positions`, `/v1/transactions`,
  `/v1/balances` to accept the wrapped-creds header and pass plaintext
  creds into the aggregator → adapter_factory → adapter.

### Backend — LongBridge

- [x] Add `longbridge` to `pyproject.toml` (use the latest stable SDK
  version on PyPI). Pin to `~=` minor.
- [x] Implement `LongbridgeClient` (the Protocol's expected wrapper) in
  `app/adapters/longbridge/client.py`: takes `(appKey, appSecret,
  accessToken)`, instantiates `longbridge.Config` + `QuoteContext` +
  `TradeContext`, exposes async methods `list_positions`,
  `list_balances`, `list_transactions`, `stream_quotes`.
- [x] Map LongBridge response shapes to the domain models in
  `app/models/domain.py`. Use Decimal where the SDK returns Decimal;
  convert to float only at the API boundary.
- [x] Unit tests with a fake `LongbridgeClient` covering happy path,
  rate-limit retry (use the existing `RetryPolicy`), and credential
  errors → `PermanentError`.
- [x] Integration test (skippable in CI) that, if env vars
  `LB_APP_KEY` / `LB_APP_SECRET` / `LB_ACCESS_TOKEN` are present, hits
  the real LongBridge API and asserts at least one balance row.

### Backend — Binance

- [x] Add `python-binance` to `pyproject.toml`.
- [x] Implement `BinanceClient` wrapper supporting both `binance.com`
  and `binance.us` (the existing adapter already takes `BinanceHost`).
  Reject keys that have `canTrade=true` or `canWithdraw=true` on init
  (defence in depth — Binance also enforces this).
- [x] Map Binance `account()`, `myTrades()`, `klines()` responses to
  domain models. Spot balances only in v1 (no futures, no margin).
- [x] Same test shape as LongBridge.

### Backend — IBKR

- [x] Add `ib_insync` to `pyproject.toml`.
- [x] `IBKRClient` connects via the IBGW sidecar (env vars already in
  `.env.example`: `MBP_IB_GATEWAY_HOST`, `MBP_IB_GATEWAY_PORT`).
- [x] Map `accountSummary()`, `positions()`, `trades()` to domain.
- [x] Same test shape.

### Backend — Futu

- [x] Add `futu-api` to `pyproject.toml`.
- [x] `FutuClient` connects to OpenD sidecar; auto-unlocks trade
  context per request using the per-call password from the request
  context (never persisted).
- [x] Map `position_list_query()`, `accinfo_query()`,
  `history_deal_list_query()` to domain.
- [x] Same test shape.

### Flutter — wire the credentials header

- [x] Extend `BackendClient` to optionally attach `X-MBP-Creds` on
  portfolio/positions/transactions/balances calls when the active
  connection is in E2E mode.
- [x] Add a `WrappedCredentialsBuilder` service that, given a
  `connectionId`, reads the encrypted blob from Firestore + the
  current `credentialKeyProvider`, decrypts to plaintext creds,
  wraps via `E2eCrypto.wrapForBackend`, returns the base64 envelope.
- [x] `portfolioRepositoryImpl.getSnapshot()` enumerates the user's
  active connections, requests one wrapped token per connection,
  attaches a `Map<connectionId, wrappedToken>` header (or one call
  per connection — pick whichever the aggregator endpoint accepts).
- [x] Surface per-connection errors via the existing `SourceHealth`
  field in `PortfolioSnapshot`.

### UI polish

- [x] On the Connections screen, show `lastSyncAt` next to each
  connection (e.g. "synced 3 min ago").
- [x] When a connection's status is `error`, show the error message
  in a tooltip or expandable panel.

## Quality gates

- `pytest --cov=backend --cov-fail-under=80` (existing project gate)
- `ruff check backend` clean
- `mypy --strict backend` clean
- `flutter analyze` clean
- `flutter test --coverage` ≥ 80% on hand-written code
- Integration test: if `LB_APP_KEY` etc. env vars are present, the test
  hits the real LongBridge sandbox and asserts a balance row exists.
  Skipped otherwise.
- Manual smoke: with at least one broker key in `.env`, run
  `docker compose up backend`, open the Flutter app, add a connection
  with real creds, pull-to-refresh the dashboard. Status must flip
  from `unknown` to `ok` and at least one holding row appears.

## Out of scope

- Order placement / trading — proposal §12 explicitly out of scope.
- Margin and futures on Binance — spot only in v1.
- Futu's HK F1 derivatives / options — equities + ETFs only.
- IBKR's exotic asset classes (warrants, structured products).
- Real-time WebSocket quote multiplexing — REST polling is enough for
  v1; quote streaming is a separate module if needed later.
