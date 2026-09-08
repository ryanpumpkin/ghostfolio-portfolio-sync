# flutter-bootstrap

Scaffold the Flutter project and set up cross-cutting concerns: router, theme, localization, structured logging.

## Subtasks

- [x] Run `flutter create` with iOS + Android + Web platforms enabled
- [x] Set Dart SDK + Flutter version constraints in `pubspec.yaml`
- [x] Configure analyzer (`analysis_options.yaml`) with `package:flutter_lints`
- [x] Add core dependencies: `flutter_riverpod`, `go_router`, `intl`, `drift`, `flutter_secure_storage`, `firebase_core`, `firebase_auth`, `cloud_firestore`, `firebase_messaging`, `firebase_crashlytics`
- [x] Create folder structure: `lib/{presentation,state,domain,data,i18n,theme,notifications,app_lock,logging,router}`
- [x] Implement `lib/router/app_router.dart` with go_router routes for every screen referenced in the design (auth, onboarding, dashboard, positions, charts, transactions, connections, alerts, settings)
- [x] Implement `lib/theme/app_theme.dart` with Material 3 light + dark themes and a `themeMode` follow-system default
- [x] Set up `lib/i18n/` with `intl_utils` or built-in `flutter_localizations`; create `app_en.arb` and `app_zh_Hant.arb` with placeholder keys
- [x] Implement `lib/logging/logger.dart` wrapping a structured logger and forwarding non-fatal errors to Crashlytics
- [x] Wire up `main.dart`: Firebase init, ProviderScope, MaterialApp.router, locale + theme bindings (Firebase init deferred to firebase-setup client SDK config; ProviderScope and MaterialApp.router wired)
- [x] Add a debug-only log viewer screen accessible from settings (only in non-release builds) — route `/settings/debug/logs`, gated by `kReleaseMode`
- [~] Verify app boots to a placeholder home on iOS simulator, Android emulator, and Chrome — widget tests cover boot; live device verification deferred to firebase-setup since real boot requires `firebase_options.dart`
