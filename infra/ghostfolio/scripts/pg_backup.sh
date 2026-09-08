#!/bin/sh
# Ghostfolio PostgreSQL backup (§11.4).
#
# "A NAS is not a backup" — this script writes a compressed pg_dump and then
# REQUIRES that it be copied off the NAS. BACKUP_OFFSITE_DIR is mandatory for
# exactly that reason; if you point it at another folder on the same volume,
# a disk failure takes both copies.
#
# Usage:
#   ./pg_backup.sh                       # uses env / defaults below
#   BACKUP_OFFSITE_DIR=/mnt/x ./pg_backup.sh
#
# Schedule it from Synology Task Scheduler (or cron) daily, off-hours (§11.3).
#
# Restore (TEST THIS AT LEAST ONCE — an untested backup is a guess):
#   gunzip -c ghostfolio-YYYYmmdd-HHMMSS.sql.gz \
#     | sudo docker exec -i gf-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STACK_DIR=$(dirname "$SCRIPT_DIR")
ENV_FILE="${ENV_FILE:-$STACK_DIR/.env}"

CONTAINER="${PG_CONTAINER:-gf-postgres}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
LOCAL_DIR="${BACKUP_LOCAL_DIR:-$STACK_DIR/backups}"

if [ ! -r "$ENV_FILE" ]; then
    echo "FATAL: cannot read $ENV_FILE" >&2
    exit 1
fi

# Read only the two keys we need. We deliberately do NOT `source` the env
# file — it holds four secrets and sourcing it would leak them into this
# shell's environment and into any child process.
POSTGRES_USER=$(sed -n 's/^POSTGRES_USER=//p' "$ENV_FILE" | head -1)
POSTGRES_DB=$(sed -n 's/^POSTGRES_DB=//p' "$ENV_FILE" | head -1)

if [ -z "${POSTGRES_USER:-}" ] || [ -z "${POSTGRES_DB:-}" ]; then
    echo "FATAL: POSTGRES_USER / POSTGRES_DB not found in $ENV_FILE" >&2
    exit 1
fi

if [ -z "${BACKUP_OFFSITE_DIR:-}" ]; then
    echo "FATAL: BACKUP_OFFSITE_DIR is not set." >&2
    echo "       A backup that never leaves the NAS is not a backup (§11.4)." >&2
    exit 1
fi

STAMP=$(date -u +%Y%m%d-%H%M%S)
NAME="ghostfolio-${STAMP}.sql.gz"

mkdir -p "$LOCAL_DIR"
umask 077

echo "Dumping ${POSTGRES_DB} from ${CONTAINER}..."
# --clean --if-exists so the dump can be replayed over an existing database.
if ! docker exec "$CONTAINER" \
        pg_dump --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        | gzip -9 > "$LOCAL_DIR/$NAME"; then
    echo "FATAL: pg_dump failed" >&2
    rm -f "$LOCAL_DIR/$NAME"
    exit 1
fi

# A dump of an empty/failed database still produces a small valid gzip, so
# check the size rather than trusting the exit code alone.
SIZE=$(wc -c < "$LOCAL_DIR/$NAME")
if [ "$SIZE" -lt 1024 ]; then
    echo "FATAL: dump is only ${SIZE} bytes — treating as failed" >&2
    rm -f "$LOCAL_DIR/$NAME"
    exit 1
fi
echo "Wrote $LOCAL_DIR/$NAME (${SIZE} bytes)"

mkdir -p "$BACKUP_OFFSITE_DIR"
cp "$LOCAL_DIR/$NAME" "$BACKUP_OFFSITE_DIR/$NAME"
echo "Copied to $BACKUP_OFFSITE_DIR/$NAME"

# Prune local copies only. Off-NAS retention is the remote's business —
# deleting there from here would defeat the point of an off-site copy.
find "$LOCAL_DIR" -name 'ghostfolio-*.sql.gz' -type f -mtime "+${KEEP_DAYS}" -delete
echo "Done. Local retention: ${KEEP_DAYS} days."
