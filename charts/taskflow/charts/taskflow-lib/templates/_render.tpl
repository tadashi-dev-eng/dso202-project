{{/*
charts/taskflow-lib/templates/_render.tpl -- defaults + entry point.

A library chart's values.yaml is never merged into the charts that use it,
so the defaults live here as a template. "taskflow-lib.render" merges the
calling chart's values over these defaults and renders every object.
A service chart's whole template is therefore one line:

    {{ include "taskflow-lib.render" . }}

Booleans that default to true cannot be switched off through a merge
(Sprig's merge skips false values), so every per-service switch below
defaults to false/empty and is turned ON by the service that needs it.
*/}}
{{- define "taskflow-lib.defaults" -}}
global:
  environment: dev
  imageRegistry: localhost:5001
  imageTag: ""
  serviceMonitors: false
  networkPolicies: false
nameOverride: ""
tier: application
replicaCount: 1
image:
  repository: ""
  tag: ""
  pullPolicy: IfNotPresent
port: 8000
servicePort: 8000
command: []
securityContext:
  runAsUser: 10001
database:
  enabled: false
  name: ""
jwt: false
nats: false
redis: false
objectStore: false
env: {}
podLabels: {}
probes:
  livenessPath: /healthz
  readinessPath: /readyz
resources:
  requests:
    cpu: 20m
    memory: 96Mi
  limits:
    cpu: 500m
    memory: 256Mi
autoscaling:
  enabled: false
  minReplicas: 2
  maxReplicas: 6
pdb:
  enabled: false
  minAvailable: 1
serviceMonitor:
  disabled: false
  path: /metrics
networkPolicy:
  fromIngressController: false
  from: []
  to: []
{{- end -}}

{{- define "taskflow-lib.render" -}}
{{- $defaults := fromYaml (include "taskflow-lib.defaults" .) -}}
{{- $values := mustMergeOverwrite $defaults (deepCopy .Values) -}}
{{- $ctx := dict "Values" $values "Chart" .Chart "Release" .Release "Capabilities" .Capabilities "Template" .Template -}}
{{ include "taskflow-lib.all" $ctx }}
{{- end -}}
