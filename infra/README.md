# Infra — Local Development & Deployment Guide

## Contents

1. [Quick start](#quick-start)
2. [Required environment variables](#required-environment-variables)
3. [Service architecture](#service-architecture)
4. [IBKR sidecar credentials](#ibkr-sidecar-credentials)
5. [Futu OpenD sidecar credentials](#futu-opend-sidecar-credentials)
6. [Firebase Local Emulator Suite](#firebase-local-emulator-suite)
7. [Running the smoke test](#running-the-smoke-test)
8. [Production notes](#production-notes)

---

## Quick start

```bash
# 1. Clone / enter the repo
cd /path/to/ghostfolio-portfolio-sync

# 2. Create your local env file
cp .env.example .env
$EDITOR .env   # fill in credentials

# 3. Start the full stack (production compose)
docker compose up -d --build

# 4. Verify the backend is healthy
curl http://localhost:8000/healthz

# 5. Tear down
docker compose down
```

### Local development (with hot-reload and emulators)

`docker-compose.override.yml` is applied automatically by `docker compose up`.
It mounts the backend source tree for hot-reload, disables auth, and starts the
Firebase emulator suite.

```bash
docker compose up -d --build
# Backend hot-reloads on file save.
# Firebase Emulator Hub: http://localhost:4000
```

---

## Required environment variables

Copy `.env.example` to `.env` and fill in the values listed below.
Variables marked **optional** have working defaults.

| Variable | Required | Default | Description |
|---|---|---|---|
| `MBP_ENV` | optional | `production` | Runtime environment (`development` / `production`) |
| `MBP_LOG_LEVEL` | optional | `INFO` | Log level (`DEBUG` / `INFO` / `WARNING` / `ERROR`) |
| `MBP_AUTH_DISABLED` | optional | `false` | Skip Firebase ID-token verification (dev only) |
| `MBP_FIREBASE_PROJECT_ID` | **yes** | — | Firebase project ID |
| `MBP_FIREBASE_CREDENTIALS_PATH` | optional | ADC | Path inside container to service-account JSON |
| `MBP_FX_PROVIDER` | optional | `exchangerate.host` | FX rate provider |
| `MBP_FX_PROVIDER_API_KEY` | optional | — | API key for paid FX provider tiers |
| `MBP_KMS_PROVIDER` | optional | — | KMS provider (`gcp` / `aws` / blank to disable) |
| `MBP_KMS_KEY_ID` | optional | — | KMS key identifier |
| `MBP_IB_GATEWAY_HOST` | optional | `ibkr-gateway` | Hostname of IBKR gateway sidecar |
| `MBP_IB_GATEWAY_PORT` | optional | `5000` | Port of IBKR gateway sidecar |
| `MBP_FUTU_OPEND_HOST` | optional | `futu-opend` | Hostname of Futu OpenD sidecar |
| `MBP_FUTU_OPEND_PORT` | optional | `11111` | Port of Futu OpenD sidecar |
| `IBKR_USERNAME` | for IBKR | — | IBKR account username |
| `IBKR_PASSWORD` | for IBKR | — | IBKR account password |
| `IBKR_TRADING_MODE` | optional | `paper` | `paper` or `live` |
| `FUTU_OPEND_LOGIN_ACCOUNT` | for Futu | — | Futu/moomoo account number |
| `FUTU_OPEND_LOGIN_PASSWORD_MD5` | for Futu | — | MD5 of Futu account password |
| `FUTU_OPEND_TRADE_RSA_FILE` | optional | — | Path to RSA key file for Futu trading |

---

## Service architecture

```
┌─────────────────────────────────────┐
│  mbp-net (bridge network)           │
│                                     │
│  backend:8000  ──►  ibkr-gateway:5000  │
│       │         └►  futu-opend:11111   │
│       │         └►  firebase-emulator (dev only) │
│                                     │
└─────────────────────────────────────┘
         │
         └──► host port 8000 (published)
```

- `ibkr-gateway` and `futu-opend` are only accessible within `mbp-net`; their
  ports are not published to the host.
- In production the backend talks to real IBKR and Futu endpoints via the
  sidecars. In dev (`override.yml`) the adapters are set to `disabled`.

---

## IBKR sidecar credentials

The `ibkr-gateway` service uses the
[`ghcr.io/unusualwhale/ibkr-cpapi`](https://github.com/unusualwhale/ibkr-cpapi)
image, which wraps the Interactive Brokers Client Portal Gateway.

### Setup

1. Ensure you have an active IBKR account (paper or live).
2. Set in your `.env`:
   ```
   IBKR_USERNAME=your_ibkr_username
   IBKR_PASSWORD=your_ibkr_password
   IBKR_TRADING_MODE=paper      # or: live
   ```
3. The container writes session state to the `ibkr-state` volume so you
   survive restarts without a full re-login.

### Session keep-alive

The Client Portal Gateway session expires after ~24 hours and requires
periodic re-authentication. The `ibkr-cpapi` image handles automatic
keep-alive. If the session expires, restart the container:

```bash
docker compose restart ibkr-gateway
```

### Security note

Never use `IBKR_TRADING_MODE=live` unless this stack is running on a
private, firewalled host. The gateway only listens inside `mbp-net`.

---

## Futu OpenD sidecar credentials

The `futu-opend` service uses the
[`ghcr.io/futu-sg/futunng-opend`](https://github.com/futu-sg/futunng-opend)
image (official Futu OpenD for Docker).

### Manual pull (if the GHCR image is unavailable)

```bash
# Pull the official image from Docker Hub alternative or build from source:
docker pull futusg/futunng-opend:latest
# Then update docker-compose.yml image: field accordingly.
```

### Setup

1. Ensure you have a Futu / moomoo account with OpenAPI access enabled.
2. Generate the MD5 of your login password (Futu's requirement):
   ```bash
   echo -n "your_password" | md5sum | awk '{print $1}'
   ```
3. Set in your `.env`:
   ```
   FUTU_OPEND_LOGIN_ACCOUNT=your_account_number
   FUTU_OPEND_LOGIN_PASSWORD_MD5=<md5_from_above>
   FUTU_OPEND_TRADE_RSA_FILE=         # optional: path to RSA key for trading
   ```
4. The `futu-state` volume persists OpenD login state across restarts.

---

## Firebase Local Emulator Suite

### Via `docker-compose.override.yml` (recommended for dev)

The override file starts a `firebase-emulator` service automatically:

```bash
docker compose up -d   # override is applied automatically
# Emulator Hub: http://localhost:4000
```

### Standalone (CI or isolated testing)

```bash
docker compose -f infra/firebase-emulator/docker-compose.firebase.yml up -d
```

### Emulator endpoints

| Emulator | Port | SDK env var |
|---|---|---|
| Auth | 9099 | `FIREBASE_AUTH_EMULATOR_HOST` |
| Firestore | 8080 | `FIRESTORE_EMULATOR_HOST` |
| Storage | 9199 | `FIREBASE_STORAGE_EMULATOR_HOST` |
| Hub / UI | 4000 | — |

The backend service automatically sets these vars when started via the
override compose file. For standalone backends set them in `.env`.

---

## Running the smoke test

### Shell script (preferred, no extra dependencies)

```bash
# Start the stack, wait for the backend, then run the smoke test:
docker compose up -d && sleep 5 && ./infra/healthcheck/smoke_test.sh

# Custom backend URL:
BACKEND_URL=http://localhost:8000 ./infra/healthcheck/smoke_test.sh
```

The script retries up to 10 times (3 seconds apart) until the backend
responds, then asserts:
- HTTP 200
- `"status": "ok"` in the JSON body
- `"version"` field present

### Pytest alternative

```bash
# Install httpx (needed for the pytest smoke test only):
pip install httpx pytest

# Run:
docker compose up -d && sleep 10
pytest infra/tests/smoke_test.py -v
```

### Tear down after testing

```bash
docker compose down
```

---

## Production notes

- **Never publish** `ibkr-gateway` or `futu-opend` ports to the public internet.
  They listen on `mbp-net` only.
- Use a reverse proxy (nginx, Caddy, or a cloud load balancer) in front of
  `backend:8000` for TLS termination and rate-limiting.
- For cloud deployments consider replacing the `docker-compose.yml` sidecars
  with a single-host deployment on a VPS, or adapt the compose file to a
  cloud-native equivalent (AWS ECS task, GCP Cloud Run side-cars, etc.).
- Set `IBKR_TRADING_MODE=paper` until you have validated that all broker
  adapters behave correctly end-to-end in production.
