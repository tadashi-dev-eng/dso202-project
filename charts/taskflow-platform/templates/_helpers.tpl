{{- define "platform.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: taskflow
app.kubernetes.io/component: infrastructure
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
taskflow.io/environment: {{ .root.Values.environment }}
{{- end -}}

{{- define "platform.selector" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: taskflow
{{- end -}}

{{/* Restricted container security context for images that allow it. */}}
{{- define "platform.containerSecurity" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: [ALL]
{{- end -}}
