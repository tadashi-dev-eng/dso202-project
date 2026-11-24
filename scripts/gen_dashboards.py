#!/usr/bin/env python3
"""scripts/gen_dashboards.py -- Grafana dashboards as code.

    python scripts/gen_dashboards.py      # writes deploy/observability/dashboards/*.json

Two dashboards, both with a ``namespace`` variable (taskflow-prod / -dev):

* TaskFlow - Application:     RED per service (rate, errors, p95 latency),
  business metrics, gateway behaviour, notification backlog vs. replicas,
  HPA replica counts, error logs (Loki) with trace links.
* TaskFlow - Infrastructure:  nodes, Pods, restarts, container CPU/memory
  against quota, storage, PostgreSQL, the etcd registry cluster and the
  Kubernetes API server.

Generated rather than hand-edited so every panel follows the same layout
and the JSON stays reviewable in Git. Grafana loads the files through the
dashboard sidecar (ConfigMaps labelled grafana_dashboard=1).
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "deploy" / "observability" / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
NS = 'namespace="$namespace"'


class Board:
    def __init__(self, uid: str, title: str, description: str):
        self.uid, self.title, self.description = uid, title, description
        self.panels: list[dict] = []
        self.y = 0
        self.x = 0
        self.row_h = 0
        self.next_id = 1

    def _place(self, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x, self.y = 0, self.y + self.row_h
            self.row_h = 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def row(self, title: str) -> None:
        if self.x:
            self.x, self.y = 0, self.y + self.row_h
        self.panels.append({"id": self.next_id, "type": "row", "title": title, "collapsed": False,
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.next_id += 1
        self.y += 1
        self.row_h = 0

    def panel(self, kind: str, title: str, targets: list[tuple[str, str]], w=12, h=8,
              unit="short", desc="", ds=PROM, **extra) -> None:
        p = {
            "id": self.next_id, "type": kind, "title": title, "description": desc,
            "datasource": ds, "gridPos": self._place(w, h),
            "targets": [{"refId": chr(65 + i), "expr": expr, "legendFormat": legend,
                         "datasource": ds} for i, (expr, legend) in enumerate(targets)],
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "options": {},
        }
        if kind == "timeseries":
            p["fieldConfig"]["defaults"]["custom"] = {"lineWidth": 2, "fillOpacity": 8,
                                                      "showPoints": "never"}
            p["options"] = {"legend": {"displayMode": "table", "placement": "right",
                                       "calcs": ["lastNotNull", "max"]},
                            "tooltip": {"mode": "multi", "sort": "desc"}}
        if kind == "stat":
            p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value",
                            "graphMode": "area", "textMode": "value_and_name"}
        thresholds = extra.pop("thresholds", None)
        if thresholds:
            p["fieldConfig"]["defaults"]["thresholds"] = {
                "mode": "absolute",
                "steps": [{"color": "green", "value": None}] +
                         [{"color": c, "value": v} for v, c in thresholds]}
            p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
        p.update(extra)
        self.panels.append(p)
        self.next_id += 1

    def json(self) -> dict:
        return {
            "uid": self.uid, "title": self.title, "description": self.description,
            "tags": ["taskflow", "dso202"], "timezone": "browser", "schemaVersion": 39,
            "editable": True, "refresh": "15s", "time": {"from": "now-1h", "to": "now"},
            "templating": {"list": [{
                "name": "namespace", "label": "Environment", "type": "custom",
                "query": "taskflow-prod,taskflow-dev",
                "current": {"text": "taskflow-prod", "value": "taskflow-prod"},
                "options": [{"text": n, "value": n, "selected": n == "taskflow-prod"}
                            for n in ("taskflow-prod", "taskflow-dev")],
            }]},
            "annotations": {"list": []},
            "panels": self.panels,
        }


def application() -> Board:
    b = Board("taskflow-app", "TaskFlow - Application",
              "Request rate, errors and latency per service; business metrics; autoscaling")
    req = f'http_server_requests_total{{{NS}}}'
    b.row("Overview")
    b.panel("stat", "Requests / s", [(f"sum(rate({req}[1m]))", "req/s")], w=4, h=4, unit="reqps")
    b.panel("stat", "Error rate (5xx)", [(
        f'100 * sum(rate(http_server_requests_total{{{NS},status=~"5.."}}[5m])) '
        f'/ clamp_min(sum(rate({req}[5m])), 0.001)', "errors")],
        w=4, h=4, unit="percent", thresholds=[(1, "orange"), (5, "red")])
    b.panel("stat", "p95 latency (gateway)", [(
        'histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket'
        f'{{{NS},service="api-gateway"}}[5m])))', "p95")],
        w=4, h=4, unit="s", thresholds=[(0.3, "orange"), (1, "red")])
    b.panel("stat", "Active users (15 min)", [(f"max(taskflow_active_users{{{NS}}})", "users")],
            w=4, h=4)
    b.panel("stat", "Tasks created / completed", [
        (f'max(taskflow_platform_total{{{NS},kind="created"}})', "created"),
        (f'max(taskflow_platform_total{{{NS},kind="completed"}})', "completed")], w=4, h=4)
    b.panel("stat", "Services up", [(
        f'count(up{{{NS},job=~".*-service|api-gateway"}} == 1)', "up")], w=4, h=4,
        thresholds=[(0, "red"), (14, "green")])

    b.row("RED per service")
    b.panel("timeseries", "Request rate by service",
            [(f"sum by (service) (rate({req}[1m]))", "{{service}}")], unit="reqps",
            desc="Rate of HTTP requests handled, from the RED middleware in taskflow_common")
    b.panel("timeseries", "Errors (5xx) by service", [(
        f'sum by (service) (rate(http_server_requests_total{{{NS},status=~"5.."}}[5m]))',
        "{{service}}")], unit="reqps")
    b.panel("timeseries", "p95 latency by service", [(
        'histogram_quantile(0.95, sum by (le, service) '
        f'(rate(http_server_request_duration_seconds_bucket{{{NS}}}[5m])))', "{{service}}")],
        unit="s")
    b.panel("timeseries", "4xx by service (client errors, auth, rate limit)", [(
        f'sum by (service, status) (rate(http_server_requests_total{{{NS},status=~"4.."}}[5m]))',
        "{{service}} {{status}}")], unit="reqps")

    b.row("Business metrics")
    b.panel("timeseries", "Task activity per minute", [(
        f'sum by (action) (rate(taskflow_task_changes_total{{{NS}}}[5m])) * 60', "{{action}}")],
        unit="short")
    b.panel("timeseries", "Notifications sent per minute", [(
        f'sum by (channel) (rate(taskflow_notifications_sent_total{{{NS}}}[5m])) * 60',
        "{{channel}}")])
    b.panel("timeseries", "Events processed per minute (analytics)", [(
        f'sum by (event) (rate(taskflow_business_events_total{{{NS}}}[5m])) * 60', "{{event}}")])
    b.panel("timeseries", "Auth outcomes per minute", [(
        f'sum by (event, result) (rate(taskflow_auth_events_total{{{NS}}}[5m])) * 60',
        "{{event}} {{result}}")])

    b.row("Autoscaling and the event backlog")
    b.panel("timeseries", "Notification backlog vs. replicas", [
        (f"max(taskflow_notification_backlog{{{NS}}})", "backlog (messages)"),
        (f'kube_deployment_status_replicas{{{NS},deployment="notification-service"}}',
         "notification-service replicas")],
        desc="The external metric the notification-service HPA scales on (target 20 per replica)")
    b.panel("timeseries", "task-service requests/s per Pod vs. replicas", [
        (f'sum by (pod) (rate(http_server_requests_total{{{NS},service="task-service"}}[1m]))',
         "{{pod}}"),
        (f'kube_deployment_status_replicas{{{NS},deployment="task-service"}}', "replicas")],
        desc="The custom metric the task-service HPA scales on (target 10 req/s per Pod)")
    b.panel("timeseries", "HPA current replicas", [(
        f'kube_horizontalpodautoscaler_status_current_replicas{{{NS}}}',
        "{{horizontalpodautoscaler}}")], w=12)
    b.panel("timeseries", "HPA desired replicas", [(
        f'kube_horizontalpodautoscaler_status_desired_replicas{{{NS}}}',
        "{{horizontalpodautoscaler}}")], w=12)

    b.row("Gateway")
    b.panel("timeseries", "Upstream requests by discovery path", [(
        f'sum by (discovery) (rate(gateway_upstream_requests_total{{{NS}}}[1m]))', "{{discovery}}")],
        w=8, desc="registry = etcd service registry; dns = Kubernetes Service fallback")
    b.panel("timeseries", "Rate-limited and short-circuited", [
        (f'sum(rate(gateway_rate_limited_total{{{NS}}}[1m]))', "429 rate limited"),
        (f'sum by (upstream) (rate(gateway_circuit_open_total{{{NS}}}[1m]))',
         "503 circuit open {{upstream}}")], w=8)
    b.panel("stat", "Rate limit in force (etcd)", [(
        f'max(gateway_rate_limit_per_minute{{{NS}}})', "requests/min")], w=8, h=8)

    b.row("Logs")
    b.panel("logs", "Warnings and errors (click a line for its trace)",
            [('{namespace="$namespace", level=~"warning|error"}', "")], w=24, h=10, ds=LOKI,
            options={"showTime": True, "wrapLogMessage": True, "enableLogDetails": True,
                     "sortOrder": "Descending"})
    return b


def infrastructure() -> Board:
    b = Board("taskflow-infra", "TaskFlow - Infrastructure",
              "Nodes, Pods, resources, storage, PostgreSQL, etcd and the API server")
    b.row("Nodes")
    b.panel("timeseries", "Node CPU used", [(
        '100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
        "{{instance}}")], w=8, unit="percent")
    b.panel("timeseries", "Node memory used", [(
        '100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)', "{{instance}}")],
        w=8, unit="percent")
    b.panel("timeseries", "Pods per node", [(
        'sum by (node) (kube_pod_info{node!=""})', "{{node}}")], w=8)

    b.row("Pods in $namespace")
    b.panel("stat", "Pods running", [(
        f'sum(kube_pod_status_phase{{{NS},phase="Running"}})', "running")], w=6, h=4)
    b.panel("stat", "Pods not ready", [(
        f'sum(kube_pod_status_ready{{{NS},condition="false"}})', "not ready")], w=6, h=4,
        thresholds=[(1, "red")])
    b.panel("stat", "Restarts (1 h)", [(
        f'sum(increase(kube_pod_container_status_restarts_total{{{NS}}}[1h]))', "restarts")],
        w=6, h=4, thresholds=[(1, "orange"), (5, "red")])
    b.panel("stat", "PodDisruptionBudgets allowing disruption", [(
        f'count(kube_poddisruptionbudget_status_pod_disruptions_allowed{{{NS}}} > 0)', "PDBs")],
        w=6, h=4)
    b.panel("timeseries", "Container memory by Pod", [(
        f'sum by (pod) (container_memory_working_set_bytes{{{NS},container!=""}})', "{{pod}}")],
        unit="bytes")
    b.panel("timeseries", "Container CPU by Pod", [(
        f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{NS},container!=""}}[5m]))',
        "{{pod}}")], unit="cores")
    b.panel("timeseries", "Quota used (requests)", [
        (f'kube_resourcequota{{{NS},type="used",resource="requests.memory"}} / '
         f'kube_resourcequota{{{NS},type="hard",resource="requests.memory"}} * 100', "memory"),
        (f'kube_resourcequota{{{NS},type="used",resource="requests.cpu"}} / '
         f'kube_resourcequota{{{NS},type="hard",resource="requests.cpu"}} * 100', "cpu"),
        (f'kube_resourcequota{{{NS},type="used",resource="pods"}} / '
         f'kube_resourcequota{{{NS},type="hard",resource="pods"}} * 100', "pods")],
        unit="percent", desc="Share of the namespace ResourceQuota in use")
    b.panel("table", "Containers restarted in the last hour", [(
        f'sort_desc(sum by (pod, container) (increase(kube_pod_container_status_restarts_total{{{NS}}}[1h])) > 0)',
        "")], options={"showHeader": True},
        transformations=[{"id": "reduce", "options": {"reducers": ["lastNotNull"]}}])

    b.row("Storage and PostgreSQL")
    b.panel("timeseries", "PersistentVolumeClaims (requested)", [(
        f'kube_persistentvolumeclaim_resource_requests_storage_bytes{{{NS}}}',
        "{{persistentvolumeclaim}}")], w=8, unit="bytes")
    b.panel("timeseries", "Database size per service", [(
        f'pg_database_size_bytes{{{NS},datname!~"postgres|template.*"}}', "{{datname}}")],
        w=8, unit="bytes")
    b.panel("timeseries", "PostgreSQL connections per database", [(
        f'sum by (datname) (pg_stat_activity_count{{{NS},datname!=""}})', "{{datname}}")], w=8)

    b.row("etcd service registry (3 members)")
    b.panel("stat", "Members with a leader", [(
        f'sum(etcd_server_has_leader{{{NS}}})', "members")], w=6, h=4,
        thresholds=[(0, "red"), (2, "orange"), (3, "green")])
    b.panel("stat", "Leader changes (1 h)", [(
        f'sum(increase(etcd_server_leader_changes_seen_total{{{NS}}}[1h]))', "changes")],
        w=6, h=4, thresholds=[(1, "orange"), (3, "red")])
    b.panel("timeseries", "Registry write latency p99 (WAL fsync)", [(
        'histogram_quantile(0.99, sum by (le, pod) '
        f'(rate(etcd_disk_wal_fsync_duration_seconds_bucket{{{NS}}}[5m])))', "{{pod}}")],
        w=12, h=8, unit="s")
    b.panel("timeseries", "etcd database size", [(
        f'etcd_mvcc_db_total_size_in_bytes{{{NS}}}', "{{pod}}")], w=12, unit="bytes")
    b.panel("timeseries", "etcd client traffic", [(
        f'sum by (pod) (rate(etcd_network_client_grpc_received_bytes_total{{{NS}}}[5m]))',
        "{{pod}}")], w=12, unit="Bps")

    b.row("Kubernetes API server")
    b.panel("timeseries", "API request latency p99 by verb", [(
        'histogram_quantile(0.99, sum by (le, verb) (rate(apiserver_request_duration_seconds_bucket'
        '{verb!~"WATCH|CONNECT"}[5m])))', "{{verb}}")], unit="s")
    b.panel("timeseries", "API requests by response code", [(
        'sum by (code) (rate(apiserver_request_total[5m]))', "{{code}}")], unit="reqps")
    return b


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for board in (application(), infrastructure()):
        path = OUT / f"{board.uid}.json"
        path.write_text(json.dumps(board.json(), indent=2) + "\n")
        print(f"wrote {path.relative_to(OUT.parent.parent.parent)} ({len(board.panels)} panels)")


if __name__ == "__main__":
    main()
