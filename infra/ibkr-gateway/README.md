# IBKR Gateway sidecar

Self-built Docker image — IB Gateway driven headlessly by IBC. Adapted
from the [gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker)
project (Apache-2.0) because IBKR retired the standalone offline
Gateway downloads — gnzsnz hosts a SHA256-verified archive of the
last working offline build on GitHub Releases, which the Dockerfile
fetches at build time.

## Trust model

  - **Dockerfile + scripts**: adapted from gnzsnz's repo (~200 LOC,
    auditable). All third-party downloads are SHA256-pinned.
  - **IB Gateway binary**: downloaded from gnzsnz's GitHub Releases
    mirror with `sha256sum --check`. Original installer signed by IBKR.
  - **IBC**: downloaded from the official IbcAlpha/IBC GitHub releases.
  - **Defense in depth**: IBC config sets `ReadOnlyApi=yes`, so the
    gateway will refuse any place_order even if the adapter tried.

## Setup

No manual downloads needed (unlike the futu-opend sidecar). Just set
credentials in `.env`:

```
IBKR_USERNAME=<your IB live username>
IBKR_PASSWORD=<your IB password>
IBKR_TRADING_MODE=live    # or paper
```

Then:

```bash
docker compose build ibkr-gateway
docker compose up ibkr-gateway -d
docker compose logs ibkr-gateway -f
```

## First boot

IBC drives the login dialog automatically. If your account uses 2FA,
you'll get a push notification on **IBKR Mobile** — approve it and the
gateway finishes coming up.

## Verify

From the backend container:

```bash
docker compose exec backend python -c "
from ib_insync import IB
ib = IB()
ib.connect('ibkr-gateway', 4001, clientId=999)
print('accounts =', ib.managedAccounts())
ib.disconnect()
"
```

A list of account ids → live API session is working.

## Daily auth

IBKR forces a session reset around 03:00 New York time. IBC handles
the restart and re-login automatically; if 2FA is required, you'll get
a fresh push on your phone for approval.
