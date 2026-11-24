# Test report

Every result below was produced on the project cluster and the raw output
is committed in [`evidence/`](evidence/). Screenshots of Grafana, Argo CD and
Jenkins are still to be added (they need the project CA trusted in the
browser; see the README).

## 1. Functional

| Test | Tool | Result | Evidence |
|---|---|---|---|
| Unit tests (template, probes, metric cardinality, auth header, recurrence dates, audit mapping, gateway routes) | `pytest services/tests` | 11 / 11 passed | run in CI (Jenkinsfile, Test stage) |
| End-to-end: every one of the 15 services, including the asynchronous results (search index, notifications, statistics, audit log, recurrence) | `scripts/smoke_test.py` | 26 / 26 on Compose, dev and prod over HTTPS | `phase3-compose-smoke-test.txt`, `phase4-helm-dev-deploy.txt`, `phase4-prod-deploy.txt` |
| Audit log cannot be changed, even by its own role | `UPDATE audit_log` in psql | rejected by trigger | phase 3 (Compose) |
| Rate limit changed at runtime through etcd | `etcdctl put .../rate_limit_per_minute 5` | 429 after 5 requests, no restart | phase 2 (Compose) |
| Gateway circuit breaker | requests to a missing service | 503 in 0.2 s instead of a 2-4 s timeout; other routes unaffected | phase 2 |

## 2. Security

| Test | Result | Evidence |
|---|---|---|
| RBAC matrix (`kubectl auth can-i` as developer, viewer, CI, workload SA) | dev: developers may exec/restart, prod: read-only; nobody reads Secrets; workload SAs have no API access | `phase4-rbac-can-i.txt` |
| Pod Security "restricted" enforced on both environments | every Pod admitted (server dry-run showed no violation) | phase 4 |
| No plaintext credentials in Git | 30 credential values searched across the repository: 0 hits; 0 private keys | `phase5-no-plaintext-secrets.txt` |
| Anonymous access | 401 on every API except `/api/auth/*` | smoke test |
| Cross-user access | another user's list returns 404 | smoke test |
| NetworkPolicies (default deny + per-service allow) | all 15 services work with policies enforced; the platform policy admits only TaskFlow Pods and Prometheus | smoke test on dev/prod |

## 3. GitOps

| Test | Result | Evidence |
|---|---|---|
| Manual drift (`kubectl scale` to 1) | reverted by self-heal in 3 s | `phase5-gitops-selfheal-and-revert.txt` |
| Change through Git (tag-service 2 -> 3) | 3 replicas ready 12 s after the push | same |
| Rollback with `git revert` | back to 2 replicas 12 s after the push; both revisions in Argo CD's sync history | same |
| Whole platform from Git | 20 Applications Synced/Healthy from one `kubectl apply` of the root app | `kubectl get applications -n argocd` |

## 4. Autoscaling and load

| Test | Result | Evidence |
|---|---|---|
| Load test 1 (before the capacity fix) | worker nodes OOM-killed; led to the capacity fix | `phase7-loadtest-output.txt`, `phase7-hpa-during-loadtest.txt` |
| Load test 2 (8 users, 6 min) | 45 req/s, p95 < 200 ms, errors only edge 503s, no OOM | `phase7-loadtest-2-output.txt`, `phase7-hpa-loadtest-2.txt` |
| task-service on custom metric | 2 -> 5 on `http_requests_per_second` | `phase7-hpa-scaling-events.txt` |
| notification-service on external metric | 2 -> 6 on `taskflow_notification_backlog` | same |
| api-gateway on CPU | 2 -> 4 | same |
| Scale-down | all three back to 2 within ~10 min of the load (backlog drained first) | same |

## 5. High availability

`scripts/ha_test.py`: an authenticated probe every 0.25 s through the whole
stack (Ingress -> gateway -> task-service -> list-service -> PostgreSQL)
while each failure is injected.

| Experiment | Availability | Notes |
|---|---|---|
| Kill a task-service and an api-gateway Pod | 100% (99/99) | |
| Drain the application tier of one worker | 100% (325/325) | 20 Pods moved; PDBs refused 6 evictions until replacements were Ready |
| Lose one of three etcd members | 100% (94/94) | cluster health showed all members back within seconds |
| Restart NATS | 100% (85/85) | JetStream state on its PV |
| Restart PostgreSQL | 98.1% (102/104) | the test row survived; ~5 s of errors: the single-instance database is the documented SPOF |

Evidence: `phase7-ha-tests.txt`.

## 6. Issues found by testing (and fixed)

| Found by | Issue | Fix |
|---|---|---|
| UI test | gateway: requests to missing services stalled others | circuit breaker; services register their IP |
| smoke test | intermittent: completing a task cancelled a just-due reminder | test waits for the reminder to fire (the cancel is correct behaviour) |
| Kubernetes deploy | Git Bash `openssl` added CR to every generated secret | strip CR; regenerate |
| Kubernetes deploy | SeaweedFS Raft state tied to the Pod IP | fixed `-ip=127.0.0.1` |
| Argo CD | ingress Service `LoadBalancer` pending forever; Jobs always OutOfSync | NodePort; Jobs as Sync hooks |
| Argo CD | chart downloads timed out on a slow link | repo-server timeout 300 s |
| load test 1 | nodes OOM-killed (each kubelet reported the whole VM) | honest allocatable, eviction, right-sized requests, HPA maxima |
| after the fix | control plane evicted the ingress controller | no memory eviction on the control plane; controller pinned to the ingress node |
