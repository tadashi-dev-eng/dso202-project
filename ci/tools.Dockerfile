# ci/tools.Dockerfile -- TaskFlow CI toolbox (the pipeline's main container)
#
#   docker build -f ci/tools.Dockerfile -t localhost:5001/taskflow/ci-tools:0.1.0 .
#
# Python 3.12 with every service dependency and pytest pre-installed (unit
# tests run without reaching PyPI), plus git, helm and kubectl for the
# lint, GitOps-commit and rollout-wait stages.
FROM alpine/helm:3.16.3 AS helm
FROM registry.k8s.io/kubectl:v1.32.0 AS kubectl

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=helm /usr/bin/helm /usr/local/bin/helm
COPY --from=kubectl /bin/kubectl /usr/local/bin/kubectl
COPY services/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt pytest==8.3.4
# Jenkins runs the container as UID 1000; git must trust the workspace.
RUN git config --system --add safe.directory '*'
