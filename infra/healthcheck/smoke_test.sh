#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# infra/healthcheck/smoke_test.sh
#
# Minimal smoke test: verifies that the backend /healthz endpoint returns HTTP
# 200 and a valid JSON body with "status":"ok".
#
# Usage (from repo root):
#   docker compose up -d && sleep 5 && ./infra/healthcheck/smoke_test.sh
#
# Or with a custom host/port:
#   BACKEND_URL=http://localhost:8000 ./infra/healthcheck/smoke_test.sh
#
# Exit codes:
#   0 — smoke test passed
#   1 — smoke test failed (details printed to stderr)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
HEALTHZ_URL="${BACKEND_URL}/healthz"
MAX_RETRIES="${SMOKE_MAX_RETRIES:-10}"
RETRY_DELAY="${SMOKE_RETRY_DELAY:-3}"

# ── Helpers ───────────────────────────────────────────────────────────────────
pass() { printf '\033[0;32m[PASS]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[0;34m[INFO]\033[0m %s\n' "$*"; }

# ── Check dependencies ────────────────────────────────────────────────────────
if ! command -v curl >/dev/null 2>&1; then
  fail "curl is required but not found in PATH"
fi

# ── Wait for backend to become available ─────────────────────────────────────
info "Waiting for backend at ${HEALTHZ_URL} (max ${MAX_RETRIES} attempts, ${RETRY_DELAY}s apart)…"
attempt=0
while true; do
  attempt=$(( attempt + 1 ))
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${HEALTHZ_URL}" 2>/dev/null || echo "000")
  if [[ "${HTTP_CODE}" == "200" ]]; then
    break
  fi
  if [[ "${attempt}" -ge "${MAX_RETRIES}" ]]; then
    fail "Backend did not become healthy after ${MAX_RETRIES} attempts. Last HTTP code: ${HTTP_CODE}"
  fi
  info "Attempt ${attempt}/${MAX_RETRIES} — got HTTP ${HTTP_CODE}, retrying in ${RETRY_DELAY}s…"
  sleep "${RETRY_DELAY}"
done

# ── Assert HTTP 200 ───────────────────────────────────────────────────────────
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${HEALTHZ_URL}")
if [[ "${HTTP_CODE}" != "200" ]]; then
  fail "/healthz returned HTTP ${HTTP_CODE}, expected 200"
fi
pass "HTTP 200 from ${HEALTHZ_URL}"

# ── Assert JSON body contains status:ok ──────────────────────────────────────
BODY=$(curl -s --max-time 5 "${HEALTHZ_URL}")
if echo "${BODY}" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
  pass "Response body contains status:ok"
else
  fail "Response body did not contain status:ok — got: ${BODY}"
fi

# ── Assert version field present ─────────────────────────────────────────────
if echo "${BODY}" | grep -q '"version"'; then
  pass "Response body contains version field"
else
  fail "Response body missing version field — got: ${BODY}"
fi

info "Smoke test complete — all assertions passed."
