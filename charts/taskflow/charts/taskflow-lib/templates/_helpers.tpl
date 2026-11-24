{{/*
charts/taskflow-lib/templates/_helpers.tpl -- naming and label helpers.

The object name is the service name (e.g. "task-service"), NOT prefixed
with the release name: services find each other by these names (DNS, the
etcd registry, NetworkPolicy selectors), so they must be identical in every
environment. Environments are separated by namespace instead.
*/}}

{{- define "taskflow-lib.name" -}}
{{- .Values.nameOverride | default .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "taskflow-lib.selectorLabels" -}}
app.kubernetes.io/name: {{ include "taskflow-lib.name" . }}
app.kubernetes.io/part-of: taskflow
{{- end -}}

{{- define "taskflow-lib.labels" -}}
{{ include "taskflow-lib.selectorLabels" . }}
app.kubernetes.io/version: {{ include "taskflow-lib.tag" . | quote }}
app.kubernetes.io/component: {{ .Values.tier | default "application" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
taskflow.io/environment: {{ .Values.global.environment | default "dev" }}
{{- end -}}

{{/* Image tag: per-service override, then the release-wide tag. */}}
{{- define "taskflow-lib.tag" -}}
{{- .Values.image.tag | default .Values.global.imageTag | default .Chart.AppVersion -}}
{{- end -}}

{{- define "taskflow-lib.image" -}}
{{- $registry := .Values.global.imageRegistry | default "localhost:5001" -}}
{{- $repo := .Values.image.repository | default (printf "taskflow/%s" (include "taskflow-lib.name" .)) -}}
{{- printf "%s/%s:%s" $registry $repo (include "taskflow-lib.tag" .) -}}
{{- end -}}
