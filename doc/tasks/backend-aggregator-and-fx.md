# backend-aggregator-and-fx

Aggregation service that unifies multi-source results, plus the FX rate service used for base-currency conversion.

## Subtasks

### Aggregator (`app/services/aggregator.py`)

- [x] `get_snapshot(user_id)`: load connections, fan-out adapter calls in parallel with `asyncio.gather(return_exceptions=True)`
- [x] Normalize results into `PortfolioSnapshot` (per-source list + totals)
- [x] Attach FX rates and compute base-currency totals
- [x] Per-source health surfacing in the response (no source kills the whole call)
- [x] In-memory cache with TTL keyed by `(user_id, source_id)` to absorb client polling

### Quote multiplexer (`app/services/quote_hub.py`)

- [x] Maintain upstream subscriptions per source; multiplex to multiple client WS connections
- [x] Reference-count symbol subscriptions; unsubscribe upstream when no clients remain
- [x] Heartbeat / reconnect logic for both upstream and client sockets

### FX service (`app/services/fx.py`)

- [x] Pluggable provider interface (exchangerate.host default; OpenExchangeRates as alternative)
- [x] In-process cache + Firestore cache (`fx_rates/{base}_{quote}` with TTL)
- [x] Currency triangulation when a direct pair is missing (via USD)
- [x] `get_rate(base, quote)` and `get_rates_for([(b,q),...])` batch API

### API routes

- [x] `GET /v1/portfolio` → snapshot
- [x] `GET /v1/positions`, `GET /v1/transactions`, `GET /v1/balances` with filters
- [x] `WS  /v1/quotes/stream` with subscribe/unsubscribe frames
- [x] `GET /v1/fx?pairs=...`

### Tests

- [x] Aggregator partial-failure test (one adapter raises)
- [x] FX triangulation test
- [x] Quote hub ref-count test
