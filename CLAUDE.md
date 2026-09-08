# Multi-Broker Portfolio Tracker

Aggregates holdings from four brokers into Ghostfolio, which is the
display layer. There is no custom UI: §10 of the spec is explicit that
the primary interface is a monthly email, not a dashboard.

## Layout

```
  backend/
    app/adapters/     futu, ibkr (Flex), longbridge, binance
    app/services/     ghostfolio/, allocation, returns, cashflows, fx, splits
    tools/            one CLI per job — see below
  config/             targets.yaml, classification.yaml, ghostfolio_symbols.yaml
  infra/              sync-all.sh, rotate-logs.sh, futu-opend/
```

## Jobs

Every job takes its secrets on **stdin**, never argv (visible in the host
process list) and never the environment (visible in `docker inspect`).

| command | what |
|---|---|
| `tools.futu_sync` / `ibkr_sync` / `longbridge_sync` | one source into Ghostfolio |
| `tools.binance_import` | one-off history recovery; account is abandoned |
| `tools.allocation` | drift vs target, and where new money goes |
| `tools.returns` | time-weighted, money-weighted, simple |
| `tools.digest` | the monthly email (§10) |
| `tools.split_check` | duplicate and share-split detection |

## Scheduled (NAS, /etc/crontab)

```
30 6 * * *   infra/sync-all.sh          all sources, then email ONLY on failure
0  9 1 * *   tools.digest --send        monthly
```

## Rules that were learned the hard way

* **OpenD runs only during the Futu sync window** (§4.3 rule 3). It holds a
  logged-in broker session. `sync-futu.sh` starts it, runs one job, stops
  it, and refuses to restart within 30 minutes — Futu throttles repeated
  logins and recovery takes hours.
* **Never fabricate a trade.** A transfer between your own accounts is not
  a sale (§6.3). Moving a holding between Ghostfolio accounts is done by
  re-pushing the same activities, never by sell-then-buy.
* **Refuse rather than guess.** An unresolvable symbol, an unclassified
  cash movement, a missing FX rate — each is reported and excluded, and
  where excluding it would flatter a return, the figure is withheld
  entirely.
* **Errors must not flatter.** A forgotten deposit understates
  contributions and improves the apparent return; a stale one only
  depresses it. When only one can be avoided, keep the row.
* **Check counts of the same thing.** The completeness guard once compared
  raw transactions against mapped activities and let a run missing six
  trades through, which booked a whole phantom position.

## Other project

rnpksync (YouTube watch-together) used to live at this repo root. Removed
2026-09-08; the source is at `/volume1/docker/rnpksync/src` on the NAS and
in this repo's history before that date. The running container uses a
prebuilt image and is unaffected.
