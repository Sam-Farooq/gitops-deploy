"""The rules against manifests built to break them.

Every case here renders, applies and passes a schema check. They are the
failures that arrive later: at the next drain, the next peak, the next
rollback. The passing side comes from the real renders in test_rendered.py;
this file is the proof that each rule can still say no.
"""

import copy
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

from gitopsvalidate.rules import (
    check_disruption_budgets,
    check_hpa_ownership,
    check_image_tags,
    check_jobs,
    check_probes,
    check_resources,
    check_rollback_safety,
    check_rollout_timing,
    check_sync_waves,
    check_topology_spread,
)

FIXTURES = Path(__file__).parent / "fixtures" / "baseline.yaml"


@lru_cache(maxsize=1)
def _baseline() -> dict[str, dict]:
    """The hand authored objects in fixtures/baseline.yaml, keyed by kind.

    One set that passes every rule. Each test deep copies it and spoils exactly
    one field, so a test cannot be passing because something else was also
    wrong.
    """
    with FIXTURES.open() as handle:
        return {doc["kind"]: doc for doc in yaml.safe_load_all(handle) if doc}


def fixture(kind: str, **spec):
    """A copy of one baseline object, with spec keys replaced or dropped.

    A None drops the key, which is how a minAvailable test stops being a
    maxUnavailable test as well: Kubernetes rejects a PDB that sets both.
    """
    doc = copy.deepcopy(_baseline()[kind])
    doc["spec"].update(spec)
    doc["spec"] = {key: value for key, value in doc["spec"].items() if value is not None}
    return doc


def deployment(**overrides):
    return fixture("Deployment", **overrides)


def service(selector=None):
    doc = fixture("Service")
    if selector is not None:
        doc["spec"]["selector"] = selector
    return doc


def job(labels=None, strategy="expand", **overrides):
    doc = fixture("Job", **overrides)
    doc["metadata"]["labels"]["deploy.sam-farooq.dev/migration-strategy"] = strategy
    if labels is not None:
        doc["spec"]["template"]["metadata"]["labels"] = labels
    return doc


def pdb(**spec):
    return fixture("PodDisruptionBudget", **spec)


def hpa(**spec):
    return fixture("HorizontalPodAutoscaler", **spec)


def config_map(wave="0"):
    doc = copy.deepcopy(_baseline()["ConfigMap"])
    doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] = wave
    return doc


def rules(findings):
    return {finding.rule for finding in findings}


# --- resources ---------------------------------------------------------------


def test_a_clean_deployment_has_no_resource_findings():
    assert check_resources([deployment()]) == []


