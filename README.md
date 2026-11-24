# TaskFlow

DSO202 (Scaling, Orchestration, Monitoring & Observability) final project.

TaskFlow is a todo platform split into **15 microservices** (14 Python/FastAPI
services and a React frontend) that runs on a three-node **kind** cluster. It
is packaged with **Helm** (library + umbrella chart), configured per
environment with **Kustomize**, delivered with **Argo CD** (GitOps, app of
apps, ApplicationSets) and **Jenkins** (CI), observed with **Prometheus,
Grafana, OpenTelemetry/Tempo and Loki**, and scaled on **custom metrics**.

- Proposal: [`docs/proposal/`](docs/proposal/)
- Architecture: [`docs/architecture.md`](docs/architecture.md)
- Capacity and HA plan: [`docs/capacity-plan.md`](docs/capacity-plan.md)
- Test report and evidence: [`docs/test-report.md`](docs/test-report.md), [`docs/evidence/`](docs/evidence/)

## The services

| # | Service | Responsibility | Storage / messaging |
|---|---|---|---|
| 1 | web-frontend | React single-page UI (nginx) | static |
| 2 | api-gateway | routing, JWT validation, rate limit (limit read live from etcd), etcd discovery, circuit breaker | Redis, etcd |
| 3 | auth-service | registration, login, JWT + rotating refresh tokens | PostgreSQL |
| 4 | user-service | profiles and preferences | PostgreSQL |
| 5 | list-service | lists, sharing, owner/editor/viewer roles | PostgreSQL, publishes events |
| 6 | task-service | task CRUD, status, priority, due dates | PostgreSQL, publishes events |
| 7 | tag-service | labels on tasks | PostgreSQL |
| 8 | comment-service | comments on tasks | PostgreSQL, publishes events |
| 9 | attachment-service | file upload/download | S3 object store (SeaweedFS) on a PV |
| 10 | reminder-service | manual and automatic due-date reminders | PostgreSQL, publishes events |
| 11 | recurrence-service | daily/weekly/monthly repeating tasks | PostgreSQL, consumes events |
| 12 | notification-service | delivers notifications (simulated provider) | consumes events, Redis |
| 13 | search-service | full-text search built from events | PostgreSQL (tsvector) |
| 14 | analytics-service | productivity statistics, business metrics | consumes events, Redis |
| 15 | audit-service | append-only audit log (trigger-enforced) | PostgreSQL, consumes events |

Shared infrastructure per environment (`charts/taskflow-platform`): PostgreSQL
(one database and role per service), Redis, NATS JetStream, a 3-member etcd
cluster (service registry + dynamic configuration) and the S3 object store,
all on PersistentVolumes.

Every Python service is built from one template, `services/common/taskflow_common`:
JSON logs with trace ids, RED metrics, OpenTelemetry tracing, liveness and
readiness probes, etcd registration with leases, PostgreSQL with retry, and
NATS JetStream publish/consume with trace propagation.

## Repository layout

```
services/                 the 15 services, one Dockerfile for the 14 Python ones, unit tests
charts/taskflow/          umbrella chart: 15 service charts + taskflow-lib library chart
charts/taskflow-platform/ PostgreSQL, Redis, NATS, etcd, object store
deploy/environments/      Kustomize: per-environment namespace, Pod Security, quota, RBAC, SealedSecrets
deploy/argocd/            Argo CD bootstrap, root app of apps, Applications and ApplicationSets
deploy/observability/     Grafana dashboards (generated) and Prometheus alert rules
deploy/platform-secrets/  SealedSecrets for the add-on namespaces
platform/ingress-nginx/   pinned ingress controller + kustomization
ci/  Jenkinsfile          CI toolbox image, agent Pod, the pipeline
cluster/  scripts/        kind cluster, bootstrap/sealing/testing/load scripts
docs/                     proposal, architecture, capacity plan, test report, evidence
```

## Running it

Prerequisites: Docker Desktop, kind, kubectl, Helm, kubeseal, Python 3, Git Bash.
Docker's VM is capped at 9 GB on this 16 GB laptop (`~/.wslconfig`).

```bash
export MSYS_NO_PATHCONV=1                                  # Git Bash only

# 1. cluster, local registry, honest node capacity
kind create cluster --config cluster/kind-cluster.yaml
bash scripts/setup-registry.sh
bash scripts/node-capacity.sh

# 2. images (15 services + CI toolbox)
bash scripts/build-images.sh
docker build -f ci/tools.Dockerfile -t localhost:5001/taskflow/ci-tools:0.1.0 . && \
  docker push localhost:5001/taskflow/ci-tools:0.1.0

# 3. Argo CD, then everything else from Git
kubectl apply -k deploy/argocd/bootstrap --server-side --force-conflicts
kubectl apply -f deploy/argocd/root/root-app.yaml

# 4. on a NEW cluster only: restore the sealing key so the SealedSecrets in Git decrypt
kubectl apply -f ~/.taskflow-keys/sealed-secrets-key.backup.yaml
kubectl -n sealed-secrets rollout restart deploy/sealed-secrets-controller

# 5. check
python scripts/smoke_test.py --base https://app.taskflow.localhost --insecure
```

