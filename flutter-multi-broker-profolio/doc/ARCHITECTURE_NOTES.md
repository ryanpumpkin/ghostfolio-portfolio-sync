# Architecture Notes — Decisions Beyond the Original Spec

`proposal.md` and `detailed-design.md` describe what to build.
This document records concrete decisions that emerged during
implementation and aren't reflected there, plus rationale for
each so a future maintainer doesn't need to re-derive them.

## 1. Authentication

### Decision: No anonymous Firebase auth on Flutter Web

We initially auto-signed-in anonymous users in `main.dart` as a
quality-of-life shortcut. On Flutter Web this turned out to break
identity continuity:

- Anonymous users live in browser IndexedDB.
- A fresh `flutter run` opens a new Chrome instance with an empty
  user-data-dir → IndexedDB empty → no persisted user.
- Each restart minted a *new* anonymous uid. Saved Firestore
  connections were orphaned under prior uids and couldn't be
  decrypted (the per-user salt that derives the encryption key
  was also wiped).

We removed the anonymous fallback in `main.dart` and added a
GoRouter `redirect` in `app_router.dart` that pushes unauth'd
users to `/auth/sign-in`. Anonymous Firebase Auth is still
enabled in the console but no code path triggers it.

If you re-enable anonymous auth, plan for an upgrade-anonymous-
to-permanent flow on first email sign-in to migrate prior data.

### Decision: signOut wipes secure storage

`AuthRepositoryImpl.signOut` runs `SecureStoreAuthSessionCleaner`
which clears the entire `flutter_secure_storage` backend. This is
intentional: signing out severs the cryptographic link, since
both the PIN hash and per-user salt live there. **Side effect:
encrypted connection blobs from the prior session are
unrecoverable after sign-out.** The Settings sign-out tile also
invalidates `appLockProvider` and clears
`credentialKeyProvider` so the in-memory state doesn't lie about
PIN existence after the wipe.

## 2. Credential encryption

### Storage layout

`users/{uid}/connections/{cid}` Firestore document holds:

```
{
  id: string,
  kind: 'longbridge' | 'ibkr' | 'futu' | 'binance' | 'manual',
  label: string,
  status: 'unknown' | 'ok' | 'error' | 'disabled',
  credentialMode: 'e2e' | 'serverKey',
  encryptedBlob: string,         // E2E mode only
  lastSyncAt: ISO8601 string,    // set by aggregator after each call
  errorMessage: string | null    // set when status == 'error'
}
```

`encryptedBlob` is exactly the string produced by
`Ciphertext.toEncoded()` — i.e. `base64(utf8(json({n, c, m})))`.
Do **not** wrap that string in another `base64(jsonEncode(...))`
layer. That double-encoding was the root cause of a long
"missing wrapped credentials" debug session — fromEncoded
expected a Map, got a string.

### Wire format for the per-request E2E envelope

`E2eCrypto.wrapForBackend` produces:

```
base64(utf8(json({
  v: 1,
  expiresAt: <millis-since-epoch>,    // now + 2 min
  ct: <base64-encoded Ciphertext>
})))
```

The backend's `unwrap_from_backend` accepts either the same
Ciphertext-as-base64 shape OR an inlined `ct: {nonce, cipherBytes,
mac}` object. We use the base64 form on the wire.

### Header attachment

The Flutter `BackendClient` puts wrapped tokens in
`X-MBP-Creds`. Backend's `parse_wrapped_credentials_header`
dependency parses them into an `AggregationCredentialContext`
that the per-request `AdapterFactory` consumes. The unwrap key
is the user's PIN-derived AES-GCM key.

## 3. Adapter lifecycle

Per-request, never per-process. The factory resolves
`(connection_kind, plaintext_creds) -> SourceAdapter` on every
REST call. The adapter instance dies with the request. Even
SDKs that maintain a connection pool inside (e.g. `LongbridgeClient`
holds quote+trade contexts) are recreated per request — the
overhead is acceptable because dashboard refreshes are
infrequent and a SDK pool isn't shared between users anyway.

If you ever need persistent connections (e.g. for real-time
WebSocket streaming), wire a separate per-process registry that
keys on uid + connection_id and lives in a connection-per-user
pool.

## 4. KMS and master key

`MBP_KMS_PROVIDER=file` keeps a 32-byte AES master at
`MBP_KMS_KEY_ID` (default
`/home/mbp/.secrets/mbp-master.key`). The Docker compose
override bind-mounts `backend/.secrets/` into the container's
`/home/mbp/.secrets/` so the same key file persists across image
rebuilds. Without that bind-mount each rebuild would regenerate
the key and invalidate every server-key-mode blob.

E2E-mode blobs aren't affected by the master key — they're
encrypted with the user's PIN-derived key, which is reproducible
from the (PIN, per-user-salt) pair.

GCP/AWS KMS providers are stubbed in `app/services/kms/{aws,gcp}.py`
but not wired in. Switching to a managed KMS for production is a
small change: implement `KmsProvider` in those files and update
`build_kms_provider` in `vault.py`.

## 5. FX rates

### Decision: Frankfurter (ECB rates) as default

`exchangerate.host` retired its free no-key tier in late 2024
and now returns `error: missing_access_key`. We default to
Frankfurter (`api.frankfurter.dev`, free, no key, ECB-derived).

If you set `MBP_FX_PROVIDER=openexchangerates` and supply
`MBP_FX_PROVIDER_API_KEY`, you get `OpenExchangeRatesProvider`
which is more comprehensive but paid.

`get_rates_for` soft-fails per pair — an unsupported currency
returns 0-contribution instead of erroring the whole snapshot.
Frankfurter covers ~30 major currencies including HKD/USD.

