#!/usr/bin/env bash
# scripts/make-secrets.sh -- TaskFlow (DSO202 final project)
# Generates the credentials of one environment once, as plain files under
# ~/.taskflow-keys/<namespace>/ (never in the repository):
#
#   postgres-admin      PostgreSQL superuser password
#   db/<database>       one password per service database
#   jwt                 HMAC key shared by auth-service and api-gateway
#   s3-access, s3-secret  object-store credentials
#
#   bash scripts/make-secrets.sh taskflow-prod
#
# Existing files are kept, so running it again never rotates a password by
# accident. scripts/seal-secrets.sh turns these files into SealedSecrets
# for Git; APPLY=1 applies them directly as Secrets instead (bootstrap
# without Argo CD).
set -euo pipefail
NS="${1:?usage: make-secrets.sh <namespace>}"
KEYS="${TASKFLOW_KEYS:-$HOME/.taskflow-keys}/$NS"
DATABASES=(auth users lists tasks tags comments attachments reminders recurrence search audit)
mkdir -p "$KEYS/db"
chmod 700 "$KEYS" 2>/dev/null || true

# tr strips CR as well as LF: Git Bash's openssl ends its output with CRLF.
gen() { [ -f "$1" ] || openssl rand -hex "${2:-24}" | tr -d '\r\n' > "$1"; }
gen "$KEYS/postgres-admin"
for db in "${DATABASES[@]}"; do gen "$KEYS/db/$db"; done
gen "$KEYS/jwt" 48
[ -f "$KEYS/s3-access" ] || printf 'taskflow' > "$KEYS/s3-access"
gen "$KEYS/s3-secret"

# SeaweedFS identity file built from the two S3 values.
cat > "$KEYS/s3.json" <<EOF
{"identities":[{"name":"taskflow","credentials":[{"accessKey":"$(cat "$KEYS/s3-access")","secretKey":"$(cat "$KEYS/s3-secret")"}],"actions":["Read","Write","List","Tagging","Admin"]}]}
EOF
echo "credentials for $NS are in $KEYS" >&2

# kubectl.exe on Windows needs Windows paths (Git Bash keeps /c/... form
# when MSYS_NO_PATHCONV=1 is set).
K="$KEYS"
if command -v cygpath >/dev/null 2>&1; then K="$(cygpath -m "$KEYS")"; fi

# Renders the four Secrets as YAML on stdout (used by seal-secrets.sh).
render() {
  kubectl create secret generic taskflow-postgres-admin -n "$NS" \
    --from-file=password="$K/postgres-admin" --dry-run=client -o yaml
  echo "---"
  args=()
  for db in "${DATABASES[@]}"; do args+=(--from-file="$db=$K/db/$db"); done
  kubectl create secret generic taskflow-db -n "$NS" "${args[@]}" --dry-run=client -o yaml
  echo "---"
  kubectl create secret generic taskflow-jwt -n "$NS" \
    --from-file=secret="$K/jwt" --dry-run=client -o yaml
  echo "---"
  kubectl create secret generic taskflow-objectstore -n "$NS" \
    --from-file=accessKey="$K/s3-access" --from-file=secretKey="$K/s3-secret" \
    --from-file=s3.json="$K/s3.json" --dry-run=client -o yaml
  echo "---"
  kubectl create secret tls taskflow-tls -n "$NS" \
    --cert="$K/../tls.crt" --key="$K/../tls.key" --dry-run=client -o yaml
}

if [ "${RENDER:-0}" = "1" ]; then render; fi
if [ "${APPLY:-0}" = "1" ]; then
  kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  render | kubectl apply -f -
fi