def test_memory_limit_above_its_request_is_caught():
    doc = deployment()
    doc["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "512Mi"
    findings = check_resources([doc])
    assert "resources.memory-guaranteed" in rules(findings)
    assert "Burstable" in findings[0].message


def test_a_missing_cpu_request_is_caught():
    doc = deployment()
    del doc["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"]
    assert "resources.cpu-request" in rules(check_resources([doc]))


def test_a_cpu_limit_on_serving_work_is_caught():
    doc = deployment()
    doc["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "1"
    findings = check_resources([doc])
    assert "resources.cpu-limit" in rules(findings)
    assert "throttles the tail latency" in findings[0].message


def test_a_missing_cpu_limit_on_batch_work_is_caught():
    doc = job()
    del doc["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"]
    assert "resources.cpu-limit" in rules(check_resources([doc]))


# --- image tags --------------------------------------------------------------


@pytest.mark.parametrize("image", ["ghcr.io/x/y:latest", "ghcr.io/x/y:main", "ghcr.io/x/y"])
def test_moving_and_absent_tags_are_caught(image):
    doc = deployment()
    doc["spec"]["template"]["spec"]["containers"][0]["image"] = image
    assert "image.tag" in rules(check_image_tags([doc]))


def test_a_registry_port_is_not_mistaken_for_a_tag():
    doc = deployment()
    doc["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.local:5000/x/y:1.2.3"
    assert check_image_tags([doc]) == []


# --- disruption budgets ------------------------------------------------------


def test_min_available_equal_to_replicas_blocks_a_drain():
    findings = check_disruption_budgets(
        [deployment(replicas=3), pdb(minAvailable=3, maxUnavailable=None)]
    )
    findings = [f for f in findings if f.rule == "pdb.blocks-drain"]
    assert findings
    assert "permits 0 evictions" in findings[0].message


def test_min_available_at_100_percent_blocks_a_drain():
    findings = check_disruption_budgets(
        [deployment(replicas=4), pdb(minAvailable="100%", maxUnavailable=None)]
    )
    assert "pdb.blocks-drain" in rules(findings)


def test_min_available_below_replicas_is_fine():
    findings = check_disruption_budgets(
        [deployment(replicas=4), pdb(minAvailable=3, maxUnavailable=None)]
    )
    assert "pdb.blocks-drain" not in rules(findings)


def test_a_budget_over_one_replica_is_caught():
    findings = check_disruption_budgets([deployment(replicas=1), pdb()])
    assert "pdb.single-replica" in rules(findings)


def test_max_unavailable_equal_to_replicas_drains_the_whole_service():
    findings = check_disruption_budgets([deployment(replicas=2), pdb(maxUnavailable=2)])
    assert "pdb.permits-everything" in rules(findings)


def test_a_budget_selecting_nothing_is_caught():
    findings = check_disruption_budgets(
        [deployment(), pdb(selector={"matchLabels": {"app": "typo"}})]
    )
    assert "pdb.selector" in rules(findings)


# --- probes ------------------------------------------------------------------


def test_one_path_for_both_probes_is_caught():
    doc = deployment()
    doc["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"] = (
        "/healthz"
    )
    findings = check_probes([doc])
    assert "probes.same-endpoint" in rules(findings)


def test_liveness_giving_up_first_is_caught():
    # 10s liveness against 30s readiness: a slow dependency restarts the pod
    # instead of draining it, and the restart loses the warm cache too.
    doc = deployment()
    container = doc["spec"]["template"]["spec"]["containers"][0]
    container["livenessProbe"]["failureThreshold"] = 1
    container["readinessProbe"] = {
        "httpGet": {"path": "/readyz", "port": "http"},
        "periodSeconds": 10,
        "failureThreshold": 3,
    }
    findings = check_probes([doc])
    assert "probes.budget-order" in rules(findings)
    assert "restarts the pod instead of draining it" in findings[0].message


def test_an_initial_delay_on_top_of_a_startup_probe_is_caught():
    doc = deployment()
    container = doc["spec"]["template"]["spec"]["containers"][0]
    container["startupProbe"] = {
        "httpGet": {"path": "/readyz", "port": "http"},
        "periodSeconds": 2,
        "failureThreshold": 30,
    }
    container["readinessProbe"]["initialDelaySeconds"] = 30
    assert "probes.double-delay" in rules(check_probes([doc]))


def test_a_probe_port_that_is_not_a_container_port_is_caught():
    doc = deployment()
    doc["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["port"] = "api"
    assert "probes.port-name" in rules(check_probes([doc]))


def test_readiness_on_a_port_the_service_does_not_route_to_is_caught():
    doc = deployment()
    container = doc["spec"]["template"]["spec"]["containers"][0]
    container["ports"].append({"name": "admin", "containerPort": 9090})
    container["readinessProbe"]["httpGet"]["port"] = "admin"
    findings = check_probes([doc, service()])
    assert "probes.service-mismatch" in rules(findings)


def test_a_deployment_with_no_probes_is_caught():
    doc = deployment()
    del doc["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert "probes.missing" in rules(check_probes([doc]))


# --- rollout timing ----------------------------------------------------------


def test_a_progress_deadline_below_the_boot_time_is_caught():
    doc = deployment(progressDeadlineSeconds=120)
    doc["spec"]["template"]["spec"]["containers"][0]["startupProbe"] = {
        "httpGet": {"path": "/readyz", "port": "http"},
        "periodSeconds": 5,
        "failureThreshold": 48,
    }
    findings = check_rollout_timing([doc])
    assert "rollout.progress-deadline" in rules(findings)


def test_min_ready_seconds_of_zero_is_caught():
    assert "rollout.min-ready" in rules(check_rollout_timing([deployment(minReadySeconds=0)]))


def test_a_routed_pod_without_a_drain_hook_is_caught():
    doc = deployment()
    del doc["spec"]["template"]["spec"]["containers"][0]["lifecycle"]
    assert "rollout.no-drain" in rules(check_rollout_timing([doc, service()]))


def test_an_unrouted_pod_needs_no_drain_hook():
    # A queue consumer is nobody's endpoint. It stops asking for work and
    # finishes what it holds; a sleep before SIGTERM would only delay that.
    doc = deployment()
    del doc["spec"]["template"]["spec"]["containers"][0]["lifecycle"]
    assert "rollout.no-drain" not in rules(check_rollout_timing([doc]))


def test_a_drain_longer_than_the_grace_period_is_caught():
    doc = deployment()
    doc["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = 5
    findings = check_rollout_timing([doc, service()])
    assert "rollout.drain-exceeds-grace" in rules(findings)


# --- jobs --------------------------------------------------------------------


def test_a_job_ttl_is_caught():
    findings = check_jobs([job(ttlSecondsAfterFinished=3600)])
    assert "job.ttl" in rules(findings)
    assert "the next sync re-runs the migration" in findings[0].message


def test_a_job_without_a_deadline_is_caught():
    doc = job()
    del doc["spec"]["activeDeadlineSeconds"]
    assert "job.no-deadline" in rules(check_jobs([doc]))


def test_a_migration_pod_that_the_service_selects_is_caught():
    # The Service selects on name and instance with no track label, because
    # that is how the canary receives traffic. A Job reusing those labels
    # becomes an endpoint, and requests land on a container running psql.
    findings = check_jobs([job(labels={"app": "x"}), service()])
    assert "job.is-service-endpoint" in rules(findings)
    assert "receives application traffic" in findings[0].message


def test_a_migration_pod_with_its_own_labels_is_fine():
    assert check_jobs([job(labels={"app": "x-migrate"}), service()]) == []


def test_a_migration_at_serving_priority_is_caught():
    doc = job()
    doc["spec"]["template"]["spec"]["priorityClassName"] = "serving"
    assert "job.priority" in rules(check_jobs([doc]))


# --- hpa ownership -----------------------------------------------------------


def test_a_committed_replica_count_beside_an_hpa_is_caught():
    findings = check_hpa_ownership([deployment(replicas=3), hpa()])
    assert "hpa.replicas-committed" in rules(findings)


def test_an_hpa_owned_deployment_omitting_replicas_is_fine():
    doc = deployment()
    del doc["spec"]["replicas"]
    assert check_hpa_ownership([doc, hpa()]) == []


def test_an_hpa_pointing_at_nothing_is_caught():
    assert "hpa.no-target" in rules(check_hpa_ownership([hpa()]))


def test_inverted_hpa_bounds_are_caught():
    doc = deployment()
    del doc["spec"]["replicas"]
    assert "hpa.bounds" in rules(check_hpa_ownership([doc, hpa(minReplicas=8)]))


def test_an_hpa_beside_a_canary_is_caught():
    doc = deployment()
    del doc["spec"]["replicas"]
    canary = deployment()
    canary["metadata"]["name"] = "app-canary"
    assert "hpa.canary-conflict" in rules(check_hpa_ownership([doc, canary, hpa()]))


# --- sync waves --------------------------------------------------------------


def test_a_migration_in_the_same_wave_as_the_rollout_is_caught():
    doc = job()
    doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] = "2"
    findings = check_sync_waves([service(), doc, deployment()])
    assert "wave.migration-after-rollout" in rules(findings)


def test_a_config_map_after_the_migration_is_caught():
    config = config_map(wave="2")
    assert "wave.config-after-migration" in rules(check_sync_waves([config, job()]))


def test_an_hpa_in_the_same_wave_as_its_target_is_caught():
    doc = hpa()
    doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] = "2"
    assert "wave.hpa-before-workload" in rules(check_sync_waves([deployment(), doc]))


def test_an_object_with_no_wave_annotation_is_caught():
    doc = deployment()
    doc["metadata"]["annotations"] = {}
    assert "wave.missing" in rules(check_sync_waves([doc]))


def test_the_shipped_wave_order_passes():
    assert check_sync_waves([service(), job(), deployment(), hpa(), pdb()]) == []


# --- rollback safety ---------------------------------------------------------


def test_a_contract_migration_beside_an_unmarked_deployment_is_caught():
    findings = check_rollback_safety([job(strategy="contract"), deployment()])
    assert "rollback.unmarked" in rules(findings)


def test_a_marked_deployment_beside_a_contract_migration_is_fine():
    doc = deployment()
    doc["metadata"]["annotations"]["deploy.sam-farooq.dev/rollback"] = "manual"
    assert check_rollback_safety([job(strategy="contract"), doc]) == []


def test_a_canary_beside_a_contract_migration_is_caught():
    canary = deployment()
    canary["metadata"]["name"] = "app-canary"
    canary["metadata"]["labels"]["deploy.sam-farooq.dev/track"] = "canary"
    canary["metadata"]["annotations"]["deploy.sam-farooq.dev/rollback"] = "manual"
    findings = check_rollback_safety([job(strategy="contract"), canary])
    assert "rollback.canary-on-contract" in rules(findings)


def test_an_expand_migration_needs_no_marking():
    assert check_rollback_safety([job(strategy="expand"), deployment()]) == []


# --- topology ----------------------------------------------------------------


def test_a_hard_topology_constraint_is_caught():
    doc = deployment()
    doc["spec"]["template"]["spec"]["topologySpreadConstraints"] = [
        {
            "maxSkew": 1,
            "topologyKey": "topology.kubernetes.io/zone",
            "whenUnsatisfiable": "DoNotSchedule",
        }
    ]
    findings = check_topology_spread([doc])
    assert "topology.hard-constraint" in rules(findings)
    assert "stays Pending" in findings[0].message
