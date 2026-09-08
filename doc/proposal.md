# Multi-Broker Portfolio Tracker — Requirements Proposal

## 1. Overview

A cross-platform Flutter application that connects to multiple brokerage accounts and presents a unified view of holdings and profit/loss (P&L) across all of them. The product is built first for personal use, with the intent to release publicly at a later stage.

## 2. Goals

- Aggregate positions, balances, and P&L from multiple brokers into a single dashboard.
- Provide both at-a-glance summary and drill-down detail.
- Support both base-currency aggregation and native-currency inspection.
- Keep credentials secure (local-first) while offering optional cloud sync across the user's devices.

## 3. Target Platforms

- iOS (iPhone, iPad)
- Android (phones, tablets)
- Web (responsive browser app)

Desktop (macOS/Windows) is out of scope for the initial release.

## 4. Supported Data Sources

### 4.1 Stock Brokers

| Broker | Region | Likely connection method |
|---|---|---|
| 長橋 (LongBridge) | HK / SG / US | Official OpenAPI (token / app-key based) |
| Interactive Brokers (IBKR) | Global | Official API (Client Portal Web API or TWS/IBGW) |
| Futu / moomoo | HK / US / SG / CN | Futu OpenAPI (OpenD gateway) |

Connection method is **broker-dependent**: the app will use the most appropriate of API keys, OAuth, or token-based login per broker. The chosen mechanism for each broker will be finalized during the technical design phase based on what each broker's documentation supports.

### 4.2 Crypto Sources

| Source | Connection method | Notes |
|---|---|---|
| Binance | Read-only API key + secret | User creates an API key in Binance with "Enable Reading" only — no trade, no withdraw permissions. Spot balances, trade history, and live prices via REST + WebSocket. Both Binance.com and Binance.US endpoints to be supported based on user region. |

**Ledger hardware wallet and other self-custody wallets are NOT supported in v1** as a direct integration — Ledger Live exposes no public API. Users holding crypto in self-custody can either use the manual holdings entry feature (Section 8) or wait for a future on-chain watch-only address feature.

## 5. Data Retrieved Per Broker

For every connected broker or crypto account, the app will pull:

1. **Current positions / holdings** — symbol (stock ticker or crypto asset), quantity, average cost, current market price, market value, currency / quote asset.
2. **Realized and unrealized P&L** — both open-position P&L and closed-trade P&L where available.
3. **Cash / quote-asset balances** — available cash per currency, or stablecoin / quote-asset balances for Binance.
4. **Transaction history** — buys, sells, dividends, fees, deposits/withdrawals, crypto trades, as far back as each source exposes.

Historical depth is whatever each broker provides through its API; the app does **not** maintain its own daily snapshot store in v1.

## 6. Refresh Strategy

- **Live quotes (prices)** — streamed via websocket where the broker supports it; otherwise polled frequently while the relevant view is open.
- **Positions, balances, transactions** — refreshed on a periodic interval while the app is foreground, plus pull-to-refresh and on-demand sync after user actions (e.g. after re-opening the app).

This minimizes API quota usage while keeping prices visibly live.

## 7. User Interface

### 7.1 Visualizations

- **Aggregated dashboard** — total portfolio value, total unrealized P&L, total realized P&L, with a per-broker breakdown.
- **Per-position list** — every holding across all brokers, sortable, showing quantity, cost basis, current value, and P&L (absolute + %).
- **Time-series charts** — portfolio value and P&L over time (range depends on broker-provided history).
- **Asset allocation chart** — pie / donut breakdown by asset class, sector, and currency.

### 7.2 Currency Handling

Multi-currency holdings (USD, HKD, CNY, etc.) are supported with a **user-toggleable** display:

- **Base currency mode** — user picks a base currency (e.g. HKD or USD); all values converted using a live FX rate source.
- **Native mode** — values shown in each position's native currency, with sub-totals per currency.

The toggle is global and persists per user.

### 7.3 Localization

- English
- Traditional Chinese (繁體中文)

All user-facing strings localized; numeric / currency / date formatting follows the active locale.