| URL | What |
|---|---|
| https://app.taskflow.localhost | TaskFlow (prod-like) |
| https://dev.taskflow.localhost | TaskFlow dev (parked by default, see below) |
| https://grafana.taskflow.localhost | Grafana (admin password in `~/.taskflow-keys/grafana-admin`) |
| https://argocd.taskflow.localhost | Argo CD (`kubectl -n argocd get secret argocd-initial-admin-secret`) |
| https://jenkins.taskflow.localhost | Jenkins (after the GitHub token step below) |

The certificate is signed by the project's own CA (`~/.taskflow-keys/ca.crt`);
import it into the browser's trusted roots or accept the warning.

**Parking dev.** `global.paused: true` in `charts/taskflow/values-dev.yaml`
and `paused: true` in `charts/taskflow-platform/values-dev.yaml` scale the dev
environment to zero Pods with all data kept (about 1.6 GB freed). Set both to
`false` and push; Argo CD brings dev back.

**Stopping everything.** Quit Docker Desktop. The cluster, volumes and data
survive and come back when Docker starts again.

## How the requirements are met

| Final project requirement | Where |
|---|---|
| Microservices application (>= 3 services) | 15 services, REST + events (`services/`) |
| a. Helm charts | `charts/taskflow` (library + umbrella, 15 service charts), `charts/taskflow-platform`; Kustomize overlays in `deploy/environments` |
| b. GitOps with Argo CD | `deploy/argocd`: pinned bootstrap, root app of apps, 2 AppProjects, ApplicationSets for dev/prod, automated sync + prune + self-heal, SealedSecrets; Jenkins pipeline commits image tags (`Jenkinsfile`) |
| c. Monitoring and observability | `deploy/argocd/apps/20-observability.yaml`: Prometheus + Alertmanager + Grafana, OpenTelemetry Collector -> Tempo, Alloy -> Loki; logs link to traces |
| d. Auto-scaling on custom metrics | Prometheus Adapter: task-service on `http_requests_per_second` (Pods metric), notification-service on `taskflow_notification_backlog` (External metric); gateway on CPU |
| e. Dashboards | `deploy/observability/dashboards`: TaskFlow - Application, TaskFlow - Infrastructure (generated by `scripts/gen_dashboards.py`) |
| Ingress, storage, security | NGINX Ingress with TLS and edge rate limit; PVCs for every stateful component; Pod Security "restricted", default-deny NetworkPolicies, least-privilege RBAC, per-workload ServiceAccounts without tokens, ResourceQuotas |
| High availability and capacity plan | 2+ replicas, zone spread, PDBs, probes, 3-member etcd, eviction-before-OOM; [`docs/capacity-plan.md`](docs/capacity-plan.md) |

## Still to do (needs the team)

- **Jenkins**: create a GitHub personal access token (scope `public_repo`), save
  it to `~/.taskflow-keys/github-token`, run `bash scripts/seal-secrets.sh`,
  move `deploy/argocd/40-cicd.yaml.pending` to `deploy/argocd/apps/40-cicd.yaml`,
  commit and push. Argo CD installs Jenkins with the pipeline job already defined.
- **Screenshots**: trust `~/.taskflow-keys/ca.crt`, then capture Grafana, Argo CD
  and Jenkins for the report.
- Fill in the team member names and IDs in the proposal; prepare the presentation.

## Software versions

| Tool | Version |
|---|---|
| Docker Desktop | 29.4.0 (WSL 2, 9 GB cap) |
| kind / kubectl / Kubernetes | v0.32.0 / v1.36.3 / v1.36.1 |
| Helm / kubeseal | v4.3.1 / 0.40.0 |
| Argo CD | v3.6.1 |
| ingress-nginx | v1.15.1 |
| Sealed Secrets chart | 2.20.0 (controller 0.40.0) |
| kube-prometheus-stack / prometheus-adapter / metrics-server | 93.10.0 / 5.4.2 / 3.14.0 |
| Loki / Tempo / Alloy / OpenTelemetry Collector charts | 7.7.0 / 1.24.4 / 1.16.0 / 0.183.2 |
| Jenkins chart | 5.13.3 (Jenkins 2.580.2 LTS) |
| PostgreSQL / Redis / NATS / etcd / SeaweedFS | 17.2 / 7.4.1 / 2.10.22 / 3.5.17 / 3.97 |
| Python (services) / Node (build) | 3.12 / 20.18.1 |
