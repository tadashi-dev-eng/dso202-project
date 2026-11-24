# High availability and capacity plan

All numbers here are **measured** on the project cluster: kind, three nodes
in one Docker/WSL VM capped at 9 GB on a 16 GB laptop. Evidence files are in
[`evidence/`](evidence/).

## 1. High-availability design

| Failure | Protection | Verified by |
|---|---|---|
| A Pod crashes or is deleted | 2+ replicas per service (prod); readiness probes remove a Pod from its Service before it serves; `preStop` sleep so a terminating Pod drains | HA test 1: 100% availability |
| A node is drained for maintenance | PodDisruptionBudget `minAvailable: 1` per service; zone/node topology spread and anti-affinity keep the replicas apart | HA test 2: 20 Pods moved, 6 evictions refused by PDBs, 100% availability |
| One etcd member is lost | 3-member Raft cluster, PDB `minAvailable: 2`; gateway falls back to DNS if the registry is unavailable | HA test 3: quorum kept, 100% availability |
| The event bus restarts | JetStream file storage on a PV; durable consumers resume where they stopped | HA test 4: 100% availability, no lost events |
| The database restarts | PVC (`volumeClaimTemplates`, `Retain`), services retry with back-off, readiness gates traffic | HA test 5: data kept, 98.1% availability (~5 s) |
| A dependency is down | readiness reports it (traffic stops), liveness does **not** (no restart storms); gateway circuit breaker returns 503 at once | unit tests; Compose test |
| Overload | edge rate limit (50 rps/IP at the Ingress), per-user limit in the gateway (live from etcd), autoscaling, bounded HPA maxima | load test 2: shed load as 503s, p95 < 200 ms |
| Memory exhaustion | honest node allocatable + eviction before the kernel OOM-killer (section 4) | load test 2: no OOM, no Pending |
| A bad release | Git is the only change path; rollback = `git revert`, synced in ~12 s | GitOps evidence |

**Known single points of failure (accepted for a laptop cluster):**
PostgreSQL, Redis, NATS and the object store are single instances (with
persistent volumes). A production deployment would run PostgreSQL with a
replication operator (e.g. CloudNativePG, 3 instances), NATS as a 3-node
JetStream cluster and Redis with Sentinel, all with zone spread. The kind
control plane is a single node.

## 2. Measured resource use (prod, steady state)

| Component | Pods | Memory per Pod (measured) | Request / limit |
|---|---|---|---|
| Python service | 28 (14 x 2) | ~60 Mi | 72 Mi / 192 Mi |
| api-gateway | 2 (HPA 2-4) | ~62 Mi | 80 Mi / 192 Mi |
| web-frontend (nginx) | 2 | ~2 Mi | 16 Mi / 64 Mi |
| PostgreSQL + exporter | 1 | ~166 Mi | 272 Mi / 832 Mi |
| object store (SeaweedFS) | 1 | ~146 Mi | 64 Mi / 256 Mi |
| etcd | 3 | ~35 Mi | 32 Mi / 128 Mi |
| NATS / Redis | 1 / 1 | ~21 Mi / ~6 Mi | 32 Mi / 256 Mi, 128 Mi |

Per namespace (kubectl top, steady state): taskflow-prod ~2.1 GiB,
observability ~1.8 GiB, Argo CD ~0.7 GiB, kube-system ~1.4 GiB, ingress
~0.1 GiB. The VM holds ~6.8 GiB of 8.7 GiB at rest, dev parked.

Requests are set **just above** measured use, so the scheduler's arithmetic
matches reality; limits leave room for bursts (and stop a leak at 192 Mi).

## 3. Load and scaling (load test 2)

8 virtual users, 6 minutes, mix of 55% list, 15% create, 10% update, 20%
comment on a list shared by 8 people (`scripts/loadtest.py`).

