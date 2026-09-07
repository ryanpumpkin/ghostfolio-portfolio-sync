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
# A cold OpenD takes minutes, not seconds, to write "Login successful".
# The old 300 s looked sufficient only because the wait was matching a
# previous session's log line within a second of starting.
READY_TIMEOUT="${FUTU_READY_TIMEOUT:-900}"
# Hard ceiling on the sync itself. The trap below cannot fire while the
# sync is still running, so without this a hung job keeps a logged-in
# broker session alive indefinitely — which is precisely what rule 3
# forbids. Observed: the futu SDK starts non-daemon threads, so a Python
# process that raises still never exits, and OpenD sat logged in for 24
# minutes behind a job that had already failed.
SYNC_TIMEOUT="${FUTU_SYNC_TIMEOUT:-1800}"
SYNC_NAME="mbp-futu-sync-$$"

log() { printf '[sync-futu] %s\n' "$*"; }

cleanup() {
    # Kill the sync container first: while it holds OpenD's network
    # namespace, OpenD cannot be removed, and a container that ignores
    # SIGTERM would otherwise outlive this script.
    $DOCKER rm -f "$SYNC_NAME" >/dev/null 2>&1 || true
    log "stopping OpenD"
    $DOCKER stop "$OPEND_CONTAINER" >/dev/null 2>&1 || true
}
# Stop OpenD however we exit — including on error or Ctrl-C. Leaving a
# logged-in broker session running is exactly what rule 3 forbids.
trap cleanup EXIT INT TERM

cd "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# Do not start OpenD again too soon
# ---------------------------------------------------------------------------
# OpenD counts its own startups (`IsAllowStartUpOpenD` in its GTWLog) and
# Futu throttles an account that logs in repeatedly. Learned the hard way
# on 2026-09-07: after eight starts in a few hours the login stopped
# happening at all — OpenD came up, loaded its config, printed its
# listening addresses, and then sat there. Neither port was ever bound and
# nothing was written to the log after startup.
#
# The tell is the GTWLog filename: a successful start is
# `GTWLog_<userid>_*.log`, a throttled one is `GTWLog_0_*.log` — user id
# zero, never logged in.
#
# There is no way to code around this, so the fix is not to cause it. A
# sync every few hours is fine; a burst is not.
MIN_RESTART_GAP="${FUTU_MIN_RESTART_GAP:-1800}"
STAMP_FILE="${FUTU_START_STAMP:-${TMPDIR:-/tmp}/mbp-futu-last-start}"

if [ -f "$STAMP_FILE" ]; then
    last_start=$(cat "$STAMP_FILE" 2>/dev/null || echo 0)
    since=$(( $(date +%s) - last_start ))
    if [ "$since" -lt "$MIN_RESTART_GAP" ] && [ "$since" -ge 0 ]; then
        log "REFUSING to start OpenD: last start was ${since}s ago, minimum"
        log "  gap is ${MIN_RESTART_GAP}s. Futu throttles repeated logins and"
        log "  the recovery is measured in hours, not minutes. Override with"
        log "  FUTU_MIN_RESTART_GAP=0 only if you know the account is idle."
        exit 2
    fi
fi
log "starting OpenD"
# `--profile futu` because the service is deliberately excluded from the
# default stack.
# `</dev/null` here and on the probe below: this script's own stdin is
# reserved for the sync container, which is how a secret reaches it
# without ever touching argv, the environment, or a file on the host.
# Any earlier command that reads stdin would eat it.
$COMPOSE --profile futu up -d futu-opend >/dev/null </dev/null

# Stamp only once the container is actually up. Writing it beforehand
# meant a run that died before OpenD ever started — a wrong COMPOSE_BIN
# was enough — still burned the full 30-minute budget, and the retry
# then had to override the very guard that protects the account.
date +%s > "$STAMP_FILE" 2>/dev/null || true

# Only look at output from THIS session. `docker logs` keeps everything
# the container has ever written, across stops and starts, so a plain
# read matched the *previous* run's "Login successful" within a second of
# starting — and every timeout downstream was then measured from the
# wrong instant.
STARTED_AT=$($DOCKER inspect -f '{{.State.StartedAt}}' "$OPEND_CONTAINER")

