#!/usr/bin/env python3
"""scripts/smoke_test.py -- TaskFlow end-to-end smoke test.

Drives every service through the public API, the way the UI does, and
checks the asynchronous results (search index, notifications, statistics,
audit log, recurrence) that only appear if the events flowed.

    python scripts/smoke_test.py                                   # dev harness
    python scripts/smoke_test.py --base https://app.taskflow.localhost --insecure

Standard library only. Exit code 0 means every check passed.
"""

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


class Client:
    def __init__(self, base: str, insecure: bool):
        self.base = base.rstrip("/") + "/api"
        self.ctx = ssl._create_unverified_context() if insecure else None
        self.token = None

    def call(self, method, path, body=None, raw=None, content_type=None):
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if raw is not None:
            data, headers["Content-Type"] = raw, content_type
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=20) as resp:
                payload = resp.read()
                status = resp.status
        except urllib.error.HTTPError as err:
            payload, status = err.read(), err.code
        try:
            return status, json.loads(payload) if payload else None
        except ValueError:
            return status, payload


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def eventually(fn, timeout=20.0, every=1.0):
    """Poll fn() until it returns a truthy value (asynchronous checks)."""
    end = time.time() + timeout
    while True:
        value = fn()
        if value or time.time() > end:
            return value
        time.sleep(every)


