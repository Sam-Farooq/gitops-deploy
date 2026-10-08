"""The cluster-scoped half: namespace, quota, limits, policies, priorities.

Kustomize rather than Helm because none of it repeats per service. The checks
here are about what the two overlays are allowed to differ in.
"""

import pytest

from gitopsvalidate import ENVIRONMENTS
from gitopsvalidate.quantity import cpu_millis, memory_bytes

pytestmark = pytest.mark.renders

# The overlays fixture is in conftest.py, because building it here called
# kustomize with no guard and a missing binary became a setup error.


def by_kind(docs, kind):
    return [doc for doc in docs if doc["kind"] == kind]


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_namespace_is_renamed_per_environment(overlays, env):
    namespaces = by_kind(overlays[env], "Namespace")
    assert [doc["metadata"]["name"] for doc in namespaces] == [f"platform-{env}"]


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_pod_security_is_enforced_not_audited(overlays, env):
    labels = by_kind(overlays[env], "Namespace")[0]["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    # A warn-only level is a label that makes a dashboard green and admits a
    # privileged pod anyway.
    assert "warn" not in labels.get("pod-security.kubernetes.io/enforce", "")


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_namespaced_objects_land_in_the_right_namespace(overlays, env):
    for doc in overlays[env]:
        if doc["kind"] in ("Namespace", "PriorityClass"):
            continue
        assert doc["metadata"]["namespace"] == f"platform-{env}"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_priority_classes_stay_cluster_scoped(overlays, env):
    classes = by_kind(overlays[env], "PriorityClass")
    assert {doc["metadata"]["name"] for doc in classes} == {"serving", "batch"}
    for doc in classes:
        # The namespace transformer must not have touched these. A namespaced
        # PriorityClass is rejected by the API server.
        assert "namespace" not in doc["metadata"]


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_batch_never_preempts_serving(overlays, env):
    classes = {doc["metadata"]["name"]: doc for doc in by_kind(overlays[env], "PriorityClass")}
    assert classes["batch"]["value"] < classes["serving"]["value"]
    # A migration that can evict the pods it is migrating for has turned a
    # deploy into an outage.
    assert classes["batch"]["preemptionPolicy"] == "Never"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_base_quota_is_patched_rather_than_shipped(overlays, env):
    quota = by_kind(overlays[env], "ResourceQuota")[0]["spec"]["hard"]
    # The base declares 1 core and 1Gi, which runs nothing. A base that is also
    # a working quota is a base that gets shipped when an overlay forgets.
    assert cpu_millis(quota["requests.cpu"]) > cpu_millis("1")
    assert memory_bytes(quota["requests.memory"]) > memory_bytes("1Gi")


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_no_load_balancer_is_permitted(overlays, env):
    quota = by_kind(overlays[env], "ResourceQuota")[0]["spec"]["hard"]
    assert quota["services.loadbalancers"] == "0"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_ingress_and_egress_are_both_denied_by_default(overlays, env):
    policies = {doc["metadata"]["name"]: doc for doc in by_kind(overlays[env], "NetworkPolicy")}
    deny = policies["default-deny"]
    assert deny["spec"]["podSelector"] == {}
    assert set(deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert "egress" not in deny["spec"]


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_dns_is_allowed_back_out(overlays, env):
    # The first thing a default-deny breaks. Without it the pods come up, pass
    # their probes, and fail every outbound call with a resolution error that
    # reads like a dead dependency.
    policies = {doc["metadata"]["name"]: doc for doc in by_kind(overlays[env], "NetworkPolicy")}
    egress = policies["allow-dns"]["spec"]["egress"][0]
    ports = {(port["protocol"], port["port"]) for port in egress["ports"]}
    assert ports == {("UDP", 53), ("TCP", 53)}


def test_prod_egress_is_narrower_than_staging(overlays):
    def cidrs(env):
        policies = {doc["metadata"]["name"]: doc for doc in by_kind(overlays[env], "NetworkPolicy")}
        rules = policies["allow-egress-datastores"]["spec"]["egress"]
        return {to["ipBlock"]["cidr"] for rule in rules for to in rule["to"]}

    assert cidrs("prod") == {"10.40.0.0/16"}
    assert "0.0.0.0/0" in cidrs("staging")


def test_staging_still_blocks_the_instance_metadata_service(overlays):
    # Reachable from a pod, it hands out the node's IAM role, which is larger
    # than any pod's.
    policies = {
        doc["metadata"]["name"]: doc for doc in by_kind(overlays["staging"], "NetworkPolicy")
    }
    rule = policies["allow-egress-datastores"]["spec"]["egress"][0]["to"][0]["ipBlock"]
    assert "169.254.169.254/32" in rule["except"]


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_governance_objects_sync_before_anything_that_needs_them(overlays, env):
    for doc in overlays[env]:
        if doc["kind"] in ("Namespace", "PriorityClass"):
            continue
        wave = int(doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
        # Negative, so the quota and the deny-all exist before wave 0 of any
        # service Application.
        assert wave < 0


def test_the_limit_range_max_differs_between_environments(overlays):
    def maximum(env):
        limits = by_kind(overlays[env], "LimitRange")[0]["spec"]["limits"][0]
        return cpu_millis(limits["max"]["cpu"]), memory_bytes(limits["max"]["memory"])

    # Smaller in staging on purpose: a container that fits in prod and not in
    # staging is caught by the environment that is allowed to break.
    assert maximum("staging") < maximum("prod")
