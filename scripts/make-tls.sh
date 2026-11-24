#!/usr/bin/env bash
# scripts/make-tls.sh -- TaskFlow (DSO202 final project)
# A private certificate authority and one wildcard certificate for
# *.taskflow.localhost (app, dev, grafana, argocd, jenkins, ...).
#
#   bash scripts/make-tls.sh            # creates the files once
#
# Files go to ~/.taskflow-keys (outside the repository and outside the
# OneDrive-synced Desktop). The CA certificate can be imported into the
# browser's trusted roots so the pages show no warning; the CA key never
# leaves that folder. The TLS Secret itself reaches the cluster sealed
# (scripts/seal-secrets.sh), never as plaintext in Git.
set -euo pipefail
KEYS="${TASKFLOW_KEYS:-$HOME/.taskflow-keys}"
mkdir -p "$KEYS"
chmod 700 "$KEYS" 2>/dev/null || true
cd "$KEYS"

if [ ! -f ca.key ]; then
  openssl genrsa -out ca.key 4096 2>/dev/null
  openssl req -x509 -new -key ca.key -sha256 -days 825 -out ca.crt \
    -subj "/CN=TaskFlow DSO202 Local CA/O=TaskFlow"
  echo "created CA: $KEYS/ca.crt"
fi

if [ ! -f tls.key ]; then
  openssl genrsa -out tls.key 2048 2>/dev/null
  openssl req -new -key tls.key -out tls.csr -subj "/CN=*.taskflow.localhost/O=TaskFlow"
  cat > tls.ext <<'EOF'
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:*.taskflow.localhost, DNS:taskflow.localhost
EOF
  openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out tls.crt -days 365 -sha256 -extfile tls.ext 2>/dev/null
  rm -f tls.csr tls.ext
  echo "created certificate: $KEYS/tls.crt"
fi
openssl x509 -in tls.crt -noout -subject -ext subjectAltName -enddate