### 7.4 Theme

Light and dark mode, automatically following the OS setting; manual override available in settings.

## 8. Additional Features

- **Price alerts and notifications** — user-configurable alerts on price thresholds or P&L change thresholds; delivered via push notifications.
- **Manual holdings entry** — user can add positions that are not in any connected broker (e.g. physical cash, crypto held elsewhere, real estate, private holdings). Manual holdings participate in the aggregated dashboard and allocation charts.
- **Export reports** — CSV and PDF exports of current portfolio snapshot and/or transaction history.

## 9. Accounts, Authentication & Storage

### 9.1 App-level Authentication

Account-based sign-in is required (email/password and/or social login). This account is the unit of identity for cloud sync, alerts, and entitlements.

### 9.2 Credential & Data Storage

- **Local-first** — broker API keys, OAuth tokens, and cached portfolio data are stored on the device in secure storage (iOS Keychain / Android Keystore / Web secure storage equivalents).
- **Optional cloud sync** — user can opt-in to encrypted backup/sync of broker connections, manual holdings, alerts, and preferences across their devices. Sync is end-to-end encrypted where feasible so the server cannot read raw broker credentials.

### 9.3 Backend Stack

Firebase will be used for the cloud component:

- **Firebase Authentication** — user sign-up / sign-in.
- **Cloud Firestore** — synced user settings, manual holdings, alert definitions, and (encrypted) broker connection metadata.
- **Firebase Cloud Messaging** — push notifications for alerts.

## 10. Non-Functional Requirements

- **Security** — secrets never logged; broker credentials encrypted at rest; HTTPS only; app-level biometric/PIN lock as a secondary safeguard (configurable).
- **Privacy** — no portfolio data shared with third parties; FX rate provider receives only currency pair queries, no balances.
- **Performance** — dashboard interactive within 2 seconds on warm start; live quote updates rendered without dropping frames.
- **Resilience** — broker outages or rate-limit errors are surfaced per-broker without taking down the rest of the dashboard.
- **Observability** — structured client logs, opt-in crash reporting (Firebase Crashlytics).

## 11. Project Phasing

**Phase 1 — MVP (personal use)**
- One broker integration end-to-end (proposed: LongBridge or Futu, whichever has the simpler API path).
- Dashboard, per-position list, native + base-currency toggle.
- On-device storage only; no cloud sync yet.
- English + Traditional Chinese.

**Phase 2 — Multi-broker + crypto**
- Add the remaining two broker integrations.
- Add Binance integration (read-only API key).
- Asset allocation chart and time-series chart (now spanning stocks + crypto).
- Manual holdings entry.

**Phase 3 — Cloud & polish**
- Firebase Auth + Firestore sync.
- Price alerts + push notifications.
- CSV / PDF export.

**Phase 4 — Public release**
- App Store / Play Store submission, public web deployment, marketing site, support channels.

No fixed deadline; build at a sustainable pace.

## 12. Out of Scope (v1)

- Order placement / trading from within the app (read-only viewer).
- Desktop (macOS / Windows) native builds.
- Tax-lot accounting and tax report generation beyond a simple realized-P&L export.
- Long-term internal snapshot store (history is whatever brokers expose).
- Social / sharing features.
- Direct integration with self-custody crypto wallets (Ledger, MetaMask, etc.).
- Crypto exchanges other than Binance (Coinbase, OKX, Kraken, etc.).
- On-chain public-address watch-only tracking.

## 13. Open Questions / To Confirm in Design Phase

1. Exact authentication method per broker (LongBridge token vs. OAuth; IBKR Client Portal vs. TWS gateway; Futu OpenD requires a local gateway — implications for mobile/web).
2. FX rate source (free vs. paid — e.g. exchangerate.host, OpenExchangeRates, broker-provided FX).
3. Push notification delivery on Web (FCM web push limitations).
4. Whether IBKR's required local gateway (TWS / IB Gateway / Client Portal Gateway) is acceptable for personal use, or whether a hosted alternative is needed before public release.
5. End-to-end encryption scheme for cloud-synced broker credentials.