| Measure | Result |
|---|---|
| Throughput | 45 req/s (17,008 requests) |
| Latency p50 / p95 / p99 | 40-54 ms / 149-194 ms / 333-445 ms |
| Errors | 6.5%, all `503` from the Ingress edge rate limit (912 limit hits in the controller log); no 5xx from services |
| task-service | 2 -> 5 replicas on `http_requests_per_second` (peaked 17/s per Pod vs target 10) |
| notification-service | 2 -> 6 (max) on `taskflow_notification_backlog` |
| api-gateway | 2 -> 4 (max) on CPU |
| Lowest VM free memory | ~930 MiB; no Pending Pods, no OOM |
| Scale-down | over ~5-10 min after load stopped (120 s stabilisation, 1 Pod/min) |

**Backlog arithmetic.** Each comment notifies the 7 other members, at
~200 ms per delivery (simulated provider) and one message at a time per
replica: 5 deliveries/s per replica. Load test 2 produced ~3,400 comments
= ~24,000 deliveries; 6 replicas drain 30/s, so the backlog took ~13 min to
reach zero after the load stopped. To keep the backlog under 1 minute at
that rate would need ~80 deliveries/s: either 16 replicas or concurrent
delivery inside each replica (the cheaper fix: `asyncio.gather` over a
batch). Recorded as the first improvement.

## 4. The capacity incident and the fix

Load test 1 (same load, before the fix) crashed: both worker containers
were OOM-killed and nearly every Pod restarted. Root cause: the three kind
nodes share one VM, but each kubelet reported the **whole VM** (8.7 GiB) as
its allocatable memory, so the scheduler believed it had 26 GiB and the
HPAs kept adding Pods until the kernel killed processes.

Fixes (`scripts/node-capacity.sh`, request changes in the charts):

1. `systemReserved` per node so allocatable sums to what the VM can hold:
   control plane 1.6 GiB, workers 3.2 GiB each.
2. Eviction at 250 MiB free on the workers, so kubelet evicts a Pod before
   the kernel kills a node. The control plane has **no** memory eviction:
   kubelet measures the whole VM, and the only evictable Pod there is the
   ingress controller (losing it takes the site down; this happened once
   before the fix).
3. Requests from measurement (72 Mi instead of 96 Mi) and HPA maxima that
   fit (gateway 4, task-service 6, notification-service 6).
4. Argo CD Pods given requests (they were BestEffort, first to be evicted).
5. dev parked (`paused`), freeing ~1.6 GiB.

Result: load test 2 ran at 2.2x the throughput with 5-6x lower latency and
no restarts.

## 5. Headroom and limits

Worker allocatable 2 x 3.2 GiB = 6.4 GiB. At rest the workers' Pods request
~4.4 GiB, so ~2 GiB (about 27 more Python Pods at 72 Mi) is available to the
autoscalers. All three at their maxima add 2 + 4 + 4 = 10 Pods (~0.75 GiB):
it fits with ~1.2 GiB to spare, which covers rolling updates (maxSurge 1).

Kubernetes does not rebalance running Pods: after the drain test, worker2
held 90% of its allocatable in requests and the other worker 43%. New Pods
still land on the emptier node (topology spread), so scaling is unaffected,
but a further drain of worker2 would have less room. The
[descheduler](https://github.com/kubernetes-sigs/descheduler) (or a rolling
restart) would rebalance; noted, not installed.

| If we needed... | Then |
|---|---|
| more than ~45 req/s from one client | raise the Ingress `limit-rps` (per client IP) |
| more throughput per task-service Pod | requests are I/O-bound: more uvicorn workers or DB pool size |
| faster notification drain | concurrent delivery per replica (see section 3) |
| dev and prod at the same time | +1.6 GiB: VM cap 11 GB, or a cloud VM (as in the proposal) |
| a real production cluster | 3+ worker nodes across zones, replicated data stores, Cluster Autoscaler |

## 6. Quotas

ResourceQuota per environment (`deploy/environments`): prod 4 CPU / 6 GiB
requests, 80 Pods; dev 2 CPU / 3 GiB, 40 Pods. Prod at full scale requests
~3.0 GiB of its 6 GiB, so the quota never blocks the HPAs, but a runaway
autoscaler or a mistaken replica count cannot take the whole cluster.
The alert `TaskflowQuotaNearlyExhausted` fires at 90%.
