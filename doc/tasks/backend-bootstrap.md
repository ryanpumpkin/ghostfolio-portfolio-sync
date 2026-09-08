# backend-bootstrap

FastAPI application scaffold, auth middleware, ops endpoints, and project conventions for the backend proxy service.

## Subtasks

### Project scaffold

- [x] Create `backend/` directory at repo root
- [x] `pyproject.toml` with FastAPI, Uvicorn, Pydantic v2, httpx, firebase-admin, python-jose, cryptography, prometheus-client, structlog, pytest
- [x] Folder layout: `app/{api,adapters,services,workers,middleware,models,core}`
- [x] Settings via `pydantic-settings` reading from env (Firebase project ID, KMS provider, FX provider key, broker gateway hosts)

### App entry

- [x] `app/main.py` creating FastAPI app, mounting `/v1` router
- [x] CORS middleware (configurable origins)
- [x] Request ID middleware (correlation header in/out)
- [x] Structured JSON logging via `structlog`
- [x] Global exception handler producing a uniform error envelope

### Auth middleware (`app/middleware/auth.py`)

- [x] Initialize Firebase Admin SDK with service account from env
- [x] FastAPI dependency `current_user` verifying `Authorization: Bearer <id_token>`
- [x] Inject `user_id` into request context; deny if invalid/expired

### Domain models (`app/models/`)

- [x] Pydantic models mirroring Flutter domain entities (Position, Transaction, CashBalance, Quote, FxRate, Connection, PortfolioSnapshot)
- [x] Shared `SourceHealth` and `PartialResult[T]` types

### Ops endpoints

- [x] `GET /healthz` returning ok + uptime + version + adapter healths
- [x] `GET /metrics` for Prometheus

### Dev tooling

- [x] `Makefile` / `taskfile` with `run`, `test`, `lint`, `fmt`
- [x] `ruff` + `mypy` configured
- [x] `pytest` skeleton with one passing smoke test
