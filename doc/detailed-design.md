# Multi-Broker Portfolio Tracker — High-Level Design

> Source of truth for requirements: [proposal.md](./proposal.md). This document decomposes the system into modules and describes how they interact. Detailed design (data schemas, API contracts, sequence diagrams) is deferred to a later document.

## 0. Technology Decisions

The proposal left several stack choices open. The following decisions are made here; they can be revisited during detailed design.

| Concern | Decision | Rationale |
|---|---|---|
| Flutter state management | **Riverpod** | Compile-safe, scales well, good testability |
| Local on-device storage | **Drift (SQLite)** | Typed SQL, cross-platform (incl. Web via sql.js), good fit for relational holdings/transactions |
| Local secure storage | **flutter_secure_storage** | Wraps iOS Keychain / Android Keystore / Web `SubtleCrypto` |
| Backend language / framework | **Python + FastAPI** | Best official SDK coverage across LongBridge, IBKR (ib_insync / Client Portal), and Futu |
| Backend packaging | **Docker** | Per user requirement — single image, deploy anywhere |
| Backend ↔ client transport | **REST + WebSocket** | REST for snapshots and writes; WebSocket for streaming quotes |
| Auth / sync / push | **Firebase** (Auth, Firestore, Cloud Messaging, Crashlytics) | Already chosen in proposal |
| Credential storage in cloud | **Hybrid E2E** (per user requirement) | Default: client-encrypted blob in Firestore. Opt-in: server-side KMS-encrypted key for background tasks |

## 1. System Context

```
                  ┌──────────────────────────────────────────────┐
                  │             End User Devices                 │
                  │   iOS  •  Android  •  Web (browser)          │
                  │   Flutter App (this project)                 │
                  └──┬──────────────┬──────────────────┬─────────┘
                     │              │                  │
       Firebase SDKs │              │ REST + WS        │ FCM push
                     ▼              ▼                  ▲
        ┌────────────────────┐  ┌──────────────────┐   │
        │  Firebase          │  │  Backend Proxy   │───┘
        │  • Auth            │  │  (FastAPI,       │
        │  • Firestore       │  │   Docker)        │
        │  • Cloud Messaging │  │  • Broker adapt. │
        │  • Crashlytics     │  │  • Crypto adapt. │
        └─────────▲──────────┘  │  • Aggregator    │
                  │             │  • Alert worker  │
                  │ Admin SDK   │  • FX cache      │
                  └─────────────┤  • Credential    │
                                │    vault         │
                                └───┬────────┬─────┘
                                    │        │
            Broker / Exchange APIs  ▼        ▼  FX rate provider
        LongBridge • IBKR • Futu/moomoo • Binance
        (some require local gateway processes co-located
         with the backend container)
```

## 2. Top-Level Module Decomposition

The system has **three deployable units** and one set of **external dependencies**:

1. **Flutter Client App** (mobile + web)
2. **Backend Proxy Service** (Dockerized FastAPI)
3. **Firebase Project** (managed)
4. **External APIs** (brokers, Binance, FX provider)

---

## 3. Flutter Client App — Modules

Organized in a layered architecture (UI → State → Domain → Data). Each layer depends only on the layer below it.

### 3.1 Presentation Layer (`lib/presentation/`)

| Module | Responsibility |
|---|---|
| `screens/auth` | Sign-in / sign-up, biometric/PIN lock |
| `screens/onboarding` | First-run setup, broker connection wizards |
| `screens/dashboard` | Aggregated portfolio view, total P&L, per-broker tiles |
| `screens/positions` | Per-position list with sort/filter |
| `screens/charts` | Time-series and asset-allocation views |
| `screens/transactions` | Transaction history list and filters |
| `screens/connections` | Manage connected brokers / Binance / manual holdings |
| `screens/alerts` | Create, list, edit price/P&L alerts |
| `screens/settings` | Currency mode, theme, language, export, lock |
| `widgets/` | Reusable widgets (charts, position row, currency toggle, etc.) |

### 3.2 State Management Layer (`lib/state/`)

Riverpod providers grouped by feature:

- `authProvider` — current user, sign-in state.
- `connectionsProvider` — list of connected sources and their health.
- `portfolioProvider` — aggregated holdings, cash, P&L.
- `quotesProvider` — live price stream subscriptions.
- `transactionsProvider` — paginated transaction list.
- `alertsProvider` — alert definitions and trigger history.
- `settingsProvider` — theme, locale, base currency, currency mode toggle.

### 3.3 Domain Layer (`lib/domain/`)

Pure Dart, no Flutter or I/O imports. Defines:

- **Entities** — `Position`, `Transaction`, `CashBalance`, `Connection`, `ManualHolding`, `Alert`, `PriceQuote`, `FxRate`.
- **Use cases** — `GetAggregatedPortfolio`, `ConvertToBaseCurrency`, `EvaluateAlert`, `ExportReport`.
- **Repository interfaces** — abstract contracts implemented by the data layer.

