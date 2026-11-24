# TaskFlow

DSO202 (Scaling, Orchestration, Monitoring & Observability) final project:
a todo platform split into 15 microservices, run on a three-node kind
cluster and delivered with Helm, Jenkins and Argo CD, observed with
Prometheus, Grafana, OpenTelemetry and Loki, and scaled on custom metrics.

The proposal is in [`docs/proposal/`](docs/proposal/).

## Repository layout

```
.
├── cluster/                 kind cluster definition (1 control plane, 2 workers)
├── scripts/                 cluster bootstrap helpers (local registry, ...)
├── services/
│   ├── Dockerfile           one Dockerfile for all 14 Python services (ARG SERVICE)
│   ├── requirements.txt     one pinned dependency set, shared image layer
│   ├── common/              taskflow_common: the shared service template
│   └── <service>/app/       each service's own code
└── docs/                    proposal, architecture, test reports
```

## Bootstrap so far

```bash
export MSYS_NO_PATHCONV=1                       # Git Bash on Windows only
kind create cluster --config cluster/kind-cluster.yaml
bash scripts/setup-registry.sh                  # registry on localhost:5001
kubectl get nodes -L topology.kubernetes.io/zone
```

## Software versions

| Tool | Version |
|---|---|
| OS | Windows 11, Git Bash |
| Docker Desktop | 29.4.0 (WSL 2, 12 GB memory limit) |
| kind / kubectl | v0.32.0 / v1.36.3 |
| Kubernetes | v1.36.1 (`kindest/node`, pinned by digest) |
| Helm | v4.3.1 |
| Python (services) | 3.12 (`python:3.12-slim`) |
