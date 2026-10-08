"""The arithmetic that decides whether a pod can be scheduled at all.

A resource request is the one number in a values file that is checked by
nothing: it renders, it applies, and then at the ceiling of the autoscaler the
pods stay Pending. These are the checks that would have caught that in CI.
"""

import pytest

from gitopsvalidate import ENVIRONMENTS
from gitopsvalidate.quantity import cpu_millis, memory_bytes
from gitopsvalidate.render import read_cluster
from gitopsvalidate.rules import check_capacity, check_limit_range, check_quota
from gitopsvalidate.workload import PodGroup, Totals, pod_groups, schedulable, surge_pods, totals

pytestmark = pytest.mark.renders


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_worst_case_fits_the_untainted_pools(worst_cases, capacities, env):
    findings = check_capacity(worst_cases[env], capacities[env])
    assert findings == [], "\n".join(str(finding) for finding in findings)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_quota_brackets_the_worst_case(renders, worst_cases, capacities, env):
    findings = check_quota(renders[env].overlay, worst_cases[env], capacities[env])
    assert findings == [], "\n".join(str(finding) for finding in findings)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_limit_range_admits_every_container(renders, env):
    findings = check_limit_range(renders[env].overlay, renders[env].per_service)
    assert findings == [], "\n".join(str(finding) for finding in findings)


def test_the_tainted_pool_is_excluded_from_prod_capacity():
    # eks-infra taints the spark pool workload=spark:NoSchedule and nothing in
    # this repo tolerates it. Counting it was the first version of this
    # function: 45.6 cores and 360Gi that no pod here can reach.
    cluster = read_cluster("prod")
    capacity = schedulable(cluster)
    assert capacity.excluded_cpu == cpu_millis("15200m") * 3
    assert capacity.excluded_memory == memory_bytes("120Gi") * 3
    assert capacity.allocatable_cpu == cpu_millis("7300m") * 4


def test_tolerating_the_taint_would_include_the_pool():
    cluster = read_cluster("prod")
    with_toleration = schedulable(cluster, tolerations=("workload",))
    assert with_toleration.excluded_cpu == 0
    assert with_toleration.allocatable_cpu == cpu_millis("7300m") * 4 + cpu_millis("15200m") * 3


def test_headroom_is_held_back():
    cluster = read_cluster("prod")
    capacity = schedulable(cluster)
    assert capacity.headroom_percent == 15
    assert capacity.cpu == capacity.allocatable_cpu * 85 // 100
    assert capacity.cpu == 24820


def test_schedulable_memory_is_a_whole_number_of_mebibytes():
    # 85% of 112Gi is not a round number of anything, and a quota message
    # reading 102220221644 is a quota message nobody checks.
    capacity = schedulable(read_cluster("prod"))
    assert capacity.memory % (1024 * 1024) == 0


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_a_single_pod_fits_on_one_node(worst_cases, capacities, env):
    largest = worst_cases[env].largest_pod()
    assert largest is not None
    assert largest.cpu_per_pod <= capacities[env].per_node_cpu
    assert largest.memory_per_pod <= capacities[env].per_node_memory


def test_a_pod_larger_than_any_node_is_caught():
    # 2 cores against a pool that can place 2.89 of them, on nodes that offer
    # 1.7 each. The pool total passes and the pod never schedules, which is why
    # this is a separate rule: more nodes do not help, and the scheduler says
    # Insufficient cpu either way.
    capacity = schedulable(read_cluster("staging"))
    group = PodGroup("x", "x", "Deployment", 1, cpu_millis("2"), memory_bytes("1Gi"))
    worst = Totals(cpu=group.cpu, memory=group.memory, pods=1, groups=(group,))
    findings = check_capacity(worst, capacity)
    assert [finding.rule for finding in findings] == ["capacity.pod-too-large"]
    assert "no node count helps" in findings[0].message
    assert worst.cpu <= capacity.cpu


def test_one_more_model_serving_replica_is_still_inside_prod_headroom(renders, capacities):
    # 8.10 cores at maxReplicas 8 plus the surge pod. The next replica is 900m.
    worst = totals(renders["prod"].per_service)
    assert worst.cpu == 22600
    assert worst.cpu + 900 <= capacities["prod"].cpu


def test_three_more_model_serving_replicas_break_prod_headroom(renders, capacities):
    worst = totals(renders["prod"].per_service)
    assert worst.cpu + 3 * 900 > capacities["prod"].cpu


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_rollout_surge_is_counted(renders, env):
    # maxSurge 1 on three serving Deployments is three extra pods' worth of
    # requests for the length of a rollout. A quota sized to the resting state
    # rejects the surge pod, and the Deployment reports FailedCreate.
    groups = {
        group.name: group
        for service in renders[env].per_service
        for group in pod_groups(service, renders[env].per_service[service])
    }
    assert groups["rag-agent"].pods == groups["rag-agent"].pods
    worker = groups["embedding-worker"]
    served = groups["hybrid-search"]
    assert worker.pods == 1 if env == "staging" else worker.pods == 4
    assert served.pods > 1


@pytest.mark.parametrize(
    ("surge", "replicas", "expected"),
    [(1, 10, 1), (0, 4, 0), ("25%", 10, 3), ("50%", 3, 2), ("100%", 2, 2)],
)
def test_surge_percentages_round_up(surge, replicas, expected):
    deployment = {"spec": {"strategy": {"rollingUpdate": {"maxSurge": surge}}}}
    assert surge_pods(deployment, replicas) == expected


def test_the_default_surge_is_kubernetes_own_25_percent():
    assert surge_pods({"spec": {}}, 8) == 2


def test_an_init_container_is_refused_rather_than_mis_costed(renders):
    from gitopsvalidate.workload import container_requests

    pod = {"initContainers": [{"name": "wait"}], "containers": []}
    # An init container's request is max()'d against the others, not added. The
    # function says so rather than implementing a branch no test can reach.
    with pytest.raises(NotImplementedError):
        container_requests(pod)