def multipart(field: str, filename: str, content: bytes, ctype: str):
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--insecure", action="store_true", help="accept the self-signed certificate")
    args = ap.parse_args()

    suffix = uuid.uuid4().hex[:8]
    alice, bob = Client(args.base, args.insecure), Client(args.base, args.insecure)

    # 1-4: gateway, auth-service, user-service
    s, body = alice.call("GET", "/tasks")
    check("api-gateway rejects a request without a token", s == 401, f"HTTP {s}")
    s, body = alice.call("POST", "/auth/register", {
        "email": f"alice-{suffix}@example.com", "password": "password123",
        "display_name": "Alice"})
    if not check("auth-service registers a user", s == 201, f"HTTP {s}"):
        return 1
    alice.token = body["access_token"]
    s, body = bob.call("POST", "/auth/register", {
        "email": f"bob-{suffix}@example.com", "password": "password123", "display_name": "Bob"})
    bob.token = body["access_token"]
    s, me = alice.call("GET", "/users/me")
    check("user-service returns the profile", s == 200 and me["display_name"] == "Alice")
    s, body = alice.call("POST", "/auth/refresh", {"refresh_token": "not-a-token"})
    check("auth-service rejects a bad refresh token", s == 401, f"HTTP {s}")

    # 5: list-service (+ sharing)
    s, lst = alice.call("POST", "/lists", {"name": f"Project {suffix}"})
    check("list-service creates a list", s == 201, f"HTTP {s}")
    s, _ = bob.call("GET", f"/lists/{lst['id']}")
    check("list-service hides another user's list", s == 404, f"HTTP {s}")
    s, _ = alice.call("POST", f"/lists/{lst['id']}/members",
                      {"email": f"bob-{suffix}@example.com", "role": "editor"})
    check("list-service shares the list with Bob", s == 201, f"HTTP {s}")

    # 6: task-service
    s, task = alice.call("POST", "/tasks", {
        "list_id": lst["id"], "title": f"Deploy observability stack {suffix}",
        "description": "Prometheus Grafana Loki Tempo", "priority": "high"})
    check("task-service creates a task", s == 201, f"HTTP {s}")
    s, tasks = bob.call("GET", f"/tasks?list_id={lst['id']}")
    check("task-service lets the shared member read it", s == 200 and len(tasks) == 1)

    # 7: tag-service
    s, tag = alice.call("POST", "/tags", {"name": f"infra-{suffix}", "color": "#059669"})
    s2, tagged = alice.call("PUT", f"/tags/task/{task['id']}", {"tag_ids": [tag["id"]]})
    check("tag-service tags the task", s2 == 200 and tagged and tagged[0]["id"] == tag["id"],
          f"HTTP {s}/{s2}")

    # 8: comment-service
    s, comment = bob.call("POST", "/comments",
                          {"task_id": task["id"], "body": f"Grafana dashboards next {suffix}"})
    check("comment-service adds a comment", s == 201 and comment["author_name"] == "Bob",
          f"HTTP {s}")

    # 9: attachment-service
    payload = f"capacity plan {suffix}\n".encode() * 100
    raw, ctype = multipart("file", "capacity.txt", payload, "text/plain")
    s, att = alice.call("POST", f"/attachments?task_id={task['id']}", raw=raw, content_type=ctype)
    ok = check("attachment-service stores a file", s == 201, f"HTTP {s}")
    if ok:
        s, data = bob.call("GET", f"/attachments/{att['id']}/download")
        check("attachment-service returns the same bytes",
              s == 200 and (data if isinstance(data, bytes) else b"") == payload, f"HTTP {s}")

    # 10: reminder-service (manual reminder due now -> reminder.due -> notification)
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
    s, _ = alice.call("POST", "/reminders", {"task_id": task["id"], "remind_at": past})
    check("reminder-service schedules a reminder", s == 201, f"HTTP {s}")

    # 11: recurrence-service
    s, _ = alice.call("PUT", f"/recurrences/task/{task['id']}", {"frequency": "weekly"})
    check("recurrence-service stores a weekly rule", s == 200, f"HTTP {s}")
    s, _ = alice.call("PATCH", f"/tasks/{task['id']}", {"status": "done"})
    check("task-service completes the task", s == 200, f"HTTP {s}")

    def next_occurrence():
        s, ts = alice.call("GET", f"/tasks?list_id={lst['id']}")
        return [t for t in (ts or []) if t.get("recurrence_of") == task["id"]] if s == 200 else []
    nxt = eventually(next_occurrence)
    check("recurrence-service created the next occurrence", bool(nxt))

    # 12: notification-service (reminder for Alice; comment + completion for Bob)
    def notes(client, kind):
        s, ns = client.call("GET", "/notifications")
        return [n for n in (ns or []) if n.get("type") == kind] if s == 200 else []
    check("notification-service delivered the reminder", bool(eventually(lambda: notes(alice, "reminder"))))
    check("notification-service told Alice about Bob's comment",
          bool(eventually(lambda: notes(alice, "comment"))))
    check("notification-service told Bob the task was completed",
          bool(eventually(lambda: notes(bob, "completed"))))

    # 13: search-service
    def found():
        s, hits = bob.call("GET", f"/search?q=dashboards%20{suffix}")
        return hits if s == 200 else []
    hits = eventually(found)
    check("search-service indexed the comment from events", bool(hits),
          hits[0]["snippet"] if hits else "")
    s, hits = Client(args.base, args.insecure).call("GET", "/search?q=x")
    check("search-service is not reachable anonymously", s == 401, f"HTTP {s}")

    # 14: analytics-service
    def stats():
        s, st = alice.call("GET", "/stats/me")
        return st if s == 200 and st["completed"] >= 1 else None
    st = eventually(stats)
    check("analytics-service counted the completion", bool(st),
          f"created={st['created']} completed={st['completed']}" if st else "")

    # 15: audit-service
    def history():
        s, rows = alice.call("GET", f"/audit?entity_id={task['id']}")
        return rows if s == 200 and len(rows) >= 4 else None
    rows = eventually(history)
    check("audit-service recorded the task's history", bool(rows),
          ", ".join(sorted({r["event_type"] for r in rows})) if rows else "")

    # web-frontend (only when served behind the Ingress)
    if not args.base.startswith("http://localhost:8000"):
        try:
            with urllib.request.urlopen(args.base, context=alice.ctx, timeout=10) as resp:
                html = resp.read().decode()
            check("web-frontend serves the app", "<div id=\"root\">" in html)
        except Exception as exc:  # noqa: BLE001
            check("web-frontend serves the app", False, str(exc))

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
