# flutter-state

Riverpod providers wiring domain use cases and repositories into the UI.

## Subtasks

- [x] `authProvider` — exposes current Firebase user; sign-in / sign-out actions
- [x] `settingsProvider` — theme mode, locale, base currency, currency display mode (base vs native), persisted via SettingsRepository
- [x] `connectionsProvider` — list of connections + per-source health; add/remove/updateMode actions
- [x] `portfolioProvider` — `AsyncNotifier` returning `PortfolioSnapshot`; refresh action
- [x] `quotesProvider` — `StreamProvider.family<PriceQuote, String symbol>` subscribing through `QuotesRepository`
- [x] `transactionsProvider` — paginated `AsyncNotifier`; filters (source, date range, type)
- [x] `alertsProvider` — list + CRUD + trigger history
- [x] `manualHoldingsProvider` — CRUD via repository
- [x] `fxProvider` — base→quote rate lookups with cache
- [x] `appLockProvider` — locked / unlocked state, attempt counter
- [x] Provider observers for logging in non-release builds
- [x] Unit tests for each provider using `ProviderContainer` overrides
