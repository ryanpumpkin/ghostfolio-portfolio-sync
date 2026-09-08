# backend-alert-worker

Background worker that evaluates price/P&L alerts for server-key-mode users and dispatches push notifications.

## Subtasks

### Scheduler (`app/workers/alerts.py`)

- [x] Async loop running at configurable interval (default 60s)
- [x] Graceful shutdown handler (SIGINT/SIGTERM)
- [x] Singleton lease (Firestore-based simple lock) so multiple replicas don't double-fire

### Evaluation

- [x] Load active alert definitions from Firestore for users with at least one server-key connection
- [x] Group alerts by required symbols; batch-fetch latest quotes via Aggregator / Quote Hub
- [x] Evaluate triggers; debounce so an alert fires at most once per cooldown window
- [x] Record trigger event in Firestore `alert_events/{id}`

### Notification dispatch

- [x] Look up user's registered FCM device tokens
- [x] Send via Firebase Admin SDK with deep-link payload (alert id, scope)
- [x] Remove tokens that come back as unregistered

### Client-side fallback note

- [x] For E2E-only users, document that alerts evaluate locally (see [flutter-notifications](./flutter-notifications.md)); worker skips them
  Worker repository loading is constrained to server-key-enabled alerts; E2E-only users are skipped and should rely on local client-side evaluation.

### Tests

- [x] Trigger logic unit tests (above/below thresholds, debounce)
- [x] FCM dispatch test with a mocked admin SDK
- [x] Lease test: only one replica processes a given tick
