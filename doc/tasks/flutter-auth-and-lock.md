# flutter-auth-and-lock

Firebase Authentication integration plus the biometric / PIN app-lock gate.

## Subtasks

### Firebase Auth

- [x] Add `firebase_auth` and platform config (project-bound Firebase plist/json/web values remain generated via `flutterfire configure`)
- [x] Implement `AuthRepository` against `FirebaseAuth.instance`
- [x] Email/password sign-up with email verification
- [x] Email/password sign-in
- [x] Password reset flow
- [x] Sign-out clears in-memory caches and secure-storage session keys
- [x] Optional: Google / Apple sign-in providers (capability hooks + explicit unsupported stubs)

### App-lock (`lib/app_lock/`)

- [x] PIN setup screen (set + confirm); store PIN hash in secure storage
- [x] Biometric prompt via `local_auth` (Touch ID / Face ID / Android biometric)
- [x] App-lock gate widget at the router root: blocks navigation when locked
- [x] Auto-lock on app background after configurable timeout
- [x] Failed-attempt counter with backoff
- [x] Settings toggles: enable lock, biometric on/off, timeout duration

### Tests

- [x] Unit tests for `AuthRepository` with `firebase_auth_mocks`
- [x] Widget test for the app-lock gate
