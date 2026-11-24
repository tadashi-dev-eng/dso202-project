#!/usr/bin/env python3
"""scripts/loadtest.py -- drives the two custom-metric autoscalers.

    python scripts/loadtest.py --base https://app.taskflow.localhost --insecure \
        --users 12 --duration 300

Each virtual user (one thread, its own account, so the gateway's per-user
limit is not the bottleneck) loops over a realistic mix:

    GET /tasks (x3) ......... read-heavy traffic   -> task-service req/s per Pod
    POST /tasks, PATCH ...... writes, task events  -> search/analytics/audit
    POST /comments .......... comment on a list shared with the other users
                              -> one notification per other member
                              -> notification-service backlog

Results: request count, error count and latency percentiles per operation,
printed at the end and written to loadtest/results/<timestamp>.json.
Standard library only.
"""

import argparse
import json
import random
import ssl
import statistics
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from pathlib import Path


class Api:
    def __init__(self, base, ctx, host=None):
        self.base, self.ctx, self.host, self.token = base.rstrip("/") + "/api", ctx, host, None

    def call(self, method, path, body=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.host:
            headers["Host"] = self.host
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=30) as resp:
                status, payload = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, payload = err.code, err.read()
        except Exception:  # noqa: BLE001 -- connection errors count as failures
            status, payload = 599, b""
        elapsed = time.perf_counter() - start
        try:
            return status, (json.loads(payload) if payload else None), elapsed
        except ValueError:
            return status, None, elapsed


stats = defaultdict(list)          # op -> [latency, ...]
errors = defaultdict(int)          # op -> count of non-2xx
lock = threading.Lock()


def record(op, status, elapsed):
    with lock:
        stats[op].append(elapsed)
        if status >= 400:
            errors[f"{op} {status}"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://app.taskflow.localhost")
    ap.add_argument("--host")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--users", type=int, default=12)
    ap.add_argument("--duration", type=int, default=300, help="seconds")
    ap.add_argument("--think", type=float, default=0.05, help="pause between requests (s)")
    args = ap.parse_args()
    ctx = ssl._create_unverified_context() if args.insecure else None
    run = uuid.uuid4().hex[:6]

    # --- setup: users, one shared list, a few tasks ---------------------------
    users = []
    for i in range(args.users):
        api = Api(args.base, ctx, args.host)
        s, body, _ = api.call("POST", "/auth/register", {
            "email": f"load-{run}-{i}@example.com", "password": "password123",
            "display_name": f"Load {i}"})
        if s != 201:
            raise SystemExit(f"registration failed: HTTP {s} {body}")
        api.token = body["access_token"]
        users.append(api)
    owner = users[0]
    _, shared, _ = owner.call("POST", "/lists", {"name": f"Load test {run}"})
    for i, u in enumerate(users[1:], start=1):
        owner.call("POST", f"/lists/{shared['id']}/members",
                   {"email": f"load-{run}-{i}@example.com", "role": "editor"})
    seed = []
    for n in range(5):
        _, t, _ = owner.call("POST", "/tasks", {"list_id": shared["id"], "title": f"seed {n}"})
        seed.append(t["id"])
    print(f"{len(users)} users on list {shared['id']}; running {args.duration}s ...", flush=True)

    stop = time.time() + args.duration

    def worker(api: Api):
        mine = list(seed)
        while time.time() < stop:
            r = random.random()
            if r < 0.55:
                s, _, e = api.call("GET", f"/tasks?list_id={shared['id']}")
                record("list tasks", s, e)
            elif r < 0.70:
                s, t, e = api.call("POST", "/tasks", {
                    "list_id": shared["id"], "title": f"load {uuid.uuid4().hex[:8]}",
                    "priority": random.choice(["low", "medium", "high"])})
                record("create task", s, e)
                if s == 201:
                    mine.append(t["id"])
            elif r < 0.80:
                s, _, e = api.call("PATCH", f"/tasks/{random.choice(mine)}",
                                   {"status": random.choice(["in_progress", "done", "todo"])})
                record("update task", s, e)
            else:
                s, _, e = api.call("POST", "/comments", {
                    "task_id": random.choice(mine), "body": f"load comment {uuid.uuid4().hex[:6]}"})
                record("comment", s, e)
            time.sleep(args.think)

    threads = [threading.Thread(target=worker, args=(u,), daemon=True) for u in users]
    started = time.time()
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(15)
        with lock:
            total = sum(len(v) for v in stats.values())
            failed = sum(errors.values())
        print(f"  t+{int(time.time() - started):>4}s  requests={total}  "
              f"rate={total / (time.time() - started):.1f}/s  errors={failed}", flush=True)
    for t in threads:
        t.join()

    took = time.time() - started
    summary = {"run": run, "users": args.users, "duration_s": round(took), "operations": {}}
    print(f"\n{'operation':<14}{'count':>8}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}")
    for op, lat in sorted(stats.items()):
        lat.sort()
        q = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] * 1000  # noqa: E731
        summary["operations"][op] = {"count": len(lat), "p50_ms": round(q(.5)),
                                     "p95_ms": round(q(.95)), "p99_ms": round(q(.99)),
                                     "mean_ms": round(statistics.mean(lat) * 1000)}
        print(f"{op:<14}{len(lat):>8}{q(.5):>9.0f}{q(.95):>9.0f}{q(.99):>9.0f}")
    total = sum(len(v) for v in stats.values())
    summary.update(total_requests=total, throughput_rps=round(total / took, 1),
                   errors=dict(errors), error_rate=round(sum(errors.values()) / max(total, 1), 4))
    print(f"\ntotal {total} requests, {total / took:.1f} req/s, "
          f"errors {sum(errors.values())} ({summary['error_rate']:.2%}) {dict(errors)}")
    out = Path("loadtest/results")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{time.strftime('%Y%m%d-%H%M%S')}-{run}.json"
    path.write_text(json.dumps(summary, indent=2))
    print(f"written {path}")


if __name__ == "__main__":
    main()
