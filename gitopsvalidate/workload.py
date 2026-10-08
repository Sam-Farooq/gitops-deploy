"""Worst case pod count and resource requests, read off the rendered objects.

Worst case means the most the scheduler can be asked for at once: every
Deployment at its ceiling, plus the extra pod each rolling update surges, plus
every migration Job. It is not a forecast. It is the number the namespace quota
has to be at least as large as, and the node pool has to be able to place.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .quantity import cpu_millis, memory_bytes

_SERVING_KINDS = ("Deployment",)


@dataclass(frozen=True)
class PodGroup:
    """One rendered controller, costed at its worst case."""

    service: str
    name: str
    kind: str
    pods: int
    cpu_per_pod: int
    memory_per_pod: int

    @property
    def cpu(self) -> int:
        return self.pods * self.cpu_per_pod

    @property
    def memory(self) -> int:
        return self.pods * self.memory_per_pod


@dataclass(frozen=True)
class Totals:
    cpu: int
    memory: int
    pods: int
    groups: tuple[PodGroup, ...]

    def largest_pod(self) -> PodGroup | None:
        """The group whose single pod is biggest by CPU.

        A pod larger than one node's allocatable never schedules, however many
        nodes there are, and the error says Insufficient cpu either way.
        """
        if not self.groups:
            return None
        return max(self.groups, key=lambda group: (group.cpu_per_pod, group.memory_per_pod))


def container_requests(pod_spec: dict) -> tuple[int, int]:
    """Summed CPU and memory requests of a pod's containers.

    Init containers would be max() against this sum rather than added, since
    they run before the others. Nothing here has one, and the function says so
    rather than implementing a branch no test can reach.
    """
    if pod_spec.get("initContainers"):
        raise NotImplementedError("init containers change the effective request calculation")
    cpu = 0
    memory = 0
    for container in pod_spec.get("containers", []):
        requests = container.get("resources", {}).get("requests", {})
        if "cpu" in requests:
            cpu += cpu_millis(requests["cpu"])
        if "memory" in requests:
            memory += memory_bytes(requests["memory"])
    return cpu, memory


def surge_pods(deployment: dict, replicas: int) -> int:
    """Extra pods a rolling update of this Deployment can add.

    maxSurge is an int or a percentage of the desired count, and Kubernetes
    rounds a percentage up.
    """
    rolling = deployment.get("spec", {}).get("strategy", {}).get("rollingUpdate", {})
    surge = rolling.get("maxSurge", "25%")
    if isinstance(surge, int):
        return surge
    text = str(surge)
    if text.endswith("%"):
        return math.ceil(replicas * int(text[:-1]) / 100)
    return int(text)


def hpa_ceilings(docs: list[dict]) -> dict[str, int]:
    """Deployment name to maxReplicas, for the Deployments an HPA owns."""
    out: dict[str, int] = {}
    for doc in docs:
        if doc.get("kind") != "HorizontalPodAutoscaler":
            continue
        target = doc["spec"]["scaleTargetRef"]
        if target.get("kind") in _SERVING_KINDS:
            out[target["name"]] = int(doc["spec"]["maxReplicas"])
    return out


def pod_groups(service: str, docs: list[dict]) -> list[PodGroup]:
    """Cost every controller in one service's render at its worst case."""
    ceilings = hpa_ceilings(docs)
    groups: list[PodGroup] = []
    for doc in docs:
        kind = doc.get("kind")
        name = doc.get("metadata", {}).get("name", "")
        if kind == "Deployment":
            spec = doc["spec"]
            # An HPA-owned Deployment has no spec.replicas by design, so its
            # ceiling comes from the HPA. A Deployment with neither is one the
            # chart could not have produced.
            if "replicas" in spec:
                replicas = int(spec["replicas"])
            elif name in ceilings:
                replicas = ceilings[name]
            else:
                raise ValueError(f"{name}: no spec.replicas and no HPA to set a ceiling")
            cpu, memory = container_requests(spec["template"]["spec"])
            groups.append(
                PodGroup(
                    service=service,
                    name=name,
                    kind=kind,
                    pods=replicas + surge_pods(doc, replicas),
                    cpu_per_pod=cpu,
                    memory_per_pod=memory,
                )
            )
        elif kind == "Job":
            spec = doc["spec"]
            parallelism = int(spec.get("parallelism", 1))
            cpu, memory = container_requests(spec["template"]["spec"])
            groups.append(
                PodGroup(
                    service=service,
                    name=name,
                    kind=kind,
                    pods=parallelism,
                    cpu_per_pod=cpu,
                    memory_per_pod=memory,
                )
            )
    return groups


def totals(per_service: dict[str, list[dict]]) -> Totals:
    groups: list[PodGroup] = []
    for service, docs in per_service.items():
        groups.extend(pod_groups(service, docs))
    return Totals(
        cpu=sum(group.cpu for group in groups),
        memory=sum(group.memory for group in groups),
        pods=sum(group.pods for group in groups),
        groups=tuple(groups),
    )


MEBIBYTE = 1024 * 1024


@dataclass(frozen=True)
class Schedulable:
    """What the untainted pools can actually place, after headroom.

    cpu and memory are the figure after headroom. allocatable_cpu and
    allocatable_memory are before it, because a share of the pool is the number
    worth reading and a share of a figure that already had 15% taken off is not.
    """

    cpu: int
    memory: int
    allocatable_cpu: int
    allocatable_memory: int
    per_node_cpu: int
    per_node_memory: int
    excluded_cpu: int
    excluded_memory: int
    headroom_percent: int


def schedulable(cluster: dict, tolerations: tuple[str, ...] = ()) -> Schedulable:
    """Allocatable capacity, minus the pools nothing tolerates, minus headroom.

    `tolerations` is the set of taint keys the workloads tolerate. Nothing in
    this repo sets one, so the spark pool is excluded, and the first version of
    this function counted it: 45 cores and 360Gi that no pod here can reach.
    """
    headroom = int(cluster.get("headroomPercent", 0))
    cpu = memory = 0
    excluded_cpu = excluded_memory = 0
    per_node_cpu = per_node_memory = 0
    for pool in cluster["pools"]:
        pool_cpu = cpu_millis(pool["allocatableCpu"]) * int(pool["count"])
        pool_memory = memory_bytes(pool["allocatableMemory"]) * int(pool["count"])
        blocking = [
            taint
            for taint in pool.get("taints") or []
            if taint.get("effect") in ("NoSchedule", "NoExecute")
            and taint["key"] not in tolerations
        ]
        if blocking:
            excluded_cpu += pool_cpu
            excluded_memory += pool_memory
            continue
        cpu += pool_cpu
        memory += pool_memory
        per_node_cpu = max(per_node_cpu, cpu_millis(pool["allocatableCpu"]))
        per_node_memory = max(per_node_memory, memory_bytes(pool["allocatableMemory"]))
    keep = 100 - headroom
    return Schedulable(
        cpu=cpu * keep // 100,
        # Floored to whole mebibytes. 85% of 12Gi is not a round number of
        # anything, and a quota message reading 10952166604 is a quota message
        # nobody checks.
        memory=(memory * keep // 100) // MEBIBYTE * MEBIBYTE,
        allocatable_cpu=cpu,
        allocatable_memory=memory,
        per_node_cpu=per_node_cpu,
        per_node_memory=per_node_memory,
        excluded_cpu=excluded_cpu,
        excluded_memory=excluded_memory,
        headroom_percent=headroom,
    )
