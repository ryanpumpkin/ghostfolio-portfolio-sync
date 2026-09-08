# Multi-Broker Portfolio — Backend

FastAPI proxy service for broker / exchange APIs. See [doc/detailed-design.md §4](../doc/detailed-design.md).

## Quickstart

```bash
cd backend
make install            # creates .venv and installs deps + dev tools
cp .env.example .env    # fill in Firebase / FX / broker gateway values
make run                # uvicorn on :8000
```

## Common tasks

| Command | What it does |
|---|---|
| `make install` | `python3 -m venv .venv && pip install -e ".[dev]"` |
| `make run` | Start uvicorn with reload on `:8000` |
| `make test` | Run pytest |
| `make cov` | Pytest + coverage gate (>= 80%) |
| `make lint` | `ruff check .` |
| `make fmt` | `ruff format .` and `ruff check --fix .` |
| `make typecheck` | `mypy --strict app` |
| `make all` | lint + typecheck + cov |

If `make` is unavailable, the raw commands are:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest --cov=app --cov-fail-under=80
ruff check .
mypy --strict app
```

## Layout

```
backend/
  app/
    api/            REST + WS routers
    adapters/       (filled by backend-adapters)
    services/       (filled by backend-aggregator-and-fx, backend-vault)
    workers/        (filled by backend-alert-worker)
    middleware/     auth, request-id, access-log
    models/         shared Pydantic domain models + error envelope
    core/           settings, logging, tracing
    main.py         create_app() + module-level FastAPI instance
  tests/
  pyproject.toml
  Makefile
```

## Ops endpoints

- `GET /healthz` — `{status, version, uptime_seconds, adapters[]}`
- `GET /metrics` — Prometheus exposition

## Auth

Every `/v1` route depends on `current_user`, which verifies the Firebase ID
token in `Authorization: Bearer <token>`. The verifier is dependency-injected
(`get_token_verifier`), so tests can substitute a fake. Set
`MBP_AUTH_DISABLED=true` to bypass auth in local dev (do not enable in prod).

## Tracing

`RequestIdMiddleware` reads `X-Request-ID` (or generates one), stores it in a
contextvar, and echoes it on the response. All structured log lines include
`request_id` automatically.
