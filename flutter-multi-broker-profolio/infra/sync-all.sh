#!/usr/bin/env bash
# Refresh every source into Ghostfolio. Intended for cron.
#
# Order is deliberate: the two API sources first, because they are quick
# and cannot fail in a way that costs anything, then Futu — which has to
# start OpenD, wait several minutes for a login, and stop it again.
#
# A failure in one source must not prevent the others from running. Each
# is reported and the script's own exit code is the count of failures,
# so cron mail says how many rather than only that something broke.
#
# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
# Read from a root-owned 0600 file, one per line, and piped to each job on
# STDIN — never argv (visible in the host process list) and never `-e`
# (visible in `docker inspect` for the life of the container).
#
#   line 1: Ghostfolio API token
#   line 2: IBKR Flex web-service token
#
# A file on disk is weaker than the encrypted credential store, and this
# is a deliberate, temporary choice: the vault is not currently
# operational on this host. Neither token can move money — the Flex token
# is read-only by construction, and the Ghostfolio one reaches only the
# portfolio mirror. The Futu trade password is NOT here and is not used
# by any of this: the syncs never call `unlock_trade` (§4.3 rule 2).
set -uo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

DOCKER="${DOCKER_BIN:-/usr/local/bin/docker}"
COMPOSE="${COMPOSE_BIN:-/usr/local/bin/docker-compose}"
IMAGE="${MBP_SYNC_IMAGE:-mbp-backend:latest}"
SECRETS="${MBP_SYNC_SECRETS:-/volume1/docker/mbp/secrets/sync.secrets}"
LEDGER_DIR="${MBP_SYNC_LEDGER_DIR:-/volume1/docker/mbp/mbp-sync-data}"

log() { printf '[sync-all] %s %s\n' "$(date '+%F %T')" "$*"; }

if [ ! -r "$SECRETS" ]; then
    log "FATAL: no secrets file at $SECRETS"
    log "  Create it root-owned and 0600, one secret per line:"
    log "    1) Ghostfolio token   2) IBKR Flex token"
    exit 1
fi

failures=0

run_api_sync() {
    # $1 = module, $2 = how many secret lines it reads
    local module=$1 lines=$2
    log "running $module"
    if head -n "$lines" "$SECRETS" | $DOCKER run --rm -i \
        --network host \
        --env-file "$PROJECT_DIR/.env" \
        -v "$PROJECT_DIR/backend:/app" \
        -v "$PROJECT_DIR/config:/config:ro" \
        -v "$LEDGER_DIR:/data" \
        -w /app --entrypoint python "$IMAGE" -m "$module" --push
    then
        log "$module ok"
    else
        log "$module FAILED (exit $?)"
        failures=$((failures + 1))
    fi
}

run_api_sync tools.longbridge_sync 1
run_api_sync tools.ibkr_sync 2

# Futu last. `sync-futu.sh` owns the OpenD lifecycle and refuses to start
# it again too soon (Futu throttles repeated logins), so a refusal here is
# an expected outcome on a re-run, not a failure worth alarming about.
log "running tools.futu_sync"
head -n 1 "$SECRETS" | DOCKER_BIN="$DOCKER" COMPOSE_BIN="$COMPOSE" \
    "$SCRIPT_DIR/futu-opend/sync-futu.sh" python -m tools.futu_sync --push
futu_status=$?
case "$futu_status" in
    0) log "tools.futu_sync ok" ;;
    2) log "tools.futu_sync skipped — OpenD restart guard (expected on a re-run)" ;;
    *) log "tools.futu_sync FAILED (exit $futu_status)"; failures=$((failures + 1)) ;;
esac

log "done; $failures source(s) failed"
exit "$failures"
