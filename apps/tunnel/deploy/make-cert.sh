#!/usr/bin/env bash
# Generate a private CA and a relay server certificate with an IP SAN.
# Private keys stay in $SECRETS (never committed); the CA cert is copied next
# to this script as relay-ca.pem for clients to pin.
set -euo pipefail
IP="${1:-60.204.202.153}"
SECRETS="${SECRETS:-$HOME/.paper2run/relay-secrets}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$SECRETS"; chmod 700 "$SECRETS"; cd "$SECRETS"
if [ ! -f ca.key ]; then
  openssl ecparam -name prime256v1 -genkey -noout -out ca.key
  openssl req -x509 -new -key ca.key -sha256 -days 3650 -subj "/CN=Paper2Run Relay CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -out ca.pem
fi
openssl ecparam -name prime256v1 -genkey -noout -out server.key
openssl req -new -key server.key -subj "/CN=$IP" -out server.csr
cat > server.ext <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=IP:$IP
EXT
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 825 -sha256 -extfile server.ext -out server.pem
openssl pkcs8 -topk8 -nocrypt -in server.key -out server.key.pk8 && mv server.key.pk8 server.key
chmod 600 ca.key server.key
cp ca.pem "$HERE/relay-ca.pem"
openssl x509 -in server.pem -noout -ext subjectAltName
