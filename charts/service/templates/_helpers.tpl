{{/*
Names. The chart is called "service" because it is the same chart for all of
them, so the chart name is left out of every object name: the usual
release-chart form would produce rag-agent-service, and a reviewer reading
`kubectl get deploy` wants to see rag-agent. The release name is the service
name, which is what the ApplicationSet sets it to.
*/}}
{{- define "service.name" -}}
{{- default .Release.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "service.fullname" -}}
{{- include "service.name" . -}}
{{- end -}}

{{/*
Selector labels. These are the only labels the Service matches on, so the
canary Deployment carries exactly these plus its own track label. Adding the
image tag here would split the Service in two the moment a canary existed,
which is the opposite of what a canary is for.
*/}}
{{- define "service.selectorLabels" -}}
app.kubernetes.io/name: {{ include "service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "service.labels" -}}
{{ include "service.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ .Values.partOf | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
deploy.sam-farooq.dev/environment: {{ .Values.environment | quote }}
{{- end -}}

{{/*
The container's resource block. Memory request equals its limit, deliberately.
CPU gets a request and no limit on anything that serves a request, and both on
anything batch. The reason is in docs/canary.md and a test asserts the split,
so changing the policy means changing the test on purpose.
*/}}
{{- define "service.resources" -}}
requests:
  cpu: {{ .cpu }}
  memory: {{ .memory }}
limits:
  memory: {{ .memory }}
{{- if .cpuLimit }}
  cpu: {{ .cpuLimit }}
{{- end }}
{{- end -}}

{{/*
Number of canary pods, as an integer, or a failure.

Go templates do integer division, so `div (mul 10 5) 100` is 0 and a 5% canary
at ten replicas silently becomes no canary at all: the rollout reports success
having routed nothing to the new image. Refuse instead of rounding.
*/}}
{{- define "service.canaryPods" -}}
{{- $total := int .Values.replicas -}}
{{- $weight := int .Values.canary.weight -}}
{{- $product := mul $total $weight -}}
{{- if ne (mod $product 100) 0 -}}
{{- fail (printf "canary.weight %d%% is not expressible with %d replicas: the smallest step is %.4g%%. Pick a weight that divides into whole pods." $weight $total (divf 100.0 (float64 $total))) -}}
{{- end -}}
{{- $pods := div $product 100 -}}
{{- if lt $pods 1 -}}
{{- fail (printf "canary.weight %d%% of %d replicas rounds to zero pods" $weight $total) -}}
{{- end -}}
{{- if ge $pods $total -}}
{{- fail (printf "canary.weight %d%% of %d replicas leaves no stable pods" $weight $total) -}}
{{- end -}}
{{- $pods -}}
{{- end -}}

{{/*
Every refusal the chart makes, in one place, evaluated before anything renders.
A guard that lives next to the resource it guards gets skipped whenever that
resource is switched off.
*/}}
{{- define "service.guards" -}}
{{- if not .Values.image.tag -}}
{{- fail "image.tag is required: an untagged image resolves to latest" -}}
{{- end -}}
{{- if or (eq .Values.image.tag "latest") (eq .Values.image.tag "main") -}}
{{- fail (printf "image.tag %q is a moving tag. Two pods of the same Deployment can then run different code, and a rollback has nothing to roll back to." .Values.image.tag) -}}
{{- end -}}
{{- if and .Values.canary.enabled .Values.autoscaling.enabled -}}
{{- fail "canary.enabled and autoscaling.enabled are mutually exclusive: the HPA changes the stable replica count, which moves the canary weight underneath the analysis window" -}}
{{- end -}}
{{- if and .Values.canary.enabled (eq .Values.migration.strategy "contract") -}}
{{- fail "a contract migration cannot be canaried: the previous image can no longer read the schema, so the stable track is not a fallback" -}}
{{- end -}}
{{- if .Values.pdb.enabled -}}
{{/*
The budget selects the stable track, not every pod of the service, so the
count that matters is the stable track's and that is not .Values.replicas.
With a canary it is replicas minus the canary's share; with an HPA it is
minReplicas, because that is the floor a drain can meet.
*/}}
{{- $stable := int .Values.replicas -}}
{{- $why := "replicas" -}}
{{- if .Values.autoscaling.enabled -}}
{{- $stable = int .Values.autoscaling.minReplicas -}}
{{- $why = "autoscaling.minReplicas" -}}
{{- else if .Values.canary.enabled -}}
{{- $stable = sub (int .Values.replicas) (int (include "service.canaryPods" .)) -}}
{{- $why = printf "replicas minus the %d%% canary" (int .Values.canary.weight) -}}
{{- end -}}
{{- if lt $stable 2 -}}
{{- fail (printf "pdb.enabled with %d pod(s) on the stable track (%s): a PodDisruptionBudget that covers one pod allows no voluntary eviction, so a node drain blocks until someone deletes the PDB by hand" $stable $why) -}}
{{- end -}}
{{- end -}}
{{- if and .Values.migration.enabled (not .Values.migration.sha) -}}
{{- fail "migration.sha is required when migration.enabled: a Job's pod template is immutable, so the Job name has to change when the migration does" -}}
{{- end -}}
{{- if eq .Values.kind "deployment" -}}
{{- if eq .Values.probes.liveness .Values.probes.readiness -}}
{{- fail (printf "probes.liveness and probes.readiness are both %q: one endpoint cannot answer both questions, and a readiness check that only proves the process is up sends traffic to a pod with a cold connection pool" .Values.probes.liveness) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Sync waves. Argo CD applies a wave, waits for it to report healthy, then moves
on. The numbers are here rather than inline so the ordering is one list to read.
*/}}
{{- define "service.wave" -}}
{{- $waves := dict "config" "0" "service" "0" "migration" "1" "workload" "2" "governor" "3" -}}
{{- get $waves . -}}
{{- end -}}
