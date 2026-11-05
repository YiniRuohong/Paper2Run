#!/usr/bin/env bash
# Build (if needed), upload and (re)start the pairing rendezvous next to the
# relay, then print its endpoint id for remote.json. Idempotent; touches only
# p2r-rendezvous files.
set -euo pipefail
HOST="${HOST:-root@60.204.202.153}"
KEY="${KEY:-$HOME/.ssh/p2r_relay_ed25519}"
BUILD="${BUILD:-$HOME/.paper2run/relay-build}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes "$HOST")
SCP=(scp -q -i "$KEY" -o IdentitiesOnly=yes)

if [ ! -x "$BUILD/p2r-tunnel-linux" ]; then
  docker run --rm --platform linux/amd64 -v "$HERE/..":/src:ro -v "$BUILD":/out -v p2r-cargo-reg:/usr/local/cargo/registry \
    -v p2r-tunnel-target:/target rust:1-bookworm \
    bash -c 'cd /src && CARGO_TARGET_DIR=/target cargo build --release --locked -p p2r-tunnel && cp /target/release/p2r-tunnel /out/p2r-tunnel-linux'
fi
"${SCP[@]}" "$BUILD/p2r-tunnel-linux" "$HOST:/usr/local/bin/p2r-tunnel.new"
"${SCP[@]}" "$HERE/relay-ca.pem" "$HOST:/etc/p2r-relay/ca.pem"
"${SCP[@]}" "$HERE/p2r-rendezvous.service" "$HOST:/etc/systemd/system/p2r-rendezvous.service"
"${SSH[@]}" 'set -e
  install -m 755 /usr/local/bin/p2r-tunnel.new /usr/local/bin/p2r-tunnel && rm /usr/local/bin/p2r-tunnel.new
  chown root:p2r-relay /etc/p2r-relay/ca.pem; chmod 644 /etc/p2r-relay/ca.pem
  install -d -m 700 -o p2r-relay -g p2r-relay /var/lib/p2r-rendezvous
  runuser -u p2r-relay -- /usr/local/bin/p2r-tunnel id --key /var/lib/p2r-rendezvous/key > /tmp/p2r-rendezvous.id
  systemctl daemon-reload && systemctl enable --now p2r-rendezvous && systemctl restart p2r-rendezvous
  sleep 2; systemctl is-active p2r-rendezvous p2r-relay
  echo "rendezvous_id=$(cat /tmp/p2r-rendezvous.id)"; rm /tmp/p2r-rendezvous.id
  ss -lntup | grep -E "derper|8443|12234" | awk "{print \$1, \$5, \$7}"'
