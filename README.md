# gitops-deploy

`eks-infra` builds a cluster. Nothing deployed to it. This is the layer that
does: one Helm chart for the four long running services on this account, two
Kustomize overlays for the things that exist once per namespace, and the Argo CD
objects that generate ten Applications from eight values files.

The chart is small. What took the time is the ten things it refuses to render
and the 60 rules that check the renders against each other, because every one of
them is a manifest that applies cleanly and then goes wrong at the next node
drain, the next traffic peak, or the next rollback.

```
charts/service/            one chart, four services, ten refusals
envs/<env>/<service>.yaml  the numbers, per environment
envs/<env>/cluster.yaml    node pools and the headroom rule
overlays/<env>/            namespace, quota, LimitRange, NetworkPolicy, priorities
argocd/                    one project, two ApplicationSets, one root Application
gitopsvalidate/            the checks a template cannot make
docs/canary.md             promotion gates, and what the mechanism cannot see
docs/rollback.md           the release where there is no rollback
```

Nothing here has talked to a cluster. Every number below is computed from the
files in this repo by `python -m gitopsvalidate`, or is labelled as a budget
that was chosen.

## The four things that break

### A PodDisruptionBudget that blocks a drain

`minAvailable` set to the replica count permits zero evictions. A node drain
then waits forever, and the cluster upgrade stops on the first node holding one
of those pods. It reads as the cautious setting.

The chart only writes `maxUnavailable`, and it refuses to write one at all
unless the **stable track** has at least two pods. That last word is the fix,
not the rule. The first version checked `.Values.replicas`, which passed
`rag-agent` in staging: two replicas with a 50% canary leaves one pod on the
stable track, and the budget selects the stable track.

```
pdb.enabled with 1 pod(s) on the stable track (replicas minus the 50% canary):
a PodDisruptionBudget that covers one pod allows no voluntary eviction, so a
node drain blocks until someone deletes the PDB by hand
```

Where the HPA owns the count, the number checked is `minReplicas`, because that
is the floor a drain at 4am can rely on. Both `model-serving` and
`hybrid-search` have `minReplicas: 1` in staging and therefore no budget.

### Resource requests that make a cluster unschedulable

Worst case is every Deployment at its ceiling, plus the pod each rolling update
surges, plus every migration Job. For prod:

```
                                       pods    cores     memory
  model-serving                           9     8.10       27Gi
  rag-agent                              10     5.00       10Gi
  hybrid-search                           9     4.50       18Gi
  embedding-worker                        4     3.00        8Gi
  rag-agent-canary                        2     1.00        2Gi
  rag-agent-migrate-4f1c2ba9e07d          1     0.50      512Mi
  hybrid-search-migrate-9b0a73e5c1d4      1     0.50      512Mi
  worst case                             36    22.60       66Gi
  allocatable, untainted pools                 29.20      112Gi
  schedulable, 15% held back                   24.82    97484Mi
  worst case as a share of allocatable         77.4%      58.9%
  excluded, taint not tolerated                45.60      360Gi
```

That last line is the part worth arguing about. The spark pool is three
`r6i.4xlarge`, 45.6 cores and 360Gi, tainted `workload=spark:NoSchedule` by
`eks-infra`. Nothing in this repo tolerates it, so none of it is reachable.
Counting it was the first version of `schedulable()` and it made prod look 29%
utilised instead of 77%.

Raising `model-serving`'s `maxReplicas` from 8 to 11 adds 2.7 cores and puts the
worst case past the headroom. `make validate` fails, and a test asserts both
sides of that boundary.

The namespace quota is bracketed from both directions. Below the worst case and
a normal rollout is rejected at the quota: the surge pod is never created, the
Deployment reports `FailedCreate`, and the sync sits Progressing with no failed
object to look at. Above what the pools can place and the quota admits pods the
cluster has nowhere to put. Prod's `requests.cpu: "24"` sits between 22.60 and
24.82.

### A readiness probe that passes before the app can serve

`/healthz` answers from the HTTP framework. `/readyz` answers after the
connection pool is warm and the model is loaded. One endpoint cannot do both,
so the chart refuses a values file where the two paths are equal.

The budgets are also ordered, which is the part that gets missed:

```
readiness   5s x 3  =  15s before the pod leaves the Service
liveness   10s x 6  =  60s before the kubelet restarts it
```

Liveness has to be the longer of the two. Invert them and a slow database
restarts every pod at once, turning a degraded service into no service, and
each restart also loses the warm cache. `check_probes` fails the build when
`liveness period x failureThreshold` is not above readiness's.

A `startupProbe` owns the boot window, 240 seconds for `model-serving`, so both
other probes run at `initialDelaySeconds: 0`. An initial delay on top of a
startup probe is the same wait counted twice, and the pod sits un-Ready after it
is already answering. `progressDeadlineSeconds` has to clear boot plus
`minReadySeconds`, or Argo CD reports the Application Degraded while the rollout
is still working, which teaches people to ignore a Degraded Application.

### A rollback that cannot work

Each service declares what its migration does:

| `migration.strategy` | reverting the commit |
|---|---|
| `none` | works |
| `expand` | works, and the added column stays |
| `contract` | does not work |

A `contract` migration has dropped something the previous image reads. Argo CD
will happily roll the Deployment back; the pods start, pass liveness, and fail
every query that touches it. The repo cannot prevent that, so it marks it:

- the Deployment gets `deploy.sam-farooq.dev/rollback: manual` and a reason
- the chart refuses to canary it, because the stable track is running the image
  that can no longer read the schema, so there is no fallback
- the Application for that service has **no `automated` block at all**. Two of
  the ten are manual: `hybrid-search` in prod and in staging. An automated sync
  with self-heal would drop a column the moment the commit landed, at whatever
  hour it landed

`docs/rollback.md` is the path out, and it is forward every time.

## Sync waves

```
wave 0   ServiceAccount, ConfigMap, Service
wave 1   migration Job, named for its content hash
wave 2   Deployment, and the canary Deployment
wave 3   PodDisruptionBudget, HorizontalPodAutoscaler
```

Argo CD holds a wave until it reports healthy, and a Job is healthy when it is
Complete, so the migration finishes before the new image starts. The HPA is
last because an HPA applied before its target logs a missing scale target on
every reconcile. `check_sync_waves` reads the annotations off the rendered
objects and asserts the ordering rather than trusting it.

The Job's name carries the first twelve characters of a sha256 over the
migration directory. A Job's pod template is immutable, so re-applying an
edited Job under the same name fails the sync with `field is immutable` and
nothing in the message mentions migrations.

**What that costs:** edit a migration back to earlier content and the completed
Job for that hash is still in the namespace, so it does not run. Reverting a
migration means writing a new migration.

`ttlSecondsAfterFinished` must never appear on these Jobs. The TTL controller
deletes the finished Job, Argo CD finds it missing on the next sync, re-creates
it, and the migration runs twice. A rule fails the build if it is set.

## The canary, and what it cannot see

Two Deployments behind one Service. Both carry the Service's selector labels and
differ in a track label, an image tag and a replica count. Traffic splits in
proportion to ready pods, which is what a ClusterIP Service does. No sidecar, no
mesh, no second controller.

So the weight is a pod count, and the smallest step is `100 / replicas`:

| environment | replicas | smallest weight | configured | canary pods |
|---|---|---|---|---|
| prod | 10 | 10% | 10% | 1 |
| staging | 2 | 50% | 50% | 1 |

`canary.weight: 5` at ten replicas is refused rather than rounded. Go templates
divide integers, so `div (mul 10 5) 100` is 0: the canary would render with zero
replicas and the rollout would report success having routed nothing to the new
image.

The four promotion gates are in `docs/canary.md` as PromQL against the metric
names `telemetry-stack` exports. The fourth is the one that makes the others
honest: at least 2,000 canary requests inside the window. Against a 1% baseline
error rate the standard error over 2,000 samples is
`sqrt(0.01 * 0.99 / 2000) = 0.00222`, so a doubling to 2% sits 4.5 standard
errors out and is distinguishable from noise. Over 200 samples the same doubling
is 1.4 standard errors, which is a coin toss.

**Three things this gives up, and they are not small:**

1. **Nothing watches the gates.** No controller evaluates the queries or scales
   the canary down when one fails. Argo Rollouts and Flagger both do exactly
   that, and neither is here. The gates are written down and applied by a human.
2. **The real weight is an upper bound, not a measurement.** The split is per
   connection. A caller holding keep-alive sockets to stable pods may never
   reach the canary at all. One pod in ten caps canary traffic at 10%; it does
   not deliver it. Gate 4 is what catches a canary being bypassed.
3. **No sub-10% weight in prod** without twenty replicas. That is the reason to
   reach for a mesh, and the reason not to is that a mesh is a second control
   plane to run for a traffic split that a replica count already expresses.

An HPA and a canary cannot share a Deployment: the scaler moves the stable count
during the analysis window, so the weight the report was written against is
gone. The chart refuses the pair, which is why `rag-agent` has a canary and a
fixed count while `model-serving` and `hybrid-search` autoscale and have none.

## Helm for one half, Kustomize for the other

`charts/service` is Helm because it repeats four times with different numbers.
The namespace, its `ResourceQuota`, its `LimitRange`, the default-deny
`NetworkPolicy` and the two `PriorityClass` objects exist once per cluster, so
they are plain manifests with two overlays. Templating them would buy a variable
and lose the ability to read the file.

The cost is two tools and two mental models in one repo, and a reviewer needs
both. The alternative was one `helm` chart with a `global.environment` value
and an `if` around every cluster-scoped object, which is how a chart ends up
rendering a `PriorityClass` eight times.

The two halves are joined by arithmetic: `check_quota` compares the overlay's
`ResourceQuota` against the sum of the Helm renders, and `check_limit_range`
compares the overlay's `max` against the largest single container request.

