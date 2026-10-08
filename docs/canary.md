# Canary: what promotes it, what aborts it, and what it cannot see

The mechanism is two Deployments behind one Service. Both carry the Service's
selector labels; they differ in a `deploy.sam-farooq.dev/track` label, their
image tag, and their replica count. Traffic splits in proportion to ready pods,
because that is what a ClusterIP Service does.

Nothing else is involved. No sidecar, no mesh, no second controller.

## The weight is a pod count, so it is not continuous

```
weight achievable = canaryPods / replicas, canaryPods an integer >= 1
smallest step     = 100 / replicas
```

| environment | replicas | smallest weight | configured | canary pods |
|---|---|---|---|---|
| prod | 10 | 10% | 10% | 1 |
| staging | 2 | 50% | 50% | 1 |

`canary.weight: 5` at ten replicas is refused. Go templates divide integers, so
`div (mul 10 5) 100` is 0: the canary Deployment would render with zero
replicas, the rollout would report success, and nothing would have reached the
new image. The chart fails the render instead and names the smallest step.

Five percent in prod needs twenty replicas. That is the cost of this mechanism
and it is the whole reason to reach for a mesh, which is not here.

## Promotion gates

All four, evaluated over `canary.analysisWindow`. The metric names are the
ones `telemetry-stack` exports. None of these queries has been run from here:
they are the gate, written down, and nothing in this repo or the cluster
evaluates them.

```promql
# 1. errors. The canary may not be worse by more than half a point.
  sum(rate(http_requests_total{track="canary",status=~"5.."}[20m]))
/ sum(rate(http_requests_total{track="canary"}[20m]))
-
  sum(rate(http_requests_total{track="stable",status=~"5.."}[20m]))
/ sum(rate(http_requests_total{track="stable"}[20m]))
< 0.005

# 2. tail latency. Within 1.2x of stable, not of an absolute target: an
#    absolute number passes a canary deployed during a quiet hour.
  histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{track="canary"}[20m])))
/ histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{track="stable"}[20m])))
< 1.2

# 3. no restarts on the canary track.
sum(increase(kube_pod_container_status_restarts_total{track="canary"}[20m])) == 0

# 4. enough requests for the first two to mean anything.
sum(increase(http_requests_total{track="canary"}[20m])) >= 2000
```

### Why 2,000

Gate 4 is the one that gets dropped and it is the one that makes the others
honest. Against a 1% baseline error rate, the standard error of a proportion
over 2,000 samples is

```
sqrt(0.01 * 0.99 / 2000) = 0.00222
```

so a doubling to 2% sits 4.5 standard errors away and is distinguishable from
noise. Over 200 samples the same doubling is 1.4 standard errors, which is a
coin toss. Below 2,000 canary requests the correct action is to widen the
window, not to promote.

At a 10% weight, 2,000 requests inside twenty minutes needs the service to be
handling at least 16.7 requests per second overall. Twenty minutes is 1,200
seconds, so 2,000 requests is 1.7 per second on the canary track, and the
canary track is a tenth of the traffic. A canary started at 3am against a
service that is quiet overnight does not meet gate 4 and must not be promoted
on the strength of gates 1 to 3 passing.

## Aborting

Scale the canary Deployment to zero. Every ready pod is then on the stable
track and the Service sends it everything.

```bash
kubectl -n platform-prod scale deploy/rag-agent-canary --replicas=0
```

That is a manual edit, so Argo CD's self-heal will undo it on the next
reconcile. It buys minutes. The durable abort is reverting the commit that set
`canary.enabled: true`, which is what should follow immediately.

## Promoting

One commit to `envs/<env>/<service>.yaml`:

```yaml
image:
  tag: "1.10.0-rc2"     # was the canary tag
canary:
  enabled: false
```

The stable Deployment rolls to the new tag with `maxUnavailable: 0`, the canary
Deployment is pruned, and `charts/service` refuses the next canary until a new
`canary.tag` is set.

## What this does not do

**Nothing watches the gates.** No controller in the cluster evaluates the four
queries or scales the canary down when one fails. They are written here and a
human or a CI job applies them. Argo Rollouts and Flagger both automate exactly
this, and neither is in this repo: adding a controller to automate a judgement
nobody has yet made by hand is how you end up with an automatic promotion
nobody trusts.

**The real weight can be nowhere near 10%.** The split is per connection, not
per request. A client holding a keep-alive connection to a stable pod never
reaches the canary at all, and a connection-pooled caller with ten warm
sockets may hold none to the canary pod. One pod in ten is an upper bound on
canary traffic, not a measurement of it. Gate 4 is what catches this: a canary
that is being bypassed does not reach 2,000 requests.

**An HPA cannot be on.** The scaler moves the stable replica count during the
window, so the weight the report was written against no longer exists.
`charts/service` refuses the pair, which is why `model-serving` and
`hybrid-search` autoscale and have no canary, and `rag-agent` has a canary and
a fixed count.

**A contract migration cannot be canaried.** The stable track is running the
previous image against a schema that no longer has what it reads, so there is
nothing to fall back to. `docs/rollback.md` is that case.