### 3.4 Data Layer (`lib/data/`)

| Module | Responsibility |
|---|---|
| `repositories/` | Concrete implementations of domain repository interfaces; merge local cache + remote |
| `remote/backend_client` | REST + WebSocket client to the Backend Proxy Service; attaches Firebase ID token |
| `remote/firestore_client` | Read/write user settings, manual holdings, alerts, encrypted credential blobs |
| `local/database` | Drift (SQLite) schema and DAOs for cached positions, transactions, FX rates |
| `local/secure_storage` | flutter_secure_storage wrapper for credentials and the user-derived encryption key |
| `crypto/e2e` | Key derivation (PBKDF2/Argon2), AES-GCM encrypt/decrypt of credential blobs |

### 3.5 Cross-Cutting Modules

| Module | Responsibility |
|---|---|
| `lib/i18n/` | ARB-based localization (`en`, `zh_Hant`); locale switcher |
| `lib/theme/` | Material 3 light + dark themes, follow-system logic |
| `lib/notifications/` | Firebase Cloud Messaging registration, foreground / background handlers |
| `lib/app_lock/` | Biometric / PIN gate using `local_auth` |
| `lib/logging/` | Structured logging + Crashlytics adapter |
| `lib/router/` | go_router configuration |

### 3.6 Client-side Dependencies (within the app)

```
Presentation → State → Domain ← Data
                              ↑
                         (impl. of Domain interfaces)

Cross-cutting modules (i18n, theme, notifications, app_lock, logging, router)
are consumed by Presentation and configured at app bootstrap.
```

---

## 4. Backend Proxy Service — Modules

Single Docker image, FastAPI app. Internally structured as:

### 4.1 API Layer (`app/api/`)

- `/v1/connections` — CRUD on broker/exchange connections (server-side mode).
- `/v1/portfolio` — aggregated snapshot for the authenticated user.
- `/v1/positions`, `/v1/transactions`, `/v1/balances` — per-source detail.
- `/v1/quotes/stream` — WebSocket endpoint multiplexing live quotes.
- `/v1/fx` — current FX rates.
- `/v1/alerts` — alert evaluation status (definitions live in Firestore).
- `/healthz`, `/metrics` — ops endpoints.

### 4.2 Auth Middleware

Verifies the Firebase ID token on every request using the Firebase Admin SDK; injects `user_id` into request context. No separate user store in the backend.

### 4.3 Source Adapter Layer (`app/adapters/`)

One adapter per external source, all implementing a common `SourceAdapter` protocol:

```python
class SourceAdapter(Protocol):
    async def list_positions(...) -> list[Position]
    async def list_balances(...) -> list[CashBalance]
    async def list_transactions(...) -> list[Transaction]
    async def stream_quotes(symbols) -> AsyncIterator[Quote]
```

| Adapter | Notes |
|---|---|
| `longbridge` | Uses official LongBridge OpenAPI SDK (Python) |
| `ibkr` | Talks to a co-located IB Gateway / Client Portal Gateway (sidecar container) |
| `futu` | Talks to a co-located Futu OpenD (sidecar container) |
| `binance` | REST + WS, read-only API key, supports both binance.com and binance.us |

Sidecar gateways (IBGW, OpenD) run as additional containers in the same Docker network; the adapter speaks to them over localhost.

### 4.4 Aggregation Service (`app/services/aggregator.py`)

Fans out to relevant adapters in parallel, normalizes results into the unified domain shape, attaches FX rates, computes per-broker and total P&L.

### 4.5 FX Service (`app/services/fx.py`)

Wraps an FX rate provider (provider TBD in detailed design — candidates: exchangerate.host, openexchangerates.org). Caches rates in-process and in Firestore for client reuse.

### 4.6 Credential Vault (`app/services/vault.py`)

Two modes per connection:

- **E2E mode (default)** — backend never sees plaintext credentials. The user must be online and unlocked for the backend to make broker calls; the client sends a short-lived decrypted token to the backend in the request, the backend uses it and drops it.
- **Server-key mode (opt-in)** — credentials encrypted at rest with a KMS-managed key (GCP KMS or equivalent). Enables background sync and alert evaluation while the user is offline.

The user toggles this per connection. The mode is recorded in Firestore.

### 4.7 Alert Worker (`app/workers/alerts.py`)

Background task that periodically:

1. Reads alert definitions from Firestore.
2. For users whose connections are in server-key mode, evaluates current prices / P&L.
3. On trigger, dispatches FCM push via Firebase Admin SDK and records the event.

Alerts on E2E-only users only fire when the app is foregrounded; the client evaluates locally.

### 4.8 Backend Internal Dependencies

```
                  API Layer
                     │
              Auth Middleware
                     │
              ┌──────┴──────┐
              ▼             ▼
        Aggregator       Vault
              │             │
        ┌─────┴─────┐       │
        ▼           ▼       ▼
   Adapters    FX Service  KMS
        │
   ┌────┼─────────────┐
   ▼    ▼    ▼        ▼
  LB  IBKR  Futu    Binance
       │     │
       ▼     ▼
   IBGW   OpenD  (sidecar containers)

Alert Worker reads Firestore + uses Aggregator/Vault, dispatches via FCM.
```

