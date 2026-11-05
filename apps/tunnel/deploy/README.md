# Paper2Run relay (60.204.202.153:12234)

Self-hosted iroh-relay 1.4.1. Only port 12234 is used: HTTPS relay on TCP,
QUIC address discovery on UDP. The mandatory plain-HTTP listener is on
`127.0.0.1:12235` (not exposed). Other services on the host (`derper` on
80/443/3478, `python3` on 8443) are not touched.

## Files

| Local | Server |
|---|---|
| `relay.toml` | `/etc/p2r-relay/relay.toml` |
| `p2r-relay.service` | `/etc/systemd/system/p2r-relay.service` |
| `~/.paper2run/relay-secrets/server.{pem,key}` | `/etc/p2r-relay/server.{pem,key}` (root:p2r-relay 640) |
| `~/.paper2run/relay-build/iroh-relay` | `/usr/local/bin/p2r-iroh-relay` |
| `relay-ca.pem` (committed, public) | — clients pin this CA |

CA and server private keys live only in `~/.paper2run/relay-secrets/` (0600) and on the server.

## Deploy / redeploy

    ./deploy.sh            # builds in Docker if needed, uploads, restarts

## Verify

    curl --cacert relay-ca.pem -sS -o /dev/null -w '%{http_code}\n' https://60.204.202.153:12234/ping   # 200
    cd ../p2r-tunnel && P2R_RELAY_URL=https://60.204.202.153:12234 P2R_RELAY_CA=../deploy/relay-ca.pem \
      cargo test --test relay_only -- --ignored real_relay_roundtrip

## Roll back

    ssh -i ~/.ssh/p2r_relay_ed25519 -o IdentitiesOnly=yes root@60.204.202.153 \
      'systemctl disable --now p2r-relay; rm -f /etc/systemd/system/p2r-relay.service /usr/local/bin/p2r-iroh-relay; rm -rf /etc/p2r-relay; systemctl daemon-reload'

## Known limitation

Access is `everyone`: any iroh endpoint that knows the URL and CA can relay
through this host. Plan 2 should restrict it (iroh-relay supports allowlist /
shared_token / http access modes).
