"""Every refusal the chart makes, proven to fire.

These were all written against a guards file named _guards.yaml, which Helm
treats as a partial and never executes. Every one of them rendered happily.
The tests are here because the guards were not.
"""

import pytest

from gitopsvalidate.render import ToolMissing, helm_template, helm_template_raises, tools_required

pytestmark = pytest.mark.renders


@pytest.fixture(autouse=True, scope="module")
def _needs_helm():
    try:
        helm_template("rag-agent", "prod")
    except ToolMissing as exc:
        if tools_required():
            pytest.fail(f"GITOPS_REQUIRE_TOOLS=1 is set and {exc}")
        pytest.skip(str(exc))


def test_moving_image_tag_is_refused():
    message = helm_template_raises("rag-agent", "prod", {"image.tag": "latest"})
    assert "moving tag" in message
    assert "rollback has nothing to roll back to" in message


def test_empty_image_tag_is_refused():
    assert "image.tag is required" in helm_template_raises("rag-agent", "prod", {"image.tag": ""})


def test_canary_and_autoscaling_together_are_refused():
    message = helm_template_raises("rag-agent", "prod", {"autoscaling.enabled": "true"})
    assert "mutually exclusive" in message


def test_canary_on_a_contract_migration_is_refused():
    message = helm_template_raises("rag-agent", "prod", {"migration.strategy": "contract"})
    assert "the stable track is not a fallback" in message


def test_budget_over_one_stable_pod_is_refused():
    # 2 replicas with a 50% canary leaves 1 pod on the stable track, which the
    # PDB selects. Reading .Values.replicas instead of the stable count was the
    # first version of this guard, and it passed this exact case.
    message = helm_template_raises("rag-agent", "prod", {"replicas": "2", "canary.weight": "50"})
    assert "1 pod(s) on the stable track" in message
    assert "replicas minus the 50% canary" in message


def test_budget_over_one_replica_is_refused():
    message = helm_template_raises(
        "embedding-worker", "prod", {"replicas": "1", "pdb.enabled": "true"}
    )
    assert "a node drain blocks" in message


def test_budget_over_an_hpa_floor_of_one_is_refused():
    message = helm_template_raises("model-serving", "prod", {"autoscaling.minReplicas": "1"})
    assert "autoscaling.minReplicas" in message


def test_migration_without_a_content_hash_is_refused():
    message = helm_template_raises("rag-agent", "prod", {"migration.sha": ""})
    assert "pod template is immutable" in message


def test_one_endpoint_answering_both_probes_is_refused():
    message = helm_template_raises("rag-agent", "prod", {"probes.readiness": "/healthz"})
    assert "cold connection pool" in message


@pytest.mark.parametrize("weight", ["5", "7", "13"])
def test_a_weight_that_is_not_whole_pods_is_refused(weight):
    # Go templates divide integers. div (mul 10 5) 100 is 0, so a 5% canary at
    # ten replicas is no canary at all, and the rollout reports success having
    # routed nothing to the new image.
    message = helm_template_raises("rag-agent", "prod", {"canary.weight": weight})
    assert "not expressible with 10 replicas" in message
    assert "the smallest step is 10%" in message


def test_a_weight_that_leaves_one_stable_pod_is_refused():
    # 90% of 10 replicas is 9 canary pods and 1 stable, which the PDB selects.
    # The weight arithmetic is fine; the budget over the remainder is not.
    message = helm_template_raises("rag-agent", "prod", {"canary.weight": "90"})
    assert "1 pod(s) on the stable track" in message


@pytest.mark.parametrize("weight", ["10", "20", "50"])
def test_a_weight_that_is_whole_pods_renders(weight):
    docs = helm_template("rag-agent", "prod", {"canary.weight": weight})
    canary = next(doc for doc in docs if doc["metadata"]["name"] == "rag-agent-canary")
    assert canary["spec"]["replicas"] == int(weight) // 10
    stable = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "rag-agent"
    )
    # The total is held constant, so the canary takes pods from stable rather
    # than adding to the service's footprint.
    assert stable["spec"]["replicas"] + canary["spec"]["replicas"] == 10
