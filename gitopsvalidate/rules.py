"""The invariants, as functions over rendered manifests.

Every rule here is a thing that renders fine, applies fine, and then goes wrong
later: at the next node drain, the next traffic peak, the next rollback. None
of them are schema errors, which is why a schema validator does not find them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .quantity import cpu_millis, format_cpu, format_memory, memory_bytes
from .workload import Schedulable, Totals, container_requests

MOVING_TAGS = frozenset({"latest", "main", "master", "edge", "stable"})
SERVING_PRIORITY = "serving"
BATCH_PRIORITY = "batch"


@dataclass(frozen=True)
class Finding:
    rule: str
    target: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.target}: {self.message}"


def _pod_templates(docs: list[dict]) -> list[tuple[dict, dict]]:
    """(owner, podTemplate) for every controller that has one."""
    out = []
    for doc in docs:
        if doc.get("kind") in ("Deployment", "Job"):
            out.append((doc, doc["spec"]["template"]))
    return out


def _name(doc: dict) -> str:
    return doc.get("metadata", {}).get("name", "<unnamed>")


def _selector_matches(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(key) == value for key, value in selector.items())


def check_resources(docs: list[dict]) -> list[Finding]:
    """Requests present, memory request equal to its limit, CPU limits by kind.

    A memory limit above its request makes the pod Burstable: it is allowed to
    grow into memory the node did not reserve, and the node reclaims it by
    killing whatever is handy. Equal request and limit is Guaranteed for
    memory, and the pod that exceeds its own number is the one that dies.

    CPU is the other way round. A CPU limit is a CFS quota, and a pod throttled
    for 20ms of a 100ms period adds that to the tail of every request inside
    it. Serving work gets a request and no limit. Batch work gets both, because
    a migration that eats the node is worse than a migration that takes longer.
    """
    findings = []
    for owner, template in _pod_templates(docs):
        kind = owner["kind"]
        for container in template["spec"].get("containers", []):
            where = f"{kind}/{_name(owner)}/{container['name']}"
            resources = container.get("resources", {})
            requests = resources.get("requests", {})
            limits = resources.get("limits", {})
            if "cpu" not in requests:
                findings.append(
                    Finding(
                        "resources.cpu-request",
                        where,
                        "no CPU request, so the LimitRange default decides it and "
                        "the quota counts a number nobody chose",
                    )
                )
            if "memory" not in requests or "memory" not in limits:
                findings.append(
                    Finding("resources.memory", where, "memory needs both a request and a limit")
                )
            elif memory_bytes(requests["memory"]) != memory_bytes(limits["memory"]):
                findings.append(
                    Finding(
                        "resources.memory-guaranteed",
                        where,
                        f"memory request {requests['memory']} and limit {limits['memory']} "
                        "differ, which makes the pod Burstable and evictable",
                    )
                )
            if kind == "Deployment" and "cpu" in limits:
                findings.append(
                    Finding(
                        "resources.cpu-limit",
                        where,
                        f"CPU limit {limits['cpu']} on request-serving work throttles the "
                        "tail latency it is meant to protect",
                    )
                )
            if kind == "Job" and "cpu" not in limits:
                findings.append(
                    Finding(
                        "resources.cpu-limit",
                        where,
                        "batch work needs a CPU limit; it has no tail latency to protect "
                        "and it will take whatever the node has",
                    )
                )
    return findings


def check_image_tags(docs: list[dict]) -> list[Finding]:
    findings = []
    for owner, template in _pod_templates(docs):
        for container in template["spec"].get("containers", []):
            image = container.get("image", "")
            where = f"{owner['kind']}/{_name(owner)}/{container['name']}"
            tag = image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else ""
            if not tag:
                findings.append(Finding("image.tag", where, f"{image!r} has no tag"))
            elif tag in MOVING_TAGS:
                findings.append(
                    Finding(
                        "image.tag",
                        where,
                        f"{tag!r} moves, so two pods of one Deployment can run different "
                        "code and a rollback has no target",
                    )
                )
    return findings


def check_disruption_budgets(docs: list[dict]) -> list[Finding]:
    """A PDB has to permit at least one eviction, or a node drain never ends.

    The shape that causes it is minAvailable set to the replica count, or any
    PDB over a single replica. Both read as cautious. Both mean the cluster
    upgrade stops on the first node holding one of these pods, and the only way
    forward is deleting the PDB during the maintenance window.
    """
    findings = []
    deployments = {_name(doc): doc for doc in docs if doc.get("kind") == "Deployment"}
    for pdb in [doc for doc in docs if doc.get("kind") == "PodDisruptionBudget"]:
        where = f"PodDisruptionBudget/{_name(pdb)}"
        spec = pdb["spec"]
        selector = spec.get("selector", {}).get("matchLabels", {})
        targets = [
            doc
            for doc in deployments.values()
            if _selector_matches(selector, doc["spec"]["template"]["metadata"]["labels"])
        ]
        if not targets:
            findings.append(Finding("pdb.selector", where, "selects no Deployment in this render"))
            continue
        for deployment in targets:
            if "replicas" not in deployment["spec"]:
                # An HPA owns the count, so there is no committed number here.
                continue
            replicas = int(deployment["spec"]["replicas"])
            if spec.get("minAvailable") is not None:
                min_available = spec["minAvailable"]
                if isinstance(min_available, str) and min_available.endswith("%"):
                    allowed = replicas - (replicas * int(min_available[:-1]) + 99) // 100
                else:
                    allowed = replicas - int(min_available)
                if allowed < 1:
                    findings.append(
                        Finding(
                            "pdb.blocks-drain",
                            where,
                            f"minAvailable {min_available} of {replicas} replicas permits "
                            f"{allowed} evictions, so a node drain blocks forever",
                        )
                    )
            if spec.get("maxUnavailable") is not None:
                max_unavailable = int(spec["maxUnavailable"])
                if max_unavailable < 1:
                    findings.append(
                        Finding(
                            "pdb.blocks-drain",
                            where,
                            "maxUnavailable 0 permits no eviction at all",
                        )
                    )
                elif replicas - max_unavailable < 1:
                    findings.append(
                        Finding(
                            "pdb.permits-everything",
                            where,
                            f"maxUnavailable {max_unavailable} of {replicas} replicas allows "
                            "the whole service to be drained at once",
                        )
                    )
            if replicas < 2:
                findings.append(
                    Finding(
                        "pdb.single-replica",
                        where,
                        f"{_name(deployment)} has {replicas} replica, and no budget over one "
                        "replica can allow an eviction",
                    )
                )
    return findings


def check_probes(docs: list[dict]) -> list[Finding]:
    """Readiness and liveness have to be asking different questions.

    One endpoint cannot answer both. A liveness probe that checks the database
    restarts every pod at once when the database is slow, turning a degraded
    service into no service. A readiness probe that only proves the process
    started sends traffic to a pod with a cold connection pool, which is the
    original complaint: the probe passed and the first hundred requests 500ed.

    The budgets also have to be ordered. readiness period times failureThreshold
    is how long a bad pod keeps receiving traffic; liveness period times
    failureThreshold is how long before the kubelet restarts it. If liveness is
    the shorter of the two, a dependency blip restarts the pod instead of taking
    it out of rotation, and the restart loses the warm cache as well.
    """
    findings = []
    services = [doc for doc in docs if doc.get("kind") == "Service"]
    for owner, template in _pod_templates(docs):
        if owner["kind"] != "Deployment":
            continue
        where = f"Deployment/{_name(owner)}"
        container = template["spec"]["containers"][0]
        liveness = container.get("livenessProbe") or {}
        readiness = container.get("readinessProbe") or {}
        startup = container.get("startupProbe")
        if not liveness or not readiness:
            findings.append(
                Finding("probes.missing", where, "a Deployment needs both liveness and readiness")
            )
            continue
        live_http = liveness.get("httpGet")
        ready_http = readiness.get("httpGet")
        if live_http and ready_http and live_http.get("path") == ready_http.get("path"):
            findings.append(
                Finding(
                    "probes.same-endpoint",
                    where,
                    f"both probes call {live_http['path']}, so readiness proves only that the "
                    "process is up",
                )
            )
        live_budget = liveness.get("periodSeconds", 10) * liveness.get("failureThreshold", 3)
        ready_budget = readiness.get("periodSeconds", 10) * readiness.get("failureThreshold", 3)
        if live_budget <= ready_budget:
            findings.append(
                Finding(
                    "probes.budget-order",
                    where,
                    f"liveness gives up after {live_budget}s and readiness after "
                    f"{ready_budget}s, so a blip restarts the pod instead of draining it",
                )
            )
        if startup:
            for label, probe in (("liveness", liveness), ("readiness", readiness)):
                if probe.get("initialDelaySeconds", 0) != 0:
                    findings.append(
                        Finding(
                            "probes.double-delay",
                            where,
                            f"{label} has initialDelaySeconds on top of a startup probe, "
                            "which is the same boot wait counted twice",
                        )
                    )
        # The probe has to be asking the port the Service sends traffic to.
        port_names = {
            port["name"]
            for port in template["spec"]["containers"][0].get("ports", [])
            if "name" in port
        }
        for label, probe in (("liveness", liveness), ("readiness", readiness)):
            http = probe.get("httpGet")
            if not http:
                continue
            port = http.get("port")
            if isinstance(port, str) and port not in port_names:
                findings.append(
                    Finding(
                        "probes.port-name",
                        where,
                        f"{label} asks port {port!r}, which is not a named container port "
                        f"({sorted(port_names)})",
                    )
                )
        for service in services:
            if not _selector_matches(service["spec"]["selector"], template["metadata"]["labels"]):
                continue
            targets = {port.get("targetPort") for port in service["spec"]["ports"]}
            ready_port = (ready_http or {}).get("port")
            if ready_port is not None and ready_port not in targets:
                findings.append(
                    Finding(
                        "probes.service-mismatch",
                        where,
                        f"readiness probes {ready_port!r} but Service/{_name(service)} routes "
                        f"to {sorted(str(t) for t in targets)}, so readiness is not measuring "
                        "the path traffic takes",
                    )
                )
    return findings


def _is_service_endpoint(docs: list[dict], labels: dict) -> bool:
    return any(
        doc.get("kind") == "Service" and _selector_matches(doc["spec"]["selector"], labels)
        for doc in docs
    )


def check_rollout_timing(docs: list[dict]) -> list[Finding]:
    """progressDeadlineSeconds has to be longer than a pod legitimately takes.

    Argo CD reads a Deployment's Progressing condition for the Application's
    health. Set the deadline below the real boot time and the Application turns
    Degraded while the rollout is still working, which trains people to ignore
    a Degraded Application.
    """
    findings = []
    for owner, template in _pod_templates(docs):
        if owner["kind"] != "Deployment":
            continue
        spec = owner["spec"]
        where = f"Deployment/{_name(owner)}"
        container = template["spec"]["containers"][0]
        startup = container.get("startupProbe") or {}
        boot = startup.get("periodSeconds", 0) * startup.get("failureThreshold", 0)
        needed = boot + spec.get("minReadySeconds", 0)
        deadline = spec.get("progressDeadlineSeconds", 600)
        if deadline <= needed:
            findings.append(
                Finding(
                    "rollout.progress-deadline",
                    where,
                    f"progressDeadlineSeconds {deadline} is not above the {needed}s a pod is "
                    "allowed to take to boot and settle",
                )
            )
        if spec.get("minReadySeconds", 0) == 0:
            findings.append(
                Finding(
                    "rollout.min-ready",
                    where,
                    "minReadySeconds 0 lets a pod that passes readiness and then crashes "
                    "count as progress, and a bad image takes every replica",
                )
            )
        lifecycle = container.get("lifecycle", {}).get("preStop")
        grace = template["spec"].get("terminationGracePeriodSeconds", 30)
        # Only a pod something routes to needs a drain window. A queue consumer
        # is not an endpoint of anything: it stops asking for work, finishes
        # what it holds, and a sleep before SIGTERM would only delay that.
        routed = _is_service_endpoint(docs, template["metadata"]["labels"])
        if lifecycle is None and routed:
            findings.append(
                Finding(
                    "rollout.no-drain",
                    where,
                    "a Service routes to this pod and it has no preStop hook, so the process "
                    "exits before kube-proxy has removed the endpoint and in-flight requests "
                    "are dropped",
                )
            )
        elif lifecycle is not None:
            sleep = int(lifecycle["exec"]["command"][-1])
            if sleep >= grace:
                findings.append(
                    Finding(
                        "rollout.drain-exceeds-grace",
                        where,
                        f"preStop sleeps {sleep}s inside a {grace}s grace period, so the "
                        "kubelet kills the pod mid-drain",
                    )
                )
    return findings


def check_jobs(docs: list[dict]) -> list[Finding]:
    """Two ways a migration Job goes wrong after it has already worked once.

    ttlSecondsAfterFinished: the TTL controller deletes the finished Job, Argo
    CD finds it missing on the next sync, re-creates it, and the migration runs
    again. On a contract migration that is a dropped column the second time.

    Service endpoints: the Service here selects on name and instance with no
    track label, because that is how the canary Deployment receives traffic. A
    Job whose pod template carries those same labels becomes an endpoint of the
    Service, and requests land on a container running psql.
    """
    findings = []
    services = [doc for doc in docs if doc.get("kind") == "Service"]
    for job in [doc for doc in docs if doc.get("kind") == "Job"]:
        where = f"Job/{_name(job)}"
        spec = job["spec"]
        if "ttlSecondsAfterFinished" in spec:
            findings.append(
                Finding(
                    "job.ttl",
                    where,
                    "ttlSecondsAfterFinished on an Argo CD managed Job: the TTL controller "
                    "deletes it and the next sync re-runs the migration",
                )
            )
        if "activeDeadlineSeconds" not in spec:
            findings.append(
                Finding(
                    "job.no-deadline",
                    where,
                    "no activeDeadlineSeconds, so a wedged migration holds the sync wave "
                    "until the whole sync times out",
                )
            )
        labels = spec["template"]["metadata"].get("labels", {})
        for service in services:
            if _selector_matches(service["spec"]["selector"], labels):
                findings.append(
                    Finding(
                        "job.is-service-endpoint",
                        where,
                        f"its pod labels satisfy Service/{_name(service)}'s selector, so the "
                        "migration pod receives application traffic",
                    )
                )
        if spec["template"]["spec"].get("priorityClassName") == SERVING_PRIORITY:
            findings.append(
                Finding(
                    "job.priority",
                    where,
                    "a migration at serving priority can preempt the pods it is migrating for",
                )
            )
    return findings


def check_hpa_ownership(docs: list[dict]) -> list[Finding]:
    """A committed replica count plus an HPA is a fight with no winner.

    Argo CD sees the live count differ from the commit and self-heals it down.
    The HPA scales it back up. Both are working correctly. The fix is for the
    manifest not to have an opinion, which means omitting spec.replicas rather
    than hiding the diff.
    """
    findings = []
    deployments = {_name(doc): doc for doc in docs if doc.get("kind") == "Deployment"}
    for hpa in [doc for doc in docs if doc.get("kind") == "HorizontalPodAutoscaler"]:
        target_name = hpa["spec"]["scaleTargetRef"]["name"]
        target = deployments.get(target_name)
        where = f"HorizontalPodAutoscaler/{_name(hpa)}"
        if target is None:
            findings.append(Finding("hpa.no-target", where, f"no Deployment/{target_name}"))
            continue
        if "replicas" in target["spec"]:
            findings.append(
                Finding(
                    "hpa.replicas-committed",
                    where,
                    f"Deployment/{target_name} commits replicas "
                    f"{target['spec']['replicas']} while this HPA also writes it",
                )
            )
        spec = hpa["spec"]
        if int(spec["minReplicas"]) > int(spec["maxReplicas"]):
            findings.append(Finding("hpa.bounds", where, "minReplicas above maxReplicas"))
        if any(doc.get("kind") == "Deployment" and "canary" in _name(doc) for doc in docs):
            findings.append(
                Finding(
                    "hpa.canary-conflict",
                    where,
                    "an HPA moves the stable replica count during the canary analysis "
                    "window, so the weight the report was written against is gone",
                )
            )
    return findings


def check_sync_waves(docs: list[dict]) -> list[Finding]:
    """The ordering Argo CD applies, read off the annotations rather than hoped for."""
    findings = []
    waves: dict[str, list[int]] = {}
    for doc in docs:
        annotations = doc.get("metadata", {}).get("annotations", {}) or {}
        raw = annotations.get("argocd.argoproj.io/sync-wave")
        if raw is None:
            findings.append(
                Finding(
                    "wave.missing",
                    f"{doc.get('kind')}/{_name(doc)}",
                    "no sync-wave annotation, so it is applied in wave 0 with everything else",
                )
            )
            continue
        waves.setdefault(doc["kind"], []).append(int(raw))

    def first(kind: str) -> int | None:
        values = waves.get(kind)
        return min(values) if values else None

    config = first("ConfigMap")
    job = first("Job")
    deployment = first("Deployment")
    hpa = first("HorizontalPodAutoscaler")
    if config is not None and job is not None and not config < job:
        findings.append(
            Finding(
                "wave.config-after-migration",
                "ConfigMap",
                f"wave {config} is not before the migration's wave {job}, and the migration "
                "reads its connection string from it",
            )
        )
    if job is not None and deployment is not None and not job < deployment:
        findings.append(
            Finding(
                "wave.migration-after-rollout",
                "Job",
                f"migration wave {job} is not before the workload's wave {deployment}, so the "
                "new image can start against the old schema",
            )
        )
    if hpa is not None and deployment is not None and not hpa > deployment:
        findings.append(
            Finding(
                "wave.hpa-before-workload",
                "HorizontalPodAutoscaler",
                f"wave {hpa} is not after the workload's wave {deployment}, so it reconciles "
                "against a scale target that does not exist yet",
            )
        )
    return findings


def check_rollback_safety(docs: list[dict]) -> list[Finding]:
    """A contract migration removes the previous image's ability to run.

    Argo CD will happily roll the Deployment back to the previous image. The
    pods start, pass liveness, and fail every query that touches the dropped
    column. The repo cannot prevent that, so it marks it: the annotation is on
    the Deployment, and the Application for that service is the one with
    automated sync switched off.
    """
    findings = []
    contract = [
        job
        for job in docs
        if job.get("kind") == "Job"
        and job["metadata"]["labels"].get("deploy.sam-farooq.dev/migration-strategy") == "contract"
    ]
    if not contract:
        return findings
    for deployment in [doc for doc in docs if doc.get("kind") == "Deployment"]:
        annotations = deployment["metadata"].get("annotations", {}) or {}
        where = f"Deployment/{_name(deployment)}"
        if annotations.get("deploy.sam-farooq.dev/rollback") != "manual":
            findings.append(
                Finding(
                    "rollback.unmarked",
                    where,
                    "ships beside a contract migration but is annotated as automatically "
                    "reversible",
                )
            )
        if deployment["metadata"]["labels"].get("deploy.sam-farooq.dev/track") == "canary":
            findings.append(
                Finding(
                    "rollback.canary-on-contract",
                    where,
                    "a canary beside a contract migration has no fallback: the stable track "
                    "runs the previous image against a schema it cannot read",
                )
            )
    return findings


def check_topology_spread(docs: list[dict]) -> list[Finding]:
    findings = []
    for owner, template in _pod_templates(docs):
        for constraint in template["spec"].get("topologySpreadConstraints", []) or []:
            if constraint.get("whenUnsatisfiable") == "DoNotSchedule":
                findings.append(
                    Finding(
                        "topology.hard-constraint",
                        f"{owner['kind']}/{_name(owner)}",
                        f"DoNotSchedule on {constraint['topologyKey']}: three zones and two "
                        "replicas cannot satisfy a skew of 1, and the pod stays Pending",
                    )
                )
    return findings


OBJECT_RULES = (
    check_resources,
    check_image_tags,
    check_disruption_budgets,
    check_probes,
    check_rollout_timing,
    check_jobs,
    check_hpa_ownership,
    check_sync_waves,
    check_rollback_safety,
    check_topology_spread,
)


def check_render(docs: list[dict]) -> list[Finding]:
    findings: list[Finding] = []
    for rule in OBJECT_RULES:
        findings.extend(rule(docs))
    return findings


def check_capacity(totals: Totals, capacity: Schedulable) -> list[Finding]:
    """Worst case against what the untainted pools can place."""
    findings = []
    if totals.cpu > capacity.cpu:
        findings.append(
            Finding(
                "capacity.cpu",
                "node pools",
                f"worst case {format_cpu(totals.cpu)} exceeds the "
                f"{format_cpu(capacity.cpu)} the untainted pools can place with "
                f"{capacity.headroom_percent}% held back",
            )
        )
    if totals.memory > capacity.memory:
        findings.append(
            Finding(
                "capacity.memory",
                "node pools",
                f"worst case {format_memory(totals.memory)} exceeds "
                f"{format_memory(capacity.memory)}",
            )
        )
    largest = totals.largest_pod()
    if largest is not None:
        if largest.cpu_per_pod > capacity.per_node_cpu:
            findings.append(
                Finding(
                    "capacity.pod-too-large",
                    largest.name,
                    f"one pod requests {format_cpu(largest.cpu_per_pod)} and the largest node "
                    f"offers {format_cpu(capacity.per_node_cpu)}, so no node count helps",
                )
            )
        if largest.memory_per_pod > capacity.per_node_memory:
            findings.append(
                Finding(
                    "capacity.pod-too-large",
                    largest.name,
                    f"one pod requests {format_memory(largest.memory_per_pod)} and the largest "
                    f"node offers {format_memory(capacity.per_node_memory)}",
                )
            )
    return findings


def _quota_hard(overlay: list[dict]) -> dict:
    for doc in overlay:
        if doc.get("kind") == "ResourceQuota":
            return doc["spec"]["hard"]
    raise ValueError("the overlay renders no ResourceQuota")


def check_quota(overlay: list[dict], totals: Totals, capacity: Schedulable) -> list[Finding]:
    """The quota is bracketed from both sides.

    Below the worst case and a normal rollout is rejected at the quota: the
    surge pod is never created, the Deployment reports a FailedCreate, and the
    sync sits Progressing with no failed object to look at. Above what the
    pools can place and the quota admits pods the cluster has nowhere to put,
    which is the same outage with Pending instead of Forbidden.
    """
    findings = []
    hard = _quota_hard(overlay)
    quota_cpu = cpu_millis(hard["requests.cpu"])
    quota_memory = memory_bytes(hard["requests.memory"])
    quota_pods = int(hard["count/pods"])
    if quota_cpu < totals.cpu:
        findings.append(
            Finding(
                "quota.below-worst-case",
                "ResourceQuota/platform",
                f"requests.cpu {format_cpu(quota_cpu)} is under the worst case "
                f"{format_cpu(totals.cpu)}, so a rollout is rejected at the quota",
            )
        )
    if quota_memory < totals.memory:
        findings.append(
            Finding(
                "quota.below-worst-case",
                "ResourceQuota/platform",
                f"requests.memory {format_memory(quota_memory)} is under the worst case "
                f"{format_memory(totals.memory)}",
            )
        )
    if quota_pods < totals.pods:
        findings.append(
            Finding(
                "quota.below-worst-case",
                "ResourceQuota/platform",
                f"count/pods {quota_pods} is under the worst case {totals.pods}",
            )
        )
    if quota_cpu > capacity.cpu:
        findings.append(
            Finding(
                "quota.above-capacity",
                "ResourceQuota/platform",
                f"requests.cpu {format_cpu(quota_cpu)} is above the "
                f"{format_cpu(capacity.cpu)} the pools can place, so the quota admits pods "
                "that stay Pending",
            )
        )
    if quota_memory > capacity.memory:
        findings.append(
            Finding(
                "quota.above-capacity",
                "ResourceQuota/platform",
                f"requests.memory {format_memory(quota_memory)} is above "
                f"{format_memory(capacity.memory)}",
            )
        )
    if hard.get("limits.memory") and memory_bytes(hard["limits.memory"]) != quota_memory:
        findings.append(
            Finding(
                "quota.memory-split",
                "ResourceQuota/platform",
                "limits.memory differs from requests.memory while the chart sets them equal, "
                "so one of the two numbers is unreachable",
            )
        )
    return findings


def check_limit_range(overlay: list[dict], per_service: dict[str, list[dict]]) -> list[Finding]:
    """A LimitRange max below the largest request rejects the pod.

    The message names the LimitRange, which is a long way from the values file
    that has to change, so this is checked where both are visible.
    """
    findings = []
    ranges = [doc for doc in overlay if doc.get("kind") == "LimitRange"]
    if not ranges:
        return [Finding("limitrange.missing", "overlay", "no LimitRange in this environment")]
    for limit in ranges[0]["spec"]["limits"]:
        if limit.get("type") != "Container":
            continue
        max_cpu = cpu_millis(limit["max"]["cpu"])
        max_memory = memory_bytes(limit["max"]["memory"])
        min_cpu = cpu_millis(limit["min"]["cpu"])
        for service, docs in per_service.items():
            for owner, template in _pod_templates(docs):
                cpu, memory = container_requests(template["spec"])
                where = f"{service} {owner['kind']}/{_name(owner)}"
                if cpu > max_cpu:
                    findings.append(
                        Finding(
                            "limitrange.max",
                            where,
                            f"requests {format_cpu(cpu)} against a LimitRange max of "
                            f"{format_cpu(max_cpu)}",
                        )
                    )
                if memory > max_memory:
                    findings.append(
                        Finding(
                            "limitrange.max",
                            where,
                            f"requests {format_memory(memory)} against a LimitRange max of "
                            f"{format_memory(max_memory)}",
                        )
                    )
                if cpu < min_cpu:
                    findings.append(
                        Finding(
                            "limitrange.min",
                            where,
                            f"requests {format_cpu(cpu)} below the LimitRange min of "
                            f"{format_cpu(min_cpu)}",
                        )
                    )
    return findings
