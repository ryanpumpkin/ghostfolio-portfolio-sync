#!/usr/bin/env bash
# Send the SMS verification code to a running futu-opend container via
# its Telnet console.
#
# Usage:
#   infra/futu-opend/verify-sms.sh <6-digit-code>
#
# Run this *after* the container logs "SMS verification code required".
# Futu sends the code to the phone on file. Codes expire, so be quick.
#
# Two things this script learned the hard way (2026-09-06):
#
#   1. `nc` is NOT installed on the Synology NAS, so the original
#      netcat implementation silently could not run there. We use
#      python3, which is present.
#
#   2. OpenD's console `exit` command SHUTS DOWN OpenD — it does not
#      merely close the telnet session. The original script sent `exit`
#      after the code, so every successful verification was immediately
#      followed by "Futu OpenD has exited". We just close the socket
#      instead.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <sms-code>" >&2
  exit 1
fi

CODE="$1"
HOST="${FUTU_OPEND_TELNET_HOST:-127.0.0.1}"
PORT="${FUTU_OPEND_TELNET_PORT:-22222}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (nc is not available on the NAS)" >&2
  exit 1
fi

python3 - "$HOST" "$PORT" "$CODE" <<'PY'
import socket
import sys
import time

host, port, code = sys.argv[1], int(sys.argv[2]), sys.argv[3]

sock = socket.create_connection((host, port), timeout=10)
sock.settimeout(5)
time.sleep(1)
try:
    print("banner:", sock.recv(4096).decode(errors="replace").strip()[:200])
except OSError:
    pass

# OpenD's telnet protocol expects CRLF line endings.
sock.sendall(("input_phone_verify_code -code=%s\r\n" % code).encode())
time.sleep(3)

received = b""
try:
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        received += chunk
except OSError:
    pass

text = received.decode(errors="replace").strip()
print("response:", text[:800])

# Deliberately NOT sending `exit` — that command terminates OpenD.
sock.close()

sys.exit(0 if "Login successful" in text or "successful" in text.lower() else 1)
PY
