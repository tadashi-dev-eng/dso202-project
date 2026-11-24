#!/usr/bin/env bash
# scripts/setup-registry.sh -- TaskFlow (DSO202 final project)
# Local container registry for the kind cluster (pattern from the kind docs,
# "Local Registry").
#
#   * registry:2 runs as container "kind-registry", published on
#     127.0.0.1:5001 for pushes from the host.
#   * The container joins the "kind" Docker network, so nodes and Pods reach
#     it as kind-registry:5000 (Jenkins pushes there from inside the cluster).
#   * Each node gets /etc/containerd/certs.d/localhost:5001/hosts.toml, so an
#     image named localhost:5001/taskflow/<svc>:<tag> is pulled from the
#     registry container over the Docker network.
#   * The ConfigMap local-registry-hosting (KEP-1755) advertises it.
#
# Idempotent: safe to run again after the cluster is recreated.
set -euo pipefail

CLUSTER="${CLUSTER:-taskflow}"
REG_NAME="kind-registry"
REG_PORT="5001"

if [ "$(docker inspect -f '{{.State.Running}}' "$REG_NAME" 2>/dev/null || true)" != "true" ]; then
  docker rm -f "$REG_NAME" >/dev/null 2>&1 || true
  docker run -d --restart=always --name "$REG_NAME" \
    -p "127.0.0.1:${REG_PORT}:5000" \
    -v taskflow-registry-data:/var/lib/registry \
    registry:2 >/dev/null
  echo "registry started on 127.0.0.1:${REG_PORT}"
fi

for node in $(kind get nodes --name "$CLUSTER"); do
  docker exec "$node" mkdir -p "/etc/containerd/certs.d/localhost:${REG_PORT}"
  printf '[host."http://%s:5000"]\n' "$REG_NAME" |
    docker exec -i "$node" cp /dev/stdin "/etc/containerd/certs.d/localhost:${REG_PORT}/hosts.toml"
  # The same registry under its in-network name, used by in-cluster builds.
  docker exec "$node" mkdir -p "/etc/containerd/certs.d/${REG_NAME}:5000"
  printf '[host."http://%s:5000"]\n' "$REG_NAME" |
    docker exec -i "$node" cp /dev/stdin "/etc/containerd/certs.d/${REG_NAME}:5000/hosts.toml"
done

if [ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "$REG_NAME")" = "null" ]; then
  docker network connect kind "$REG_NAME"
fi

kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:${REG_PORT}"
    hostFromContainerRuntime: "${REG_NAME}:5000"
    hostFromClusterNetwork: "${REG_NAME}:5000"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
EOF
echo "registry wired into cluster ${CLUSTER}"