log "waiting for login (up to ${READY_TIMEOUT}s)"
deadline=$(( $(date +%s) + READY_TIMEOUT ))
ready=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    logs=$($DOCKER logs --since "$STARTED_AT" "$OPEND_CONTAINER" 2>&1 | tr -d '\r' || true)
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
    # Distinguish "slow" from "throttled", because they need opposite
    # responses: waiting longer helps the first and makes the second
    # worse. OpenD names its own log after the logged-in user id, so a
    # `GTWLog_0_*` file from this session means it never logged in —
    # nothing in `docker logs` says so.
    newest_log=$($DOCKER exec "$OPEND_CONTAINER" sh -c \
        'ls -t /root/.com.futunn.FutuOpenD/Log/GTWLog_*.log 2>/dev/null | head -1' \
        2>/dev/null || true)
    case "$newest_log" in
        *"/GTWLog_0_"*)
            log "  OpenD never logged in — its log for this session is"
            log "  ${newest_log##*/}, and the 0 is the user id."
            log "  Futu throttles an account that starts OpenD repeatedly."
            log "  Wait a few hours; restarting again extends it."
            ;;
    esac
    exit 1
fi
log "login done; waiting for the API port to accept connections"

# "Login successful" in the log is NOT the same as "port 11111 is
# listening": OpenD writes it first and opens the API socket some seconds
# later. Starting the sync on the log line alone means the SDK burns its
# connect budget on ECONNREFUSED retries — observed failing even a 120 s
# budget on a slow start. Probe the socket from inside OpenD's own network
# namespace, which is the only place it is reachable at all.
# One container that polls internally, rather than one container per
# attempt: starting a container on this host costs several seconds, so a
# probe-per-attempt loop spent its whole budget on docker startup and
# reported "never opened" against a port that was in fact open.
set +e
$DOCKER run --rm --network "container:${OPEND_CONTAINER}" \
    --entrypoint python "$SYNC_IMAGE" -c '
import socket, sys, time
# Measured against real OpenD 10.6.6608: the API port opens three to four
# minutes AFTER "Login successful" is written, not seconds. A 180 s budget
# reported "never opened" against a port that opened at ~210 s.
deadline = time.time() + 600
while time.time() < deadline:
    probe = socket.socket()
    probe.settimeout(3)
    if probe.connect_ex(("127.0.0.1", 11111)) == 0:
        probe.close()
        sys.exit(0)
    probe.close()
    time.sleep(2)
sys.exit(1)
' </dev/null
port_ready=$?
set -e

if [ "$port_ready" -ne 0 ]; then
    log "FATAL: OpenD logged in but never opened port 11111"
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
# The sync needs more than OpenD: the app itself, the symbol config it
# refuses to guess without (§7.1), and the idempotency ledger that makes a
# re-run a no-op rather than a duplicate (§3.3). The ledger directory is
# the only writable mount; everything else is read-only on purpose.
LEDGER_DIR="${FUTU_SYNC_LEDGER_DIR:-$PROJECT_DIR/../mbp-sync-data}"
mkdir -p "$LEDGER_DIR"

set +e
timeout --signal=TERM --kill-after=30 "$SYNC_TIMEOUT" \
$DOCKER run --rm -i \
    --name "$SYNC_NAME" \
    --network "container:${OPEND_CONTAINER}" \
    --env-file "$PROJECT_DIR/.env" \
    -e MBP_FUTU_OPEND_HOST=127.0.0.1 \
    -e MBP_FUTU_OPEND_PORT=11111 \
    -v "$PROJECT_DIR/backend:/app" \
    -v "$PROJECT_DIR/config:/config:ro" \
    -v "$LEDGER_DIR:/data" \
    -w /app \
    --entrypoint "$1" \
    "$SYNC_IMAGE" "${@:2}"
sync_status=$?
set -e

if [ "$sync_status" -eq 124 ] || [ "$sync_status" -eq 137 ]; then
    log "FATAL: sync exceeded ${SYNC_TIMEOUT}s and was killed."
    log "  OpenD is being stopped regardless — see the trap."
    exit 1
fi

log "sync finished (exit $sync_status)"
# OpenD is stopped by the EXIT trap.
