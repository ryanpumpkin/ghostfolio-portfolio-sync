# Futu OpenD sidecar — runs the official Linux command-line binary headless.
#
# Futu does not publish an OpenD Docker image. You must download the
# Linux tarball from https://www.futunn.com/download/openAPI and place
# it at `infra/futu-opend/OpenD.tar.gz` before building.
#
# The tarball ships two flavours:
#   Futu_OpenD-GUI_*/ — desktop GUI (AppImage) — we don't use this
#   Futu_OpenD_*/     — command-line, headless — what we extract
#
# Credentials are NOT baked into the image — they come from env vars
# templated into FutuOpenD.xml by entrypoint.sh at runtime.

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        libssl-dev \
        gettext-base \
        telnet \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/OpenD

# Extract ONLY the command-line subfolder. The tarball structure is:
#   Futu_OpenD_<ver>_Ubuntu<rel>/
#     Futu_OpenD-GUI_<ver>_Ubuntu<rel>/...   (skip)
#     Futu_OpenD_<ver>_Ubuntu<rel>/...       (keep — the CLI binary)
# We use a wildcard for the inner dir name so this Dockerfile doesn't
# have to be updated every OpenD version.
COPY OpenD.tar.gz /tmp/OpenD.tar.gz
RUN mkdir -p /tmp/opend-extract \
 && tar -xzf /tmp/OpenD.tar.gz -C /tmp/opend-extract \
 && BIN=$(find /tmp/opend-extract -type f -name 'FutuOpenD' | head -1) \
 && if [ -z "$BIN" ]; then \
        echo "Could not find FutuOpenD binary in tarball" >&2; \
        find /tmp/opend-extract -maxdepth 3; \
        exit 1; \
    fi \
 && CLI_DIR=$(dirname "$BIN") \
 && cp -a "$CLI_DIR"/. /opt/OpenD/ \
 && rm -rf /tmp/opend-extract /tmp/OpenD.tar.gz \
 && chmod +x /opt/OpenD/FutuOpenD

# Bundled .so libraries live in /opt/OpenD — make them discoverable.
ENV LD_LIBRARY_PATH=/opt/OpenD

COPY FutuOpenD.xml.template /opt/OpenD/FutuOpenD.xml.template
# RSA private key for encrypted trade connections (gitignored; place at
# infra/futu-opend/conn_key.pem before building — see README.md).
COPY conn_key.pem /opt/OpenD/conn_key.pem
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 11111

ENTRYPOINT ["/entrypoint.sh"]
