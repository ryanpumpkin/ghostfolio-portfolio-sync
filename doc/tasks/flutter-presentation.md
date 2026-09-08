# flutter-presentation

All UI screens and reusable widgets. Each screen consumes Riverpod providers; no direct repository / domain calls.

## Subtasks

### Reusable widgets (`lib/presentation/widgets/`)

- [x] `PnlBadge` — colored absolute + % change
- [x] `CurrencyAmount` — locale-aware formatter respecting base/native toggle
- [x] `SourceTile` — broker/exchange logo, label, status indicator
- [x] `PositionRow` — symbol, quantity, value, P&L
- [x] `LineChartCard` — wrapper around `fl_chart` for time series
- [x] `AllocationDonut` — pie/donut for allocation breakdowns
- [x] `EmptyState`, `ErrorBanner`, `LoadingShimmer`

### Screens (`lib/presentation/screens/`)

- [x] `auth/` — sign-in, sign-up, forgot-password
- [x] `onboarding/` — base currency picker, first connection wizard
- [x] `dashboard/` — totals, per-source tiles, allocation donut, navigation entry points
- [x] `positions/` — sortable/filterable per-position list; tap → detail sheet
- [x] `charts/` — portfolio value time series + P&L time series + allocation (multiple tabs)
- [x] `transactions/` — paginated list with filter chips and export action
- [x] `connections/` — list connections, add new (broker chooser → broker-specific wizard), remove, toggle credential mode
- [x] `connections/manual/` — manual holding CRUD form
- [x] `alerts/` — list + create/edit form with kind, scope, threshold
- [x] `settings/` — currency mode toggle, base currency picker, theme, locale, app-lock toggle, export, sign-out, debug log viewer (non-release)

### Navigation & layout

- [x] Bottom nav / drawer for top-level screens
- [x] Responsive layout: phone (compact), tablet (split-view), web (wide)

### Tests

- [x] Widget tests for at least: dashboard happy path, positions sorting, alerts form validation
- [x] Golden tests for key widgets (PnlBadge, CurrencyAmount, AllocationDonut)
