# Ghostfolio — display layer

Ghostfolio replaces the retired Flutter app for display, history and cost
basis. It does **not** own target allocation, drift or rebalancing — that is
the allocation engine (spec §8). See `PORTFOLIO_TRACKER_SPEC.md` §7, §11.

## What this stack is

| Service | Image | Published? |
|---|---|---|
| `ghostfolio` | `ghostfolio/ghostfolio:3.67.0` | one port, one interface |
| `postgres` | `postgres:15.19-alpine` | **no** |
| `redis` | `redis:7.4.11-alpine` | **no** |

Two networks:

- **`gf-backend`** — `internal: true`. Postgres and Redis live here and have
  no gateway, so they cannot reach or be reached from off-host at all.
- **`mbp-internal`** — external, shared with the aggregator stack so it can
  POST activities to `http://ghostfolio:3333` without either side publishing
  a host port.

Create the shared network once:

```bash
sudo docker network create mbp-internal
```

## First run

```bash
cp .env.example .env
chmod 600 .env
# fill in every <INSERT_...> with: openssl rand -hex 32
sudo docker-compose up -d
```

> The NAS has standalone **`docker-compose` v2.9.0** (Docker 20.10.23). There
> is no `docker compose` subcommand here — use the hyphenated form. Where the
> spec says `docker compose config`, run `docker-compose config`.

Then open `http://<GHOSTFOLIO_BIND_IP>:3333`, create the account, and keep
the generated security token somewhere safe — Ghostfolio has no password
reset.

## Exposure (§11.2)

`GHOSTFOLIO_BIND_IP` binds the UI to exactly one interface. It defaults to
`127.0.0.1`; the deployed value is the NAS LAN address.

**Never set it to `0.0.0.0`, and never add a router port-forward.** Ghostfolio
holds the complete financial picture. Reach it from outside the LAN through
the existing self-hosted VPN, or terminate TLS at the LAN reverse proxy and
leave the container port on the internal network.

Verify no service has grown a stray port mapping:

```bash
sudo docker-compose config | grep -A3 published
```

Only `ghostfolio` should appear, with `host_ip` set to a specific address.

## Backups (§11.4)

`scripts/pg_backup.sh` dumps Postgres and copies the result off the NAS.
`BACKUP_OFFSITE_DIR` is mandatory — the script refuses to run without it,
because a copy that stays on the same box is not a backup.

```bash
BACKUP_OFFSITE_DIR=/path/to/offsite ./scripts/pg_backup.sh
```

Schedule it daily and off-hours via Synology Task Scheduler.

**Restore, which must be tested at least once:**

```bash
gunzip -c ghostfolio-YYYYmmdd-HHMMSS.sql.gz \
  | sudo docker exec -i gf-postgres psql -U ghostfolio -d ghostfolio-db
```

An untested backup is a guess.

## Version pinning (§11.3)

Every image is pinned to an exact patch version. When bumping Ghostfolio,
take a backup first and read its release notes — it runs Prisma migrations
against the database on start, and those are not reversible.
