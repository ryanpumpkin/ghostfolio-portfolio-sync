#!/usr/bin/env bash
# Cap the job logs. Called at the start of each sync run.
#
# Why not logrotate: it is installed on this NAS but nothing schedules
# it — /etc/crontab, /etc/cron.* and synocrond mention it nowhere, and
# /var/log contains not one rotated file. A config dropped into
# /etc/logrotate.d/ would have looked like a fix and done nothing.
#
# Why copy-and-truncate rather than rename: cron holds the log open and
# redirects into it with `>>`. Renaming the file leaves that descriptor
# pointing at the renamed inode, so the running job keeps writing to the
# archive while the new log stays empty. Truncating in place keeps the
# descriptor valid and the offset resets on the next append.
set -uo pipefail

LOG_DIR="${MBP_LOG_DIR:-/volume1/docker/mbp/logs}"
MAX_BYTES="${MBP_LOG_MAX_BYTES:-5242880}"   # 5 MiB
KEEP="${MBP_LOG_KEEP:-3}"

[ -d "$LOG_DIR" ] || exit 0

for log in "$LOG_DIR"/*.log; do
    [ -f "$log" ] || continue
    size=$(wc -c < "$log" 2>/dev/null || echo 0)
    [ "$size" -gt "$MAX_BYTES" ] || continue

    # Age the archives, oldest first, so nothing is overwritten early.
    i=$KEEP
    while [ "$i" -gt 1 ]; do
        prev=$((i - 1))
        [ -f "$log.$prev" ] && mv -f "$log.$prev" "$log.$i"
        i=$prev
    done
    cp -f "$log" "$log.1" && : > "$log"
    printf '[rotate-logs] %s rotated at %s bytes\n' \
        "$(basename "$log")" "$size" >> "$log"
done
