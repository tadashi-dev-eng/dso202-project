#!/usr/bin/env bash
# scripts/build-images.sh -- TaskFlow (DSO202 final project)
# Builds the 15 service images and pushes them to the local registry that
# the kind nodes pull from (scripts/setup-registry.sh).
#
#   scripts/build-images.sh                 # all services, tag 0.1.0
#   TAG=0.1.1 scripts/build-images.sh task-service api-gateway
#
# The 14 Python services share services/Dockerfile (ARG SERVICE); the
# frontend has its own two-stage Dockerfile. Jenkins runs the same commands
# in CI (see Jenkinsfile).
set -euo pipefail
cd "$(dirname "$0")/.."

REGISTRY="${REGISTRY:-localhost:5001}"
TAG="${TAG:-0.1.0}"
PY_SERVICES=(api-gateway auth-service user-service list-service task-service tag-service
  comment-service attachment-service reminder-service recurrence-service
  notification-service search-service analytics-service audit-service)

targets=("$@")
[ ${#targets[@]} -eq 0 ] && targets=(web-frontend "${PY_SERVICES[@]}")

for svc in "${targets[@]}"; do
  image="${REGISTRY}/taskflow/${svc}:${TAG}"
  if [ "$svc" = "web-frontend" ]; then
    docker build -q -t "$image" services/web-frontend >/dev/null
  else
    docker build -q -f services/Dockerfile --build-arg SERVICE="$svc" \
      --build-arg VERSION="$TAG" -t "$image" services >/dev/null
  fi
  docker push -q "$image" >/dev/null
  echo "pushed $image"
done
