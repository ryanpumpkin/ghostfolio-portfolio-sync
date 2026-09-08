# Task: infra-deployment

Docker Compose stack with IBKR/Futu sidecars, Firebase emulator, healthcheck,
and smoke test.

## Subtasks

- [x] `backend/Dockerfile` — multi-stage Python 3.11-slim image, non-root user, `/healthz` HEALTHCHECK
- [x] `docker-compose.yml` — backend, ibkr-gateway, futu-opend services on `mbp-net`; volumes for gateway state; ports for gateway exposed only to `backend`
- [x] `.env.example` — sample env file with all required variables documented
- [x] `docker-compose.override.yml` — dev overrides: hot-reload, adapters=disabled, Firebase emulator sidecar
- [x] `infra/firebase-emulator/docker-compose.firebase.yml` — standalone Firebase emulator compose snippet
- [x] `infra/healthcheck/smoke_test.sh` — shell smoke test: start compose, hit `/healthz`, assert 200, tear down
- [x] `infra/tests/smoke_test.py` — pytest alternative smoke test
- [x] `infra/README.md` — local run instructions, env vars table, smoke test docs, IBKR/Futu credential notes
- [x] `docker compose config` validates without errors

## Quality gates

- `docker compose config` — PASS (validated locally)
- Smoke test script — syntactically valid, documented, executable
- All YAML config structurally correct

## Notes

- IBKR sidecar image: `ghcr.io/unusualwhale/ibkr-cpapi:latest`
- Futu sidecar image: `ghcr.io/futu-sg/futunng-opend:latest` (manual pull fallback documented)
- Gateway ports are NOT published to host — only accessible within `mbp-net`
- Dev override disables real broker connections via `IBKR_ADAPTER=disabled` / `FUTU_ADAPTER=disabled`
