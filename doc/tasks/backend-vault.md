# backend-vault

Credential vault with the hybrid E2E model: client-encrypted by default, KMS-encrypted on opt-in for background tasks.

## Subtasks

### Mode handling

- [x] Connection metadata records `credential_mode: "e2e" | "server-key"`
- [x] API enforces mode-appropriate flow (reject server-decrypt for E2E connections, etc.)

### E2E mode

- [x] Client sends a short-lived decryption token in each request that needs broker access
- [x] Backend decrypts the credential blob fetched from Firestore in-memory, uses it, then discards
- [x] No plaintext credential ever written to disk or logged

### Server-key mode

- [x] Pluggable KMS interface (`KmsProvider`): `encrypt`, `decrypt`, `rotate`
- [x] Default impl: GCP KMS (envelope encryption — DEK per connection, KEK in KMS)
- [x] Fallback impl for self-hosted deploys: file-backed master key (documented as less secure)
- [x] Persist encrypted credentials in Firestore (under connection metadata), DEK wrapped by KMS

### API

- [x] `POST /v1/connections` accepts (mode, encrypted_blob or plaintext_for_server_mode)
- [x] `PATCH /v1/connections/{id}/mode` switches mode (re-encrypt step)
- [x] `DELETE /v1/connections/{id}` wipes blob + revokes refs

### Audit

- [x] Structured log entry on every credential use (user_id, connection_id, mode, purpose) — no plaintext
- [x] Counter metric for failed decrypts

### Tests

- [x] Round-trip encrypt/decrypt for both modes
- [x] Mode-switch test: re-encrypts blob correctly
- [x] Negative test: server cannot decrypt an E2E blob without the client token
