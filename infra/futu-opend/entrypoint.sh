#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${FUTU_OPEND_LOGIN_ACCOUNT:-}" ]]; then
  echo "FUTU_OPEND_LOGIN_ACCOUNT is required" >&2
  exit 1
fi
if [[ -z "${FUTU_OPEND_LOGIN_PASSWORD_MD5:-}" ]]; then
  echo "FUTU_OPEND_LOGIN_PASSWORD_MD5 is required" >&2
  exit 1
fi

export FUTU_OPEND_API_PORT="${FUTU_OPEND_API_PORT:-11111}"
export FUTU_OPEND_LOG_LEVEL="${FUTU_OPEND_LOG_LEVEL:-info}"
export FUTU_OPEND_LISTEN_IP="${FUTU_OPEND_LISTEN_IP:-127.0.0.1}"

# Template FutuOpenD.xml with credentials from env vars.
envsubst < /opt/OpenD/FutuOpenD.xml.template > /opt/OpenD/FutuOpenD.xml

cd /opt/OpenD
exec ./FutuOpenD
