# Ghostfolio Portfolio Sync

Pulls holdings, trades and cash from four brokers into
[Ghostfolio](https://ghostfol.io), which is the display layer. There is
no custom UI — §10 of the spec is explicit that the primary interface is
a monthly email, not a dashboard you have to remember to open.

```
Futu ─┐
IBKR ─┼─ adapters ─→ reconcile ─→ Ghostfolio ─→ monthly email
LB   ─┤                  │
Ledger┘                  └─→ allocation engine ─→ "where does the next contribution go"
```

## What runs by itself

On the NAS, via `/etc/crontab`:

| when | what |
|---|---|
| `30 6 * * *` | `infra/sync-all.sh` — all sources; emails **only** on failure |
| `0 9 1 * *` | `tools.digest --send` — the monthly message |

06:30 HKT is deliberate: after the US close (04:00–05:00 HKT) and after
IBKR's T+1 Flex statement exists, before HK opens at 09:30.

## Running a job by hand

Every job takes its secrets on **stdin** — never argv, which is visible
in the host process list, and never the environment, which is visible in
`docker inspect`.

```bash
# where you are against target, and where new money should go
python -m tools.allocation --new-money 10000 < token

# time-weighted, money-weighted and simple returns
python -m tools.returns < token

# one source
python -m tools.ibkr_sync --push < secrets     # gf token, then flex token
```

Futu is the exception: it must go through `infra/futu-opend/sync-futu.sh`,
which starts OpenD, runs one job, and stops it again.

## Sources

| source | how | notes |
|---|---|---|
| IBKR | Flex Web Service | read-only token; cannot place an order |
| LongBridge | OpenAPI | token is trade-capable — read calls only |
| Futu | OpenD, ephemeral | separate crypto account; never calls `unlock_trade` |
| Binance | one-off import | account abandoned; coins moved to a Ledger wallet |

## Configuration

`config/` — hand-written by design, because no automatic classifier can
decide whether the Grayscale trust is "crypto" or "US equity" for a
particular owner.

* `targets.yaml` — asset class targets and tolerance bands
* `classification.yaml` — symbol → asset class
* `ghostfolio_symbols.yaml` — verified crypto symbol mappings
* `own_accounts.yaml` — **gitignored**; on-chain addresses are deanonymising

## Principles this codebase paid to learn

* **Refuse rather than guess.** An unresolvable symbol, an unclassified
  cash movement, a missing FX rate — reported and excluded, never
  invented.
* **Errors must not flatter.** A forgotten deposit understates
  contributions and *improves* the apparent return; a stale one only
  depresses it. When only one is avoidable, keep the row.
* **Never fabricate a trade.** Moving a holding between accounts is done
  by re-pushing the same activities, never sell-then-buy — that would
  realise a disposal that never happened and destroy the cost basis.
* **OpenD runs only during its sync window.** It holds a logged-in broker
  session; it is started, used, and stopped.
* **Compare counts of the same thing.** A guard that compared raw
  transactions against mapped activities let a run missing six trades
  through, and booked a phantom position.

## Other projects

rnpksync (YouTube watch-together) lived at this repo root until
2026-09-08 and now has its own repo:
[ryanpumpkin/rnpksync](https://github.com/ryanpumpkin/rnpksync).
