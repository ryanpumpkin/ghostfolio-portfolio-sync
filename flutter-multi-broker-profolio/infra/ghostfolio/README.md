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

## API facts, verified against the running 3.67.0 instance

§7.1 says to confirm the import surface against the running instance rather
than trusting documentation. Doing that caught four things that would each
have failed at runtime. Re-run these checks after any version bump.

| Assumption | Reality on 3.67.0 |
|---|---|
| `GET /api/v1/order` lists activities | **404.** The module was renamed; it is `GET /api/v1/activities`. |
| `CreateAccountDto` takes `isExcluded` | **Rejected.** Not in the DTO, and unknown properties fail the whole request. |
| `platformId` is optional | **Required, but nullable.** Its validator is `@ValidateIf(value !== null)`, so explicit `null` passes and omitting the key fails. |
| Crypto uses Yahoo's `BTC-USD` | **404.** Ghostfolio normalises to `BTCUSD` (YAHOO) or `bitcoin` (COINGECKO). |

The authoritative route list is the instance's own boot log:

```bash
sudo docker logs ghostfolio 2>&1 | grep -oE 'Mapped \{[^}]+\}' | sort -u
```

To verify a symbol actually prices before trusting it (this is the check
that caught `BTC-USD`):

```
GET /api/v1/symbol/lookup?query=<name>     # what does Ghostfolio know?
GET /api/v1/symbol/<dataSource>/<symbol>   # does it return a real price?
```

Verified mappings are recorded in `config/ghostfolio_symbols.yaml`.

### Idempotency (§3.3)

Ghostfolio deduplicates on import, and its dedup **takes `comment` into
account**. The exporter writes the source-derived external id there, which
makes dedup effectively id-based. That matters in both directions, and both
were tested against the live instance:

- re-importing the same activities creates **no duplicates**; and
- a genuine second identical fill (same instrument, date, quantity and
  price, different source id) is **still kept** rather than silently
  swallowed.

Content-based dedup would have dropped that second fill and quietly
understated the position.

### Accounts

One Ghostfolio Account per source (§7.1), created with the currency of that
venue's primary cash — not the base currency. Each is one
`PUT /api/v1/account/:id` away from changing.

The default `My Account` that Ghostfolio creates at signup is left in place
and unused; delete it from the UI if you want it gone.