---

## 5. Firebase Project — Modules

| Service | Use |
|---|---|
| **Auth** | Email/password + optional social providers; emits ID tokens consumed by both the client and the backend |
| **Firestore** | Per-user documents: settings, manual holdings, alert definitions, connection metadata (encrypted credential blob for E2E mode, opaque ref for server-key mode), alert event log |
| **Cloud Messaging** | Push delivery; client registers device tokens, backend (and optionally client) sends messages |
| **Crashlytics** | Opt-in crash + non-fatal reporting from the Flutter client |

Firestore security rules restrict every document to its owning `uid`.

---

## 6. External Integrations

| Source | Protocol | Hosted by us? |
|---|---|---|
| LongBridge OpenAPI | HTTPS + WebSocket | No |
| IBKR | HTTPS via IB Gateway / Client Portal Gateway | Yes — sidecar container |
| Futu OpenAPI | TCP via OpenD gateway | Yes — sidecar container |
| Binance | HTTPS + WebSocket (binance.com / binance.us) | No |
| FX rate provider | HTTPS | No |

---

## 7. Cross-Cutting Concerns

### 7.1 Security

- All transport TLS.
- Firebase ID token required on every backend call; backend never trusts client-supplied user IDs.
- Local credentials in OS-secure storage; cloud-stored credentials encrypted (client-side or KMS depending on mode).
- IP allow-listing recommended at Binance / LongBridge where supported (documented for users).
- Optional biometric / PIN gate on app open.

### 7.2 Resilience

- Adapter failures isolated: the aggregator returns partial results plus a per-source health status, so one broker's outage does not blank the dashboard.
- Rate-limit aware retries with exponential backoff in each adapter.
- Local cache (Drift) ensures the last-known portfolio renders instantly while a refresh is in flight.

### 7.3 Observability

- Backend: structured JSON logs, Prometheus `/metrics`, request tracing IDs propagated from the client.
- Client: Crashlytics + a debug log viewer screen in non-release builds.

### 7.4 Internationalization

- All user-facing strings in `.arb` files for `en` and `zh_Hant`.
- Number, currency, and date formatting via the `intl` package, locale-aware.

### 7.5 Refresh Strategy (Implementation Map)

| Data | Mechanism |
|---|---|
| Live quotes | Backend multiplexes broker / Binance WebSocket streams; pushed to clients over a single `/v1/quotes/stream` WS connection |
| Positions, balances | Polled by client on app foreground, on pull-to-refresh, and at a configurable interval |
| Transactions | Lazy-loaded on demand; incrementally cached locally |
| FX rates | Cached on backend (TTL ~minutes), pushed to client on subscription |

---

## 8. Module Dependency Summary

```
Flutter Client ──REST/WS──▶ Backend Proxy ──▶ Source Adapters ──▶ External APIs
       │                          │
       │                          ├──▶ FX Service ──▶ FX Provider
       │                          ├──▶ Vault ──▶ KMS
       │                          └──▶ Alert Worker ──▶ FCM
       │
       ├──Auth/Firestore/FCM──▶ Firebase
       │
       └──Local──▶ Drift DB + Secure Storage + E2E Crypto
```

---

## 9. Mapping to Proposal Requirements

| Proposal section | Modules that fulfill it |
|---|---|
| §4 Data sources | Backend Source Adapters + sidecar gateways |
| §5 Data retrieved | Adapter `list_*` methods + Drift cache |
| §6 Refresh strategy | Backend WS multiplexer + client polling logic |
| §7.1 Visualizations | Presentation `screens/dashboard`, `positions`, `charts` |
| §7.2 Currency toggle | Domain `ConvertToBaseCurrency` + FX Service + settingsProvider |
| §7.3 Localization | `lib/i18n` |
| §7.4 Theme | `lib/theme` |
| §8 Alerts / manual / export | `alerts` screens + Alert Worker; `connections` (manual); export utility in domain layer |
| §9.1 Auth | Firebase Auth + Auth Middleware |
| §9.2 Storage | Secure storage + Drift + Firestore + Vault (hybrid E2E) |
| §10 Non-functional | Cross-cutting concerns §7 |

---

## 10. Open Items for Detailed Design

1. Final FX rate provider selection.
2. Exact data schema in Firestore and Drift.
3. Precise REST/WS API contracts between client and backend.
4. KMS provider choice for server-key mode (GCP KMS vs. self-managed).
5. Exact deployment topology for the Docker image plus IBGW / OpenD sidecars (compose vs. single-host Kubernetes).
6. Web-platform handling of `flutter_secure_storage` limitations and Drift via sql.js.
7. Strategy for IBKR session keep-alive (Client Portal Gateway requires periodic re-auth).
