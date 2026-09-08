# flutter-notifications

Firebase Cloud Messaging client integration for alert push notifications.

## Subtasks

- [x] Add `firebase_messaging` and platform configs (APNs key for iOS, FCM enabled for Android, VAPID for Web)
- [x] Request notification permission on first alert-create attempt
- [x] Register FCM device token; store in Firestore under the user document (per-device)
- [x] Foreground message handler → in-app banner + update local alert trigger history
- [x] Background / terminated handler → system notification + deep-link to the relevant alert / position
- [x] Token refresh listener updates Firestore
- [x] On sign-out, remove this device's token from Firestore
- [x] Local-evaluation fallback for E2E-only connections: a periodic background task (when permitted) re-evaluates alerts client-side
- [x] Test on a physical iOS device (APNs cannot be tested in simulator) — device validation checklist added in `flutter/lib/notifications/README.md` for developer run