`batch` priority is `preemptionPolicy: Never`. A migration that evicts the pods
it is migrating for has turned a deploy into an outage. **What that costs:**
under real pressure the migration stays Pending and the wave stalls, which is
the failure that can be read off `kubectl get pods` instead of a graph.

## The ApplicationSet, and why there is a templatePatch

Two ApplicationSets generate ten Applications. `spec.template` is structured
YAML, so no field in it can be conditional: there is no way to write "this
Application has an `ignoreDifferences` block and that one does not".
`templatePatch` is a string, which is why it can, and the result is merged onto
the rendered template.

```yaml
templatePatch: |
  spec:
    revisionHistoryLimit: 5
    {{- if .hpa }}
    ignoreDifferences: ...
    {{- end }}
```

`revisionHistoryLimit` is unconditional on purpose. A patch where every key sits
behind an `if` renders as a bare `spec:` when no branch fires, and a merge patch
of `spec: null` empties the Application.

Because the patch is a string, the only thing that renders it is Argo CD's own
controller. `gitopsvalidate/gotemplate.py` is a 142 line evaluator for the
subset used here, `{{ .field }}`, `if`, `else`, `end` and the trim markers, and
it raises on anything else. So the eight Applications are produced offline and
the rules assert on real objects instead of on text a regex is hopeful about. A
`range` or a pipeline added to the patch breaks the evaluator rather than
quietly being approximated.

Two Argo CD details that are easy to get wrong and are both asserted:

- **`RespectIgnoreDifferences=true`.** Without it, `ignoreDifferences` only
  hides the diff in the UI and the sync still applies the committed value. The
  HPA keeps being overwritten and nothing says so, which is worse than the
  visible version.
- **`spec.replicas` is omitted from the Deployment entirely** when the HPA is
  on. That is the actual fix; `ignoreDifferences` is for the window after
  somebody has run `kubectl scale` by hand during an incident.

The list generator duplicates two booleans that are already in the values files.
`check_applications` cross-checks them, so an element claiming `hpa: false` for
a service with `autoscaling.enabled: true` fails the build. A git file generator
reading the values files would remove the duplication and put a template inside
a template for two booleans, which was not worth it at eight elements.

## Running it

Both binaries render locally. Nothing reads a kubeconfig, contacts a cluster or
needs a credential, which is what makes all of this run on a pull request from a
fork.

```bash
pip install -e ".[dev]"

make tools              # helm and kustomize on PATH
make lint               # helm lint --strict, 8 combinations
make template build     # every chart and both overlays into rendered/
make validate           # the rules, the capacity table, the Argo CD checks
make test               # 257 tests, a missing binary is a failure
make check              # what CI runs
```

`make test` sets `GITOPS_REQUIRE_TOOLS=1`, which turns a missing `helm` into a
failure instead of a skip. Plain `pytest` skips the rendered assertions when the
binary is absent, which is convenient locally and would be a green tick meaning
nothing in CI.

CI runs two jobs. `python` has no helm and runs `pytest -m "not renders"` for a
fast failure. `manifests` installs helm 3.16.4 and kustomize 5.5.0, runs the
whole suite with the binaries required, then renders everything and checks each
document for `apiVersion`, `kind` and `metadata`.

## Where the numbers come from

`envs/<env>/cluster.yaml` declares each pool's allocatable CPU and memory. Those
are instance specs minus a flat reservation budgeted for the kubelet, the VPC
CNI and the node's system daemons. **They are a planning budget, not a reading
from a live node.** A node whose real allocatable is lower will place fewer pods
than the arithmetic promises, which is why the headroom is 15% and not 2%.

15% is a chosen number, not a derived one. It covers the reservation estimate
being wrong, one node being unavailable during a pool roll, and DaemonSets this
repo does not declare.

`tests/fixtures/baseline.yaml` was typed out, not captured. `tests/fixtures/README.md`
says so. The passing side of every rule is the real `helm template` output for
all eight combinations; the fixture exists so a test that breaks one field cannot
be passing because a second field was also wrong.

## Chart refusals, in one list

Ten conditions fail the render, in `charts/service/templates/_helpers.tpl`:

```
image.tag empty                      an untagged image resolves to latest
image.tag latest or main             two pods can run different code
canary + autoscaling                 the HPA moves the weight mid-analysis
canary + contract migration          the stable track is not a fallback
pdb over fewer than 2 stable pods    a node drain blocks forever
migration without a content hash     a Job's pod template is immutable
liveness path == readiness path      readiness proves only the process is up
weight not expressible in whole pods integer division makes it zero
weight rounding to zero pods         a canary with no canary in it
weight leaving no stable pods        nothing to fall back to
```

They live in `templates/guards.yaml`, which renders nothing and evaluates all of
them. The filename has no leading underscore for a reason: Helm treats
`templates/_*` as partials and never executes them, and as `_guards.yaml` every
one of these silently did nothing. `helm template` accepted `image.tag=latest`,
a canary beside a contract migration, and a budget over one pod. The sixteen
tests in `tests/test_guards.py` exist because the guards did not: thirteen assert
that a refusal fires, and three assert that a valid weight still renders.
