# Local Runbook

The proven recipe to get from a fresh clone to a working dashboard
that pulls real positions from a connected broker. Reflects the
actual state after the end-to-end smoke through May 2026.

## Prerequisites

| Tool | Version checked | Used for |
|---|---|---|
| Docker Desktop | 29.2.0 | Backend + sidecars |
| Flutter SDK | 3.x with web + iOS + Android | Client app |
| Python | 3.11+ (3.14 OK) | Local backend dev outside Docker |
| Firebase CLI | `firebase`, `flutterfire` | Deploy rules, generate platform config |
| A Firebase project | Free tier | Auth, Firestore, FCM |
| A broker API key | At least one of LongBridge / Binance / IBKR / Futu | Real data |

## One-time setup

### 1. Firebase project

- Create the project (or reuse `mbp-tracker-dev`).
- Enable **Authentication → Email/Password** sign-in.
- Enable **Firestore** in Native mode.
- Generate a service-account JSON from
  Project Settings → Service Accounts → Generate new private key.
- Save it to `backend/.secrets/firebase-service-account.json`
  (the `backend/.secrets/` directory is gitignored).
- The directory is bind-mounted into the backend container at
  `/home/mbp/.secrets/` — see `docker-compose.override.yml`.

### 2. Flutter platform config

From the Flutter project root:

```bash
cd flutter
flutterfire configure --project=<your-firebase-project-id>
```

This regenerates `lib/firebase_options.dart`, the iOS plist, and
the Android `google-services.json`. All three are committed.

### 3. KMS master key

The file-backed KMS auto-generates a 32-byte AES master on first
boot:

```
backend/.secrets/mbp-master.key
```

**Keep this file safe.** Losing it makes every server-key-mode
encrypted credential unreadable. (E2E-mode blobs are derived from
the user's PIN and are unaffected.)

### 4. Firestore security rules

```bash
cd firebase
firebase deploy --only firestore:rules,firestore:indexes \
  --project <your-firebase-project-id>
```

## .env

Copy `.env.example` to `.env` and tweak. The defaults that matter:

```env
MBP_ENV=development
MBP_AUTH_DISABLED=false        # MUST be false to see real users
MBP_FIREBASE_PROJECT_ID=mbp-tracker-dev
MBP_FIREBASE_CREDENTIALS_PATH=/home/mbp/.secrets/firebase-service-account.json
MBP_KMS_PROVIDER=file
MBP_KMS_KEY_ID=/home/mbp/.secrets/mbp-master.key
MBP_FX_PROVIDER=frankfurter     # ECB rates, no API key required
```

## Running

### Backend

```bash
docker compose up backend --no-deps -d   # skip IBKR/Futu sidecars
docker compose logs -f backend           # watch logs
```

`http://localhost:8000/healthz` should return `{"status":"ok",...}`.

### Flutter (web)

```bash
cd flutter
flutter run -d chrome
```

A Chrome window opens at `http://localhost:<random>/`.

## End-to-end test

1. **Create an email/password account.** First app load redirects
   to `/auth/sign-in` because there is no anonymous fallback (any
   prior anonymous user is regenerated on every Chrome profile
   reset, so we force real accounts). Sign up with any email.
2. **Settings → App Lock → Set PIN.** Pick 4–8 digits. Saving the
   PIN also derives the AES-GCM credential-encryption key into
   in-memory `credentialKeyProvider`.
3. **Connections → Add.** Pick `longbridge`, enter:
   - App Key / App Secret / Access Token from
     https://open.longportapp.com/
   The blob is AES-GCM encrypted in-browser and written to
   `users/{uid}/connections/{cid}.encryptedBlob` in Firestore.
   The plaintext never leaves the device unencrypted.
4. **Dashboard → 🔄 refresh icon.** If the in-memory key was wiped
   (auto-lock, hot-restart), a PIN prompt appears; enter your PIN.
   The browser sends `X-MBP-Creds` with a wrapped token; backend
   unwraps with the user's PIN-derived key, instantiates a
   `LongbridgeClient` per request, calls `stock_positions()` +
   `account_balance()` + `quote()`, and returns a snapshot.

Expected output: source tile shows **Healthy · synced N min ago**,
totals populated, allocation donut drawn, P&L computed against
last-close prices.

## Verifying credentials reach the backend

Look for these lines in `docker compose logs backend`:

```
get_snapshot user_id=<uid> connections_found=1 kinds=['longbridge']
  wrapped_keys=['conn-...']
list_for_user uid=<uid> firestore_docs=1
stock_positions channel[0] name=lb positions=3
quote-enrich: prices_resolved={'PLTR.US': 135.140, ...}
```

If `wrapped_keys=[]`, the Flutter side didn't attach the
`X-MBP-Creds` header — usually because the user hasn't entered
their PIN since last hot-restart or auto-lock.

If `connections_found=0`, the backend uid doesn't match what
Flutter wrote under. Re-check sign-in.

## Common gotchas

- **`drift_db_worker.dart.js: 404`** in browser console.
  Harmless. Drift falls back to in-page `sqlite3.wasm`. To get
  the off-thread worker you'd have to build it via
  `dart compile js` against a small worker stub.
- **`/home/mbp/.secrets` permission denied at backend startup.**
  The container user `mbp` needs `/home/mbp` writable — already
  configured in the Dockerfile. If you change the user, mirror
  the chown.
- **`exchangerate.host: missing_access_key`.** Their free tier
  was retired in late 2024. We default to `frankfurter` instead.
- **Sign-out wipes the PIN.** That's intentional. `signOut()`
  clears `flutter_secure_storage`, which contains both the PIN
  hash and the per-user salt that derives the encryption key.
  Re-add connections after each sign-out cycle.
- **Anonymous Firebase auth on Flutter Web isn't persistent**
  across fresh `flutter run` invocations (new Chrome profile =
  fresh IndexedDB). We deliberately don't auto-sign-in anonymously
  for this reason; the router redirects to `/auth/sign-in`.

## Resetting

```bash
docker compose down -v          # nuke containers + KMS master volume
rm backend/.secrets/mbp-master.key
# In Firebase console: delete all docs under users/{uid}/connections/
```
