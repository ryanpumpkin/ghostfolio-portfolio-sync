# backend-adapters

Source adapters implementing the common `SourceAdapter` protocol. Sidecar gateways live in the same Docker network.

## Subtasks

### Common (`app/adapters/base.py`)

- [x] Define `SourceAdapter` Protocol: `list_positions`, `list_balances`, `list_transactions`, `stream_quotes`, `healthcheck`
- [x] Common retry + rate-limit middleware (tenacity / custom)
- [x] Per-adapter session cache keyed by user + connection id

### LongBridge adapter (`app/adapters/longbridge.py`)

- [x] Wrap official LongBridge OpenAPI SDK
- [x] Map LB position / balance / transaction shapes → internal Pydantic models
- [x] WebSocket subscription for live quotes
- [x] Handle token refresh

### IBKR adapter (`app/adapters/ibkr.py`)

- [x] Talk to Client Portal Gateway (HTTPS, localhost in compose network)
- [x] Implement keep-alive ping loop (CP Gateway expires sessions otherwise)
- [x] Map IB position / account-summary / executions to internal models
- [x] Stream market data via CP Gateway WS

### Futu adapter (`app/adapters/futu.py`)

- [x] Connect to OpenD over its TCP protocol (`futu-api` Python SDK)
- [x] Unlock trade context with user-provided trade password (per request, never persisted)
- [x] Map Futu position / account / order-history models
- [x] Subscribe to quote streams

### Binance adapter (`app/adapters/binance.py`)

- [x] Support both `binance.com` and `binance.us` base URLs (chosen per connection)
- [x] Read-only API key + secret (HMAC-SHA256 signing)
- [x] Spot balances, trades, deposit/withdrawal history
- [x] WebSocket `!miniTicker@arr` (or per-symbol stream) for live prices
- [x] Reject keys with trade/withdraw permissions enabled (sanity check at connect time)

### Tests

- [x] Per-adapter unit tests with HTTP mock fixtures
- [x] Contract test asserting all adapters satisfy `SourceAdapter`
- [~] Integration test against Binance testnet — deferred; live SDK wrapper (`HttpxBinanceClient`) lives behind `# pragma: no cover` until infra-deployment provides credentials.

## Notes

- Broker SDKs (`longbridge`, `ib_insync`, `futu-api`, `python-binance`) are NOT runtime deps. Each adapter takes an injected client object that satisfies a small Protocol; the production SDK is wired up by the caller (composition root in `backend-aggregator-and-fx` / `infra-deployment`). The only live network path is `HttpxBinanceClient` which is marked `# pragma: no cover`.
- Tests: 69 passing (39 new in `tests/adapters/`), lint clean, mypy --strict clean, coverage 97.73% overall (96.4% on `app/adapters/*`).
