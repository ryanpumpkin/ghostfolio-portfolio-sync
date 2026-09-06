#!/usr/bin/env bash
# Run one Futu sync with an ephemeral OpenD (spec §4.3 rule 3).
#
#     start OpenD  ->  run the sync  ->  stop OpenD
#
# OpenD holds a logged-in broker session, so it must not sit running 24/7.
# This reduces its exposure from "always" to "a few minutes a day".
#
# Usage:
#   infra/futu-opend/sync-futu.sh [command...]
#
# With no arguments it runs a read-only smoke check. Pass a command to run
# something else inside the sync container, e.g.
#   infra/futu-opend/sync-futu.sh python -m app.workers.futu_sync
#
# ---------------------------------------------------------------------------
# Why the sync runs in a container sharing OpenD's network namespace
# ---------------------------------------------------------------------------
# Verified against real OpenD 10.6.6608 on 2026-09-06: with OpenD bound to
# 0.0.0.0, EVERY trade_ctx call — including read-only accinfo_query and
# position_list_query — fails with:
#
#     "To ensure trading security, cross-network trade connections must
#      be encrypted."
#
# That restriction is not specific to unlock_trade, so removing unlock did
# not lift it. Enabling RSA would, but the encrypted handshake was
# previously found unstable. Sharing the namespace keeps the connection on
# localhost, which OpenD accepts unencrypted — and unlike the old design,
# only this short-lived job shares it, not the backend. That is what lets
# OpenD be stopped the rest of the time and publish no ports at all.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)

DOCKER="${DOCKER_BIN:-docker}"
COMPOSE="${COMPOSE_BIN:-docker-compose}"
OPEND_CONTAINER="${FUTU_OPEND_CONTAINER:-mbp-futu-opend}"
SYNC_IMAGE="${FUTU_SYNC_IMAGE:-mbp-backend:latest}"
READY_TIMEOUT="${FUTU_READY_TIMEOUT:-300}"

log() { printf '[sync-futu] %s\n' "$*"; }

stop_opend() {
    log "stopping OpenD"
    $DOCKER stop "$OPEND_CONTAINER" >/dev/null 2>&1 || true
}
# Stop OpenD however we exit — including on error or Ctrl-C. Leaving a
# logged-in broker session running is exactly what rule 3 forbids.
trap stop_opend EXIT INT TERM

cd "$PROJECT_DIR"

log "starting OpenD"
# `--profile futu` because the service is deliberately excluded from the
# default stack.
$COMPOSE --profile futu up -d futu-opend >/dev/null

log "waiting for login (up to ${READY_TIMEOUT}s)"
deadline=$(( $(date +%s) + READY_TIMEOUT ))
ready=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    logs=$($DOCKER logs "$OPEND_CONTAINER" 2>&1 | tr -d '\r' || true)
    case "$logs" in
        # "Login successful" is the reliable marker. An earlier version
        # waited for "Required data is ready", which OpenD emits only
        # sometimes (it did not appear at all on a successful run), so the
        # script timed out on a session that was actually up and logged in.
        *"Login successful"*) ready=1; break ;;
        *"Required data is ready"*) ready=1; break ;;
        *"verification code required"*)
            log "FATAL: OpenD is asking for an SMS verification code."
            log "  Device trust lives in the futu-opend-home volume. If this"
            log "  appears, that volume was lost or replaced. Enter the code"
            log "  once with:"
            log "    infra/futu-opend/verify-sms.sh <code>"
            log "  and it will not be needed again."
            exit 1
            ;;
        *"password you"*"don't match"*)
            log "FATAL: OpenD rejected the login. Check"
            log "  FUTU_OPEND_LOGIN_PASSWORD_MD5 in .env — it is the MD5 of"
            log "  your Futu LOGIN password, not the trade password."
            exit 1
            ;;
    esac
    sleep 5
done

if [ "$ready" -ne 1 ]; then
    log "FATAL: OpenD did not become ready within ${READY_TIMEOUT}s"
    exit 1
fi
log "OpenD ready"

if [ "$#" -eq 0 ]; then
    set -- python -c '
from futu import OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv, RET_OK
ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.HK, host="127.0.0.1",
                          port=11111, security_firm=SecurityFirm.FUTUSECURITIES)
try:
    ret, data = ctx.position_list_query(trd_env=TrdEnv.REAL)
    print("positions:", len(data) if ret == RET_OK else data)
finally:
    ctx.close()
'
fi

log "running sync"
# Shares OpenD's network namespace, so OpenD sees 127.0.0.1 and accepts
# the connection unencrypted. Never calls unlock_trade (§4.3 rule 2).
$DOCKER run --rm \
    --network "container:${OPEND_CONTAINER}" \
    --env-file "$PROJECT_DIR/.env" \
    -e MBP_FUTU_OPEND_HOST=127.0.0.1 \
    -e MBP_FUTU_OPEND_PORT=11111 \
    --entrypoint "$1" \
    "$SYNC_IMAGE" "${@:2}"

log "sync finished"
# OpenD is stopped by the EXIT trap.
