#!/usr/bin/env bash
# Build (if needed), upload and (re)start the relay. Idempotent; touches only
# p2r-relay files, never derper or other services on the host.
set -euo pipefail
HOST="${HOST:-root@60.204.202.153}"
KEY="${KEY:-$HOME/.ssh/p2r_relay_ed25519}"
SECRETS="${SECRETS:-$HOME/.paper2run/relay-secrets}"
BUILD="${BUILD:-$HOME/.paper2run/relay-build}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes "$HOST")
SCP=(scp -q -i "$KEY" -o IdentitiesOnly=yes)

if [ ! -x "$BUILD/iroh-relay" ]; then
  mkdir -p "$BUILD"
  docker run --rm --platform linux/amd64 -v "$BUILD":/out -v p2r-cargo-reg:/usr/local/cargo/registry rust:1-bookworm \
    bash -c 'cargo install iroh-relay --version 1.4.1 --features server --locked --root /tmp/r && cp /tmp/r/bin/iroh-relay /out/'
fi
[ -f "$SECRETS/server.pem" ] || "$HERE/make-cert.sh"

"${SSH[@]}" 'id p2r-relay >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin p2r-relay; install -d -m 750 -o root -g p2r-relay /etc/p2r-relay'
"${SCP[@]}" "$BUILD/iroh-relay" "$HOST:/usr/local/bin/p2r-iroh-relay.new"
"${SCP[@]}" "$HERE/relay.toml" "$SECRETS/server.pem" "$SECRETS/server.key" "$HOST:/etc/p2r-relay/"
"${SCP[@]}" "$HERE/p2r-relay.service" "$HOST:/etc/systemd/system/p2r-relay.service"
"${SSH[@]}" 'set -e
  install -m 755 /usr/local/bin/p2r-iroh-relay.new /usr/local/bin/p2r-iroh-relay && rm /usr/local/bin/p2r-iroh-relay.new
  chown root:p2r-relay /etc/p2r-relay/*; chmod 640 /etc/p2r-relay/*
  systemctl daemon-reload && systemctl enable --now p2r-relay && systemctl restart p2r-relay
  sleep 2; systemctl is-active p2r-relay; ss -lntup | grep -E "12234|12235|derper|8443"'
