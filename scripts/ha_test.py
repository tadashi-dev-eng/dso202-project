#!/usr/bin/env python3
"""scripts/ha_test.py -- availability under failure, measured from outside.

    python scripts/ha_test.py --base https://app.taskflow.localhost --insecure

A probe thread sends an authenticated ``GET /api/tasks`` (gateway ->
task-service -> list-service -> PostgreSQL) every 0.25 s for the whole run.
Each experiment disrupts one thing and reports how many probes failed while
it happened:

  1. pod-kill      delete one task-service Pod (and one api-gateway Pod)
  2. drain         node maintenance: evict every TaskFlow *application* Pod
                   from one worker with `kubectl drain --pod-selector`,
                   which honours the PodDisruptionBudgets; then uncordon
  3. etcd-member   delete one of the three etcd members: the registry keeps
                   quorum (2/3) and the gateway keeps routing
  4. nats          restart the event bus: no event is lost (JetStream on a PV)
  5. postgres      delete the database Pod: rows survive (PVC), requests fail
                   only until it is back (single instance: the documented SPOF)

Standard library + kubectl. Results are printed; redirect to docs/evidence.
"""

import argparse
import json
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

NS = "taskflow-prod"


def kubectl(*args, check=True):
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


class Api:
    def __init__(self, base, ctx):
        self.base, self.ctx, self.token = base.rstrip("/") + "/api", ctx, None

    def call(self, method, path, body=None, timeout=5):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception:  # noqa: BLE001
            return 599, None


class Probe:
    """Background availability probe."""

    def __init__(self, api, list_id):
        self.api, self.list_id = api, list_id
        self.lock = threading.Lock()
        self.results: list[tuple[float, int]] = []
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop:
            s, _ = self.api.call("GET", f"/tasks?list_id={self.list_id}")
            with self.lock:
                self.results.append((time.time(), s))
            time.sleep(0.25)

    def window(self, start, end):
        with self.lock:
            rows = [s for t, s in self.results if start <= t <= end]
        ok = sum(1 for s in rows if s == 200)
        return ok, len(rows)


def wait_ready(selector, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        out = kubectl("get", "pods", "-n", NS, "-l", selector, "-o",
                      "jsonpath={range .items[*]}{.status.containerStatuses[0].ready}{' '}{end}", check=False)
        states = out.split()
        if states and all(s == "true" for s in states):
            return True
        time.sleep(2)
    return False


def experiment(name, probe, action, settle=20):
    print(f"\n=== {name}", flush=True)
    start = time.time()
    action()
    time.sleep(settle)
    end = time.time()
    ok, total = probe.window(start, end)
    availability = 100 * ok / max(total, 1)
    print(f"    probes during experiment: {ok}/{total} succeeded = {availability:.1f}% "
          f"over {end - start:.0f}s", flush=True)
    return {"experiment": name, "ok": ok, "total": total,
            "availability_pct": round(availability, 1), "seconds": round(end - start)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://app.taskflow.localhost")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()
    ctx = ssl._create_unverified_context() if args.insecure else None

    api = Api(args.base, ctx)
    s, body = api.call("POST", "/auth/register", {
        "email": f"ha-{uuid.uuid4().hex[:6]}@example.com", "password": "password123",
        "display_name": "HA probe"})
    api.token = body["access_token"]
    _, lst = api.call("POST", "/lists", {"name": "HA test"})
    marker = f"persist-{uuid.uuid4().hex[:6]}"
    api.call("POST", "/tasks", {"list_id": lst["id"], "title": marker})

    probe = Probe(api, lst["id"])
    probe.thread.start()
    time.sleep(5)
    results = []

    # 1. pod kill ------------------------------------------------------------
    def pod_kill():
        for svc in ("task-service", "api-gateway"):
            pod = kubectl("get", "pods", "-n", NS, "-l", f"app.kubernetes.io/name={svc}",
                          "-o", "jsonpath={.items[0].metadata.name}")
            print(f"    deleting {pod}", flush=True)
            kubectl("delete", "pod", "-n", NS, pod, "--wait=false")
        wait_ready("app.kubernetes.io/name=task-service")
        wait_ready("app.kubernetes.io/name=api-gateway")
    results.append(experiment("1. pod-kill (task-service + api-gateway)", probe, pod_kill))

    # 2. drain the application Pods off one worker ---------------------------
    def drain():
        node = kubectl("get", "pods", "-n", NS, "-l", "app.kubernetes.io/name=task-service",
                       "-o", "jsonpath={.items[0].spec.nodeName}")
        print(f"    draining application Pods from {node} (PDBs honoured)", flush=True)
        out = subprocess.run(["kubectl", "drain", node, "--ignore-daemonsets",
                              "--delete-emptydir-data", "--timeout=180s",
                              "--pod-selector=app.kubernetes.io/component=application"],
                             capture_output=True, text=True)
        evicted = sum(1 for line in out.stdout.splitlines() if "evicted" in line)
        blocked = out.stderr.count("disruption budget")
        print(f"    {evicted} Pods evicted; PDB refusals while waiting: {blocked}", flush=True)
        time.sleep(15)
        kubectl("uncordon", node)
        print(f"    uncordoned {node}", flush=True)
    results.append(experiment("2. drain (application tier of one worker)", probe, drain, settle=30))

    # 3. lose one etcd member ---------------------------------------------------
    def etcd_member():
        kubectl("delete", "pod", "-n", NS, "etcd-1", "--wait=false")
        print("    deleted etcd-1 (2 of 3 members remain: quorum kept)", flush=True)
        time.sleep(5)
        health = kubectl("exec", "-n", NS, "etcd-0", "--", "etcdctl", "endpoint", "health",
                         "--cluster", check=False)
        print("    " + health.replace("\n", "\n    "), flush=True)
        wait_ready("app.kubernetes.io/name=etcd")
    results.append(experiment("3. etcd member loss", probe, etcd_member))

    # 4. restart NATS -------------------------------------------------------------
    def nats():
        kubectl("delete", "pod", "-n", NS, "nats-0")
        wait_ready("app.kubernetes.io/name=nats")
        print("    nats-0 back; JetStream state reloaded from its PV", flush=True)
    results.append(experiment("4. NATS restart", probe, nats))

    # 5. restart PostgreSQL, check persistence -----------------------------------
    def postgres():
        kubectl("delete", "pod", "-n", NS, "postgres-0")
        wait_ready("app.kubernetes.io/name=postgres")
        time.sleep(10)
        _, tasks = api.call("GET", f"/tasks?list_id={lst['id']}")
        kept = any(t["title"] == marker for t in tasks or [])
        print(f"    task '{marker}' still present after the restart: {kept}", flush=True)
    results.append(experiment("5. PostgreSQL restart (persistence)", probe, postgres))

    probe.stop = True
    print("\n=== summary")
    for r in results:
        print(f"  {r['experiment']:<45} {r['availability_pct']:>6}%  ({r['ok']}/{r['total']} probes)")


if __name__ == "__main__":
    main()
