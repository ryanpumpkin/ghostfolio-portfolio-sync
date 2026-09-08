# Futu OpenD sidecar

Custom Docker image built on top of Futu's official Linux OpenD binary.

We build our own image because Futu does not publish an official OpenD
Docker image. The community mirror that used to be referenced
(`ghcr.io/futu-sg/futunng-opend`) is unmaintained.

## One-time setup: download the OpenD tarball

1. Go to https://www.futunn.com/download/openAPI
2. Select **OpenD for Linux (Ubuntu)** — pick the version matching your
   Futu app version.
3. Save the file as `infra/futu-opend/OpenD.tar.gz` (gitignored).

The tarball is large (~200 MB) and licensed to Futu, which is why we
download it manually rather than baking it into the repo.

## Build

```bash
docker compose build futu-opend
```

The build will fail with a clear error if `OpenD.tar.gz` is missing.

## Run

Set credentials in your `.env`:

```
FUTU_OPEND_LOGIN_ACCOUNT=<your futu account number>
FUTU_OPEND_LOGIN_PASSWORD_MD5=<md5 of your login password>
```

To compute the MD5:

```bash
echo -n "your-futu-login-password" | md5sum   # Linux
echo -n "your-futu-login-password" | md5      # macOS
```

Start it:

```bash
docker compose up futu-opend -d
docker compose logs futu-opend -f
```

You should see something like `OpenD API listening on 0.0.0.0:11111`.

## Verify

From the backend container:

```bash
docker compose exec backend python -c "
from futu import OpenQuoteContext
ctx = OpenQuoteContext(host='futu-opend', port=11111)
print(ctx.get_global_state())
ctx.close()
"
```

A successful response with `market_hk`, `market_us`, etc. means OpenD
is reachable and authenticated.


## RSA key setup (required for cross-network encrypted trade connections)

Futu OpenD refuses `unlock_trade` calls from cross-network clients (such as
a backend container on a Docker bridge network) unless RSA encryption is
enabled. You must generate a 2048-bit RSA private key and place it at
`infra/futu-opend/conn_key.pem` before building the image.

### 1. Generate the key

Run this once from the repo root:

```bash
openssl genrsa -out infra/futu-opend/conn_key.pem 2048
```

The file is gitignored — it will never be committed. Keep a secure backup.

### 2. Place the file

The key must be present at `infra/futu-opend/conn_key.pem` **before**
running `docker compose build futu-opend`. The Dockerfile COPYs it into
`/opt/OpenD/conn_key.pem` inside the image. The build fails loudly if the
file is missing (same pattern as `OpenD.tar.gz`).

### 3. Build and start

```bash
docker compose build futu-opend
docker compose up futu-opend -d
```

### 4. Verify

Check OpenD logs for RSA-related startup messages:

```bash
docker compose logs futu-opend | grep -i rsa
```

Then verify trade unlock no longer returns the cross-network error:

```bash
docker compose exec backend python -c "
from futu import OpenSecTradeContext
ctx = OpenSecTradeContext(host='futu-opend', port=11111, is_encrypt=True,
                          rsa_private_key='/secrets/futu_conn_key.pem')
print(ctx.unlock_trade(password_md5='<your_trade_pwd_md5>'))
ctx.close()
"
```

A response with `RET_OK` (or a password-wrong error, not a cross-network
security error) means encryption is working correctly.

## Production deployment

When deploying to a remote server:

1. SCP or `rsync` the `OpenD.tar.gz` to the server's
   `infra/futu-opend/` directory (don't commit it).
2. Set the env vars on the server's `.env`.
3. `docker compose up futu-opend -d --build`.

The `futu-state` named volume persists OpenD's local state (rate-limit
counters, market subscriptions) across restarts — keep it.
