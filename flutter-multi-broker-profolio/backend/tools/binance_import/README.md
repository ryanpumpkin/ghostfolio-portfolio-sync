# Binance one-off historical import (spec §5)

**Run this once, then revoke the key.** It is not a scheduled job and is
deliberately not wired into the scheduler (§5.10 item 1).

## Why it exists

§5.0: the owner is not continuing to use Binance — Futu is the ongoing
crypto venue (§4.4). This recovers the **cost basis** of coins already
bought here, so the BTC/ETH/DOGE now on the Ledger are not orphaned.

A coin bought here and withdrawn to the Ledger is **one BUY followed by a
custody change** — never a sale (§6.3). That is the single most important
rule in the whole spec and it has its own tests.

## What it deliberately does NOT do

Per §5.0, none of this is built, because there is no "next time":

| Not built | Why |
|---|---|
| Scheduled sync | Runs once, manually |
| Incremental sync via `fromId` cursors | There is no next run |
| Negative cache for empty symbols | Same |
| Earn position tracking | Only matters for live balances (§5.8) |
| Rate-limit tuning for sustained load | A single run, run slowly |

What *does* matter is **completeness of the historical record**. A missed
trade is a wrong cost basis forever. The crawl is exhaustive rather than
clever, and it is slow on purpose.

## Before running

1. Create a **read-only** API key: *Enable Reading* only. Spot trading and
   withdrawals OFF (§5.2).
2. Bind it to the server IP. Note that a key **without** an IP whitelist
   expires after 90 days.
3. Set the credentials through the existing encrypted path (§3.5) — not a
   committed `.env`.

## Endpoints used (§5.4)

Verify every path against current Binance docs before running: they
rename and deprecate `sapi` endpoints regularly, and the spec's table is
"a starting point, not a contract".

The three that catch people out:

- **`myTrades` requires a `symbol`** and there are thousands. Candidates
  are derived from assets ever held (§5.5), never brute-forced.
- **Convert trades do NOT appear in `myTrades`** — `sapi/v1/convert/tradeFlow`,
  30-day windows.
- **Dust conversions do not either** — `sapi/v1/asset/dribblet`.

Reading only `myTrades` silently loses both.

## Raw archive

Every response is written to `data/binance_raw/` (gitignored) **before**
normalisation. §5.5: if the normaliser has a bug, fixing it must not
require re-hitting an API whose key has since been revoked.

## After a successful run

Check §5.11's acceptance criteria, then:

- Revoke the Binance API key.
- Delete the credential from the encrypted store.
- Leave this code in place, marked one-off, in case a re-run is needed.
