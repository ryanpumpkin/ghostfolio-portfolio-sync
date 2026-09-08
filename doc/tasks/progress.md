# Overall Progress

Each module has its own task file under `doc/tasks/`. Tick a module here once **all** its subtasks are done.

## Flutter Client

- [x] [flutter-bootstrap](./flutter-bootstrap.md) — project scaffold, router, theme, i18n, logging (28 tests passing, lint clean, ~97% coverage); firebase_options.dart + platform config files generated via flutterfire configure
- [x] [flutter-domain](./flutter-domain.md) — entities, repo interfaces, use cases (54 domain tests passing, 99% domain coverage, lint clean)
- [x] [flutter-data](./flutter-data.md) — Drift, secure storage, E2E crypto, remote clients, repos (166 tests passing, lint clean; 87.47% coverage on hand-written `lib/data/*` excluding generated `app_database.g.dart`)
- [x] [flutter-state](./flutter-state.md) — Riverpod providers (184 tests passing, `flutter analyze` clean, `lib/state/*` coverage 94.12%)
- [x] [flutter-presentation](./flutter-presentation.md) — all screens and widgets (234 tests passing, `flutter analyze` clean, `lib/presentation/*` coverage 80.90%)
- [x] [flutter-auth-and-lock](./flutter-auth-and-lock.md) — Firebase Auth + biometric/PIN lock (209 tests passing, `flutter analyze` clean, auth/lock coverage 87.37%)
- [x] [flutter-notifications](./flutter-notifications.md) — FCM client integration

## Backend Proxy

- [x] [backend-bootstrap](./backend-bootstrap.md) — FastAPI scaffold, auth middleware, ops endpoints
- [x] [backend-adapters](./backend-adapters.md) — LongBridge, IBKR, Futu, Binance adapters with retry/health; SDKs dependency-injected behind Protocol wrappers (69 tests passing, 97.73% project coverage, lint + mypy --strict clean)
- [x] [backend-aggregator-and-fx](./backend-aggregator-and-fx.md) — aggregation + FX service (77 tests passing; ruff + mypy --strict clean; API/services coverage 96.58%)
- [x] [backend-vault](./backend-vault.md) — credential vault (E2E + KMS) (87 tests passing; ruff + mypy --strict clean; backend coverage 97.20%)
- [x] [backend-alert-worker](./backend-alert-worker.md) — background alert evaluator (90 tests passing; ruff + mypy --strict clean; backend coverage 97.20%)

## Platform / Infra

- [x] [firebase-setup](./firebase-setup.md) — rules, indexes, schema, emulator tests (10/10 passing); client SDK config files generated via flutterfire configure
- [x] [infra-deployment](./infra-deployment.md) — docker-compose with IBKR/Futu sidecars, Firebase emulator, smoke test (`docker compose config` validates; smoke test documented and executable)

---

**Legend:** `[ ]` not started · `[~]` in progress · `[x]` done. Update both the module file and this index when status changes.