### Cost-basis fallback for missing prices

When a broker returns `last_price: null` (markets closed, missing
permission, etc.), `LongBridgeAdapter._map_position` falls
through three tiers when computing `market_value`:

```
explicit market_value
  -> last_price * quantity
  -> avg_cost * quantity
```

The cost-basis fallback keeps the dashboard usable after-hours.
P&L naturally becomes 0 when market_value collapses to cost.

## 6. Live quote enrichment for LongBridge

`stock_positions()` on the trade API returns `last_price` for
*active* positions but null for many others. We follow up with
a single `QuoteContext.quote(symbols)` call to fetch
`last_done` for every held symbol and merge the price into each
position dict before handing off to the adapter.

Because the SDK returns slotted immutable dataclasses, we can't
`setattr` the live price onto them. We snapshot each position to
a plain `dict` via `_position_to_dict` and merge there.

## 7. Drift on Web

`flutter/lib/data/local/database/connection/web.dart` uses the
modern `WasmDatabase.open` API from `drift/wasm.dart`. It tries
to load `drift_db_worker.dart.js` for off-thread SQL; if missing
(default), drift falls back to in-page sqlite3 via the bundled
`flutter/web/sqlite3.wasm`. The 404 for `drift_db_worker.dart.js`
in the browser console is harmless.

To get the off-thread worker, write a small Dart entrypoint that
imports `drift_dev/web_utils.dart`, compile it with
`dart compile js`, and drop the output at
`flutter/web/drift_db_worker.dart.js`. We chose not to do this
because IndexedDB performance is fine for the current schema.

## 8. Docker quirks

### Non-root `mbp` user needs `$HOME`

The `futu-api` Python SDK touches `~/.futu*` on import, which
needs a writable home dir for the non-root container user.
Dockerfile creates `/home/mbp` with the right ownership and
exports `HOME=/home/mbp`. Without this, `import futu` raises
`PermissionError: '/home/mbp'` and the adapter factory's
deferred Futu import fails on first Futu request.

### Health-check heredoc

The earlier Dockerfile used a heredoc in `HEALTHCHECK CMD`
which BuildKit doesn't support. Replaced with a one-line
`python -c "import urllib.request, sys; ..."` invocation.

## 9. Flutter ↔ Backend wire-shape quirks

Three places where the original mappers needed to learn the
*real* backend wire shape rather than the camelCase fixtures the
sub-agents wrote first:

1. `Mappers.snapshotFromJson` accepts both `asOf` and `as_of`,
   both `balances` and `cashBalances`,
   `total_market_value` / `total_unrealized_pnl` as strings, etc.
2. `Mappers.positionFromJson` reads `last_price` (snake_case)
   into `currentPrice` (camelCase domain field).
3. `_num` accepts numeric strings ("0", "143.500") because the
   backend serializes `Decimal` as strings to preserve precision.

Don't try to "fix" the backend to be all-camelCase; the
serialization is correct (it's `pydantic`'s default for `Decimal`
+ a populate_by_name on field aliases). Just make the mappers
accept both shapes.

## 10. Identity layering at a glance

```
Firebase Auth user (uid: stable per email)
  └── PIN hash + per-user salt (flutter_secure_storage)
        └── Argon2id(PIN, salt) -> 32-byte AES-GCM key
              (held only in memory; wiped on lock/sign-out)
              └── encrypts each broker's credential JSON
                    └── stored as base64 in users/{uid}/connections/{cid}.encryptedBlob

Backend has:
  - Firebase Admin SDK (verifies the user's ID token)
  - File-backed AES master key (only used for server-key mode)
  - No PIN, no E2E key — those live only in the client.
```

The backend can read the encrypted blob from Firestore but can't
decrypt it without the user-supplied wrapped token on each
request.

## 11. Base-currency conversion: current rate (§6.6, §14 Q3)

**Decision: convert at the CURRENT spot rate, not the trade-date rate.**
Owner's call, 2026-09-06.

§6.6 requires picking one and applying it consistently, because both are
defensible but mixing them is not.

### What this means

Every foreign-currency value — market value, cost basis, realised P&L —
is converted to the base currency (HKD) using today's rate. The reported
return is therefore the **total return in HKD**, with the FX effect
folded in rather than broken out.

The trade-date alternative would have given the 原幣 return plus a
separately identifiable FX component. We do not get that, by choice.

### The consequence to be aware of

A pure currency move changes your reported return even when no position
changed. If USD strengthens against HKD, US holdings show a gain in HKD
terms without a single share moving. That is correct under this
convention — it is what "total return in my spending currency" means —
but it is the thing to remember before concluding a position performed
well or badly.

### Why this was cheap to adopt

The existing FX service was already current-rate only: `FxProvider.
fetch_rate(base, quote)` takes no date, and the Frankfurter provider
calls `/latest`. There is no historical-rate path in the codebase at all,
so this decision matches what was already built rather than requiring a
refactor.

**If anyone later adds a dated rate lookup, this is the decision it would
violate.** Historical rates should only be introduced together with a
deliberate reversal of this choice, not as a quiet capability.

## 12. Monthly digest delivery: email (§10, §14 Q4)

**Decision: email, not Slack.** Owner's call, 2026-09-06.

Lowest-friction option: the WIP already carries a working Gmail path —
`MBP_GMAIL_FROM_EMAIL` / `MBP_GMAIL_APP_PASSWORD` /
`MBP_GMAIL_DIGEST_RECIPIENT` in settings, and `send_digest_email` in
`app/services/watchlist.py`. The monthly portfolio digest reuses that
transport rather than introducing a second one.

Keep the body plain text (§10): "It should be readable on a phone lock
screen without opening anything."
