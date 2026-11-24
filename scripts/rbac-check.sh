#!/usr/bin/env bash
# scripts/rbac-check.sh -- prints the RBAC allow/deny matrix for both
# environments with `kubectl auth can-i` (impersonating each identity).
set -uo pipefail
D=(--as=jane --as-group=taskflow-developers)
V=(--as=vic --as-group=taskflow-viewers)
row() { printf '%-34s %s\n' "$1" "$(kubectl auth can-i "${@:2}" 2>/dev/null)"; }
for ns in taskflow-dev taskflow-prod; do
  echo "== $ns"
  row "developer: list pods"            list pods -n "$ns" "${D[@]}"
  row "developer: exec into pods"       create pods --subresource=exec -n "$ns" "${D[@]}"
  row "developer: delete (restart) pods" delete pods -n "$ns" "${D[@]}"
  row "developer: read secrets"         get secrets -n "$ns" "${D[@]}"
  row "developer: update deployments"   update deployments -n "$ns" "${D[@]}"
  row "viewer: list deployments"        list deployments -n "$ns" "${V[@]}"
  row "viewer: delete pods"             delete pods -n "$ns" "${V[@]}"
  row "ci-smoke-test: get deployments"  get deployments -n "$ns" --as="system:serviceaccount:$ns:ci-smoke-test"
  row "ci-smoke-test: get secrets"      get secrets -n "$ns" --as="system:serviceaccount:$ns:ci-smoke-test"
  row "task-service SA: list pods"      list pods -n "$ns" --as="system:serviceaccount:$ns:task-service"
done
