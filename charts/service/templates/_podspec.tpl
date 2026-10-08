{{/*
The pod template, shared by the stable and canary Deployments. Called with a
dict: { "ctx": $, "track": "stable" | "canary", "tag": <image tag> }.

The canary differs in exactly two things, its track label and its image tag.
Anything else that differed would make the canary a test of the wrong thing.
*/}}
{{- define "service.podTemplate" -}}
{{- $ctx := .ctx -}}
{{- $v := $ctx.Values -}}
metadata:
  labels:
    {{- include "service.selectorLabels" $ctx | nindent 4 }}
    deploy.sam-farooq.dev/track: {{ .track }}
  annotations:
    # Rolls the pods when the ConfigMap changes. Without this, a config-only
    # commit syncs clean and changes nothing, because the ConfigMap is mounted
    # as env and env is read once at process start.
    deploy.sam-farooq.dev/config-checksum: {{ include (print $ctx.Template.BasePath "/configmap.yaml") $ctx | sha256sum | trunc 16 }}
spec:
  serviceAccountName: {{ include "service.fullname" $ctx }}
  priorityClassName: {{ $v.priorityClassName }}
  {{- if eq $v.kind "deployment" }}
  # kube-proxy removes the endpoint asynchronously. A process that exits the
  # instant it sees SIGTERM therefore drops requests that were routed to it
  # microseconds earlier. The sleep is the endpoint propagation window, and
  # terminationGracePeriodSeconds has to be longer than sleep plus the longest
  # request, or the kubelet kills the pod mid-drain.
  terminationGracePeriodSeconds: {{ $v.lifecycle.terminationGracePeriodSeconds }}
  {{- else }}
  # A worker finishes the message it is holding. The grace period is the
  # visibility timeout, not a guess.
  terminationGracePeriodSeconds: {{ $v.lifecycle.terminationGracePeriodSeconds }}
  {{- end }}
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    fsGroup: 10001
    seccompProfile:
      type: RuntimeDefault
  {{- if $v.topologySpread.enabled }}
  topologySpreadConstraints:
    - maxSkew: {{ $v.topologySpread.maxSkew }}
      topologyKey: topology.kubernetes.io/zone
      # ScheduleAnyway, not DoNotSchedule. Three zones and two replicas cannot
      # satisfy a hard skew of 1, and DoNotSchedule turns that arithmetic into
      # a pod that stays Pending with no obvious cause.
      whenUnsatisfiable: ScheduleAnyway
      labelSelector:
        matchLabels:
          {{- include "service.selectorLabels" $ctx | nindent 10 }}
  {{- end }}
  containers:
    - name: {{ include "service.name" $ctx }}
      image: "{{ $v.image.repository }}:{{ .tag }}"
      imagePullPolicy: {{ $v.image.pullPolicy }}
      {{- if eq $v.kind "deployment" }}
      ports:
        - name: {{ $v.service.targetPort }}
          containerPort: {{ $v.containerPort }}
          protocol: TCP
      {{- end }}
      envFrom:
        - configMapRef:
            name: {{ include "service.fullname" $ctx }}
      env:
        - name: DEPLOY_TRACK
          value: {{ .track | quote }}
        - name: POD_NAME
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
      resources:
        {{- include "service.resources" $v.resources | nindent 8 }}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      {{- if eq $v.kind "deployment" }}
      {{- if gt (int $v.probes.startupSeconds) 0 }}
      # The startup probe owns the boot window. With it, liveness and readiness
      # both start at zero delay: an initialDelaySeconds on top would be the
      # same wait counted twice, and the pod would sit un-Ready after it was
      # already answering.
      startupProbe:
        httpGet:
          path: {{ $v.probes.readiness }}
          port: {{ $v.service.targetPort }}
        periodSeconds: {{ $v.probes.startupPeriodSeconds }}
        failureThreshold: {{ div (int $v.probes.startupSeconds) (int $v.probes.startupPeriodSeconds) }}
      {{- end }}
      # Liveness asks whether the process is wedged. It must not check a
      # dependency: a slow database would otherwise restart every pod at once
      # and turn a degraded service into no service.
      livenessProbe:
        httpGet:
          path: {{ $v.probes.liveness }}
          port: {{ $v.service.targetPort }}
        initialDelaySeconds: 0
        periodSeconds: {{ $v.probes.livenessPeriodSeconds }}
        failureThreshold: {{ $v.probes.livenessFailureThreshold }}
      # Readiness asks whether this pod can serve, which includes its
      # dependencies. Its budget is deliberately shorter than liveness, so a
      # blip takes the pod out of rotation before the kubelet restarts it.
      readinessProbe:
        httpGet:
          path: {{ $v.probes.readiness }}
          port: {{ $v.service.targetPort }}
        initialDelaySeconds: 0
        periodSeconds: {{ $v.probes.readinessPeriodSeconds }}
        failureThreshold: {{ $v.probes.readinessFailureThreshold }}
      lifecycle:
        preStop:
          exec:
            command: ["/bin/sleep", "{{ $v.lifecycle.preStopSleepSeconds }}"]
      {{- else }}
      # Nothing routes to a worker, so readiness has no traffic meaning. It
      # still gates the rollout and is counted by the PodDisruptionBudget, so
      # it checks the broker connection rather than being left off.
      livenessProbe:
        exec:
          command: ["/app/bin/heartbeat-age", "--max-seconds", "{{ $v.probes.heartbeatMaxSeconds }}"]
        periodSeconds: {{ $v.probes.livenessPeriodSeconds }}
        failureThreshold: {{ $v.probes.livenessFailureThreshold }}
      readinessProbe:
        exec:
          command: ["/app/bin/broker-connected"]
        periodSeconds: {{ $v.probes.readinessPeriodSeconds }}
        failureThreshold: {{ $v.probes.readinessFailureThreshold }}
      {{- end }}
      volumeMounts:
        - name: tmp
          mountPath: /tmp
  volumes:
    # readOnlyRootFilesystem is only survivable if the process has somewhere to
    # write. Every one of these four writes a temp file.
    - name: tmp
      emptyDir:
        sizeLimit: {{ $v.tmpSizeLimit }}
{{- end -}}
