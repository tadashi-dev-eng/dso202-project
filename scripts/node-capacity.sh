#!/usr/bin/env bash
# scripts/node-capacity.sh -- TaskFlow (DSO202 final project)
#
# Make the scheduler see the memory that really exists.
#
# The three kind "nodes" are containers sharing ONE Docker/WSL VM (9 GB on
# this laptop). Unpatched, each kubelet reports the whole VM as its own
# allocatable memory: Kubernetes believes it has 3 x 8.7 GiB and keeps
# scheduling until the kernel OOM-killer takes down a whole node. That is
# exactly what happened in the first load test (docs/evidence, phase 7).
#
# This script reserves memory on each node so that allocatable memory adds
# up to what pods can really use, and turns on memory-pressure eviction:
#
#   control plane   allocatable ~1.0 GiB  (DaemonSets + ingress controller;
#                                          the rest is its own API server/etcd)
#   each worker     allocatable ~2.9 GiB  (all workloads)
#   eviction        a node evicts Pods when the VM has < 400 MiB free,
#                   long before the kernel OOM-killer would act
#
# With honest capacity, an autoscaler that wants more Pods than fit gets
# Pending Pods (visible, recoverable) instead of a crashed node.
#
#   bash scripts/node-capacity.sh            # idempotent; run after cluster creation
set -euo pipefail
export MSYS_NO_PATHCONV=1
CLUSTER="${CLUSTER:-taskflow}"

patch_node() {
  local node="$1" reserved="$2"
  docker exec -i "$node" sh -s "$reserved" <<'EOF'
set -e
f=/var/lib/kubelet/config.yaml
# drop any earlier copy of our block, and kind's 0% eviction defaults
sed -i '/^# taskflow-capacity begin/,/^# taskflow-capacity end/d' "$f"
sed -i '/^evictionHard:/,/^[a-zA-Z]/{/^evictionHard:/d;/^  /d}' "$f"
cat >> "$f" <<YAML
# taskflow-capacity begin (scripts/node-capacity.sh)
systemReserved:
  memory: $1
evictionHard:
  memory.available: 400Mi
  nodefs.available: 5%
  imagefs.available: 5%
# taskflow-capacity end
YAML
systemctl restart kubelet
EOF
}

for node in $(kind get nodes --name "$CLUSTER"); do
  case "$node" in
    *control-plane) patch_node "$node" 7300Mi ;;
    *)              patch_node "$node" 5400Mi ;;
  esac
  echo "patched $node"
done

sleep 15
kubectl get nodes -o custom-columns='NODE:.metadata.name,CAPACITY:.status.capacity.memory,ALLOCATABLE:.status.allocatable.memory'
