{{/*
charts/taskflow-lib/templates/_deployment.tpl -- the workload.

Production practices applied to every service:
* hardened Pod: non-root fixed UID, read-only root filesystem, no privilege
  escalation, all capabilities dropped, RuntimeDefault seccomp, and no
  Kubernetes API token mounted (no service talks to the API server);
* startup, readiness and liveness probes (readiness checks dependencies,
  liveness does not -- see taskflow_common/app.py);
* spread across zones and nodes, so losing a node never takes all replicas;
* a preStop pause so the Pod leaves the Service endpoints before it stops
  accepting connections (no dropped requests during a rollout or drain);
* surge-only rolling updates (maxUnavailable 0);
* Downward API: POD_NAME / POD_IP / POD_NAMESPACE for the etcd registry,
  logs and trace attributes.
*/}}
{{- define "taskflow-lib.deployment" -}}
{{- $name := include "taskflow-lib.name" . -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $name }}
  labels:
    {{- include "taskflow-lib.labels" . | nindent 4 }}
spec:
  {{- if .Values.global.paused }}
  # global.paused: the environment is parked (0 Pods, data kept) to save
  # memory on the laptop cluster; set it to false to bring it back.
  replicas: 0
  {{- else if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
  revisionHistoryLimit: 5
  selector:
    matchLabels:
      {{- include "taskflow-lib.selectorLabels" . | nindent 6 }}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        {{- include "taskflow-lib.labels" . | nindent 8 }}
        {{- with .Values.podLabels }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      annotations:
        # Roll the Pods when the shared configuration changes.
        checksum/config: {{ .Values.global.config | toYaml | sha256sum | trunc 16 | quote }}
    spec:
      serviceAccountName: {{ $name }}
      automountServiceAccountToken: false
      enableServiceLinks: false
      terminationGracePeriodSeconds: 30
      securityContext:
        runAsNonRoot: true
        runAsUser: {{ .Values.securityContext.runAsUser }}
        runAsGroup: {{ .Values.securityContext.runAsUser }}
        fsGroup: {{ .Values.securityContext.runAsUser }}
        seccompProfile:
          type: RuntimeDefault
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              {{- include "taskflow-lib.selectorLabels" . | nindent 14 }}
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              {{- include "taskflow-lib.selectorLabels" . | nindent 14 }}
      affinity:
        # Prefer worker nodes; the control-plane node hosts the ingress
        # controller and the cluster's own components.
        nodeAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              preference:
                matchExpressions:
                  - key: taskflow.io/node-role
                    operator: In
                    values: [worker]
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 50
              podAffinityTerm:
                topologyKey: kubernetes.io/hostname
                labelSelector:
                  matchLabels:
                    {{- include "taskflow-lib.selectorLabels" . | nindent 20 }}
      containers:
        - name: {{ $name }}
          image: {{ include "taskflow-lib.image" . }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - name: http
              containerPort: {{ .Values.port }}
          {{- if .Values.command }}
          command: {{ toYaml .Values.command | nindent 12 }}
          {{- end }}
          envFrom:
            - configMapRef:
                name: {{ .Values.global.configMapName | default "taskflow-config" }}
          env:
            - name: SERVICE_NAME
              value: {{ $name }}
            - name: SERVICE_VERSION
              value: {{ include "taskflow-lib.tag" . | quote }}
            - name: POD_NAME
              valueFrom: {fieldRef: {fieldPath: metadata.name}}
            - name: POD_IP
              valueFrom: {fieldRef: {fieldPath: status.podIP}}
            - name: POD_NAMESPACE
              valueFrom: {fieldRef: {fieldPath: metadata.namespace}}
            {{- with .Values.database }}
            {{- if .enabled }}
            - name: DB_NAME
              value: {{ .name }}
            - name: DB_USER
              value: {{ .name }}
            - name: DB_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ $.Values.global.dbSecretName | default "taskflow-db" }}
                  key: {{ .name }}
            {{- end }}
            {{- end }}
            {{- if .Values.jwt }}
            - name: JWT_SECRET
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.global.jwtSecretName | default "taskflow-jwt" }}
                  key: secret
            {{- end }}
            {{- if .Values.objectStore }}
            - name: S3_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.global.s3SecretName | default "taskflow-objectstore" }}
                  key: accessKey
            - name: S3_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.global.s3SecretName | default "taskflow-objectstore" }}
                  key: secretKey
            {{- end }}
            {{- range $k, $v := .Values.env }}
            - name: {{ $k }}
              value: {{ $v | quote }}
            {{- end }}
          {{- with .Values.probes }}
          startupProbe:
            httpGet: {path: {{ .livenessPath }}, port: http}
            periodSeconds: 2
            failureThreshold: 60
          readinessProbe:
            httpGet: {path: {{ .readinessPath }}, port: http}
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 3
          livenessProbe:
            httpGet: {path: {{ .livenessPath }}, port: http}
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
          {{- end }}
          lifecycle:
            preStop:
              # Give kube-proxy and the gateway's registry cache time to stop
              # routing here before the process receives SIGTERM.
              sleep:
                seconds: 5
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir:
            sizeLimit: 64Mi
{{- end -}}
