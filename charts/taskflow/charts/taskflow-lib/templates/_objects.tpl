{{/*
charts/taskflow-lib/templates/_objects.tpl -- everything around the Deployment.
*/}}

{{/* One ServiceAccount per workload: least privilege, nothing bound to it. */}}
{{- define "taskflow-lib.serviceaccount" -}}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "taskflow-lib.name" . }}
  labels:
    {{- include "taskflow-lib.labels" . | nindent 4 }}
automountServiceAccountToken: false
{{- end -}}

{{- define "taskflow-lib.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "taskflow-lib.name" . }}
  labels:
    {{- include "taskflow-lib.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    {{- include "taskflow-lib.selectorLabels" . | nindent 4 }}
  ports:
    - name: http
      port: {{ .Values.servicePort | default 8000 }}
      targetPort: http
{{- end -}}

{{/*
HorizontalPodAutoscaler (autoscaling/v2). Metrics come from values:
  autoscaling.cpu            -> Resource metric (CPU utilisation %)
  autoscaling.podsMetric     -> Pods metric from Prometheus Adapter,
                                e.g. http_requests_per_second per pod
  autoscaling.externalMetric -> External metric from Prometheus Adapter,
                                e.g. taskflow_notification_backlog
The behaviour block scales up fast and down slowly, so a short dip in
traffic does not flap the replica count.
*/}}
{{- define "taskflow-lib.hpa" -}}
{{- with .Values.autoscaling }}
{{- if .enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "taskflow-lib.name" $ }}
  labels:
    {{- include "taskflow-lib.labels" $ | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "taskflow-lib.name" $ }}
  minReplicas: {{ .minReplicas }}
  maxReplicas: {{ .maxReplicas }}
  metrics:
    {{- if .cpu }}
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .cpu }}
    {{- end }}
    {{- with .podsMetric }}
    - type: Pods
      pods:
        metric:
          name: {{ .name }}
        target:
          type: AverageValue
          averageValue: {{ .target | quote }}
    {{- end }}
    {{- with .externalMetric }}
    - type: External
      external:
        metric:
          name: {{ .name }}
          {{- with .selector }}
          selector:
            matchLabels:
              {{- toYaml . | nindent 14 }}
          {{- end }}
        target:
          type: AverageValue
          averageValue: {{ .target | quote }}
    {{- end }}
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
        - type: Pods
          value: 2
          periodSeconds: 30
    scaleDown:
      stabilizationWindowSeconds: 120
      policies:
        - type: Pods
          value: 1
          periodSeconds: 60
{{- end }}
{{- end }}
{{- end -}}

{{/* PodDisruptionBudget: a node drain may evict at most what keeps minAvailable. */}}
{{- define "taskflow-lib.pdb" -}}
{{- if .Values.pdb.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "taskflow-lib.name" . }}
  labels:
    {{- include "taskflow-lib.labels" . | nindent 4 }}
spec:
  minAvailable: {{ .Values.pdb.minAvailable }}
  selector:
    matchLabels:
      {{- include "taskflow-lib.selectorLabels" . | nindent 6 }}
{{- end }}
{{- end -}}

{{/* ServiceMonitor (Prometheus Operator CRD): scrape /metrics every 15 s. */}}
{{- define "taskflow-lib.servicemonitor" -}}
{{- if and (not .Values.serviceMonitor.disabled) .Values.global.serviceMonitors }}
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: {{ include "taskflow-lib.name" . }}
  labels:
    {{- include "taskflow-lib.labels" . | nindent 4 }}
    release: observability
spec:
  selector:
    matchLabels:
      {{- include "taskflow-lib.selectorLabels" . | nindent 6 }}
  endpoints:
    - port: http
      path: {{ .Values.serviceMonitor.path }}
      interval: 15s
{{- end }}
{{- end -}}

{{/*
NetworkPolicy, on top of the namespace's default-deny (umbrella chart).
Ingress: only the Pods named in networkPolicy.from (plus Prometheus for
scraping). Egress: DNS, the shared infrastructure the service uses, the
etcd registry, the OpenTelemetry Collector, and only the services named in
networkPolicy.to.
*/}}
{{- define "taskflow-lib.networkpolicy" -}}
{{- if .Values.global.networkPolicies }}
{{- $np := .Values.networkPolicy }}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "taskflow-lib.name" . }}
  labels:
    {{- include "taskflow-lib.labels" . | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      {{- include "taskflow-lib.selectorLabels" . | nindent 6 }}
  policyTypes: [Ingress, Egress]
  ingress:
    {{- if $np.fromIngressController }}
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ingress-nginx
      ports:
        - port: {{ .Values.port }}
    {{- end }}
    {{- if $np.from }}
    - from:
        {{- range $np.from }}
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: {{ . }}
        {{- end }}
      ports:
        - port: {{ .Values.port }}
    {{- end }}
    - from:   # Prometheus scrapes /metrics
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ .Values.global.observabilityNamespace | default "observability" }}
      ports:
        - port: {{ .Values.port }}
  egress:
    - to:     # cluster DNS
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - {port: 53, protocol: UDP}
        - {port: 53, protocol: TCP}
    - to:     # traces to the OpenTelemetry Collector
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ .Values.global.observabilityNamespace | default "observability" }}
      ports:
        - port: 4317
    {{- $infra := list "etcd" }}
    {{- if .Values.database.enabled }}{{ $infra = append $infra "postgres" }}{{ end }}
    {{- if .Values.nats }}{{ $infra = append $infra "nats" }}{{ end }}
    {{- if .Values.redis }}{{ $infra = append $infra "redis" }}{{ end }}
    {{- if .Values.objectStore }}{{ $infra = append $infra "objectstore" }}{{ end }}
    - to:     # shared infrastructure this service uses
        {{- range $infra }}
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: {{ . }}
        {{- end }}
    {{- if $np.to }}
    - to:     # services this service calls
        {{- range $np.to }}
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: {{ . }}
        {{- end }}
      ports:
        - port: 8000
    {{- end }}
{{- end }}
{{- end -}}

{{/* The single entry point every service chart calls. */}}
{{- define "taskflow-lib.all" -}}
{{ include "taskflow-lib.serviceaccount" . }}
---
{{ include "taskflow-lib.deployment" . }}
---
{{ include "taskflow-lib.service" . }}
{{- with (include "taskflow-lib.hpa" . | trim) }}
---
{{ . }}
{{- end }}
{{- with (include "taskflow-lib.pdb" . | trim) }}
---
{{ . }}
{{- end }}
{{- with (include "taskflow-lib.servicemonitor" . | trim) }}
---
{{ . }}
{{- end }}
{{- with (include "taskflow-lib.networkpolicy" . | trim) }}
---
{{ . }}
{{- end }}
{{- end -}}
