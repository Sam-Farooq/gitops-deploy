"""Every chart, rendered for every environment, asserted on.

Eight renders. The rules in gitopsvalidate.rules run over all of them, and then
the facts a rule cannot state generically are checked here against what the
values files actually say.
"""

import pytest

from gitopsvalidate import ENVIRONMENTS, SERVICES
from gitopsvalidate.render import read_values
from gitopsvalidate.rules import check_render

pytestmark = pytest.mark.renders

CASES = [(env, service) for env in ENVIRONMENTS for service in SERVICES]


@pytest.mark.parametrize(("env", "service"), CASES)
def test_every_rule_passes_on_every_render(renders, env, service):
    findings = check_render(renders[env].per_service[service])
    assert findings == [], "\n".join(str(finding) for finding in findings)


@pytest.mark.parametrize(("env", "service"), CASES)
def test_helm_lint_is_clean(lints, env, service):
    # --strict, so a warning is a failure. Without it lint returns 0 on a chart
    # with no icon, no description and a values file the schema rejects.
    assert "0 chart(s) failed" in lints[(env, service)]


@pytest.mark.parametrize(("env", "service"), CASES)
def test_every_container_declares_its_own_requests(renders, env, service):
    # The LimitRange has defaultRequest values. Relying on them means the quota
    # counts a number nobody chose, and the pod spec does not say what it is.
    for doc in renders[env].per_service[service]:
        if doc["kind"] not in ("Deployment", "Job"):
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            requests = container["resources"]["requests"]
            assert "cpu" in requests and "memory" in requests, doc["metadata"]["name"]


@pytest.mark.parametrize(("env", "service"), CASES)
def test_no_container_runs_as_root_or_can_write_its_root_filesystem(renders, env, service):
    for doc in renders[env].per_service[service]:
        if doc["kind"] not in ("Deployment", "Job"):
            continue
        pod = doc["spec"]["template"]["spec"]
        assert pod["securityContext"]["runAsNonRoot"] is True
        for container in pod["containers"]:
            security = container["securityContext"]
            assert security["readOnlyRootFilesystem"] is True
            assert security["allowPrivilegeEscalation"] is False
            assert security["capabilities"]["drop"] == ["ALL"]


@pytest.mark.parametrize(("env", "service"), CASES)
def test_a_read_only_root_filesystem_has_somewhere_to_write(renders, env, service):
    # readOnlyRootFilesystem without a writable mount is a container that
    # starts and then fails on its first temp file, which looks like an
    # application bug.
    for doc in renders[env].per_service[service]:
        if doc["kind"] != "Deployment":
            continue
        pod = doc["spec"]["template"]["spec"]
        mounts = {mount["mountPath"] for mount in pod["containers"][0]["volumeMounts"]}
        assert "/tmp" in mounts
        assert any(volume["name"] == "tmp" for volume in pod["volumes"])


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_service_routes_to_the_port_the_container_names(renders, env):
    for service in SERVICES:
        docs = renders[env].per_service[service]
        services = [doc for doc in docs if doc["kind"] == "Service"]
        if not services:
            continue
        target = services[0]["spec"]["ports"][0]["targetPort"]
        for doc in docs:
            if doc["kind"] != "Deployment":
                continue
            ports = {
                port["name"] for port in doc["spec"]["template"]["spec"]["containers"][0]["ports"]
            }
            assert target in ports, f"{service}: Service routes to {target}, container has {ports}"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_a_worker_gets_no_service_and_no_http_probes(renders, env):
    docs = renders[env].per_service["embedding-worker"]
    assert [doc for doc in docs if doc["kind"] == "Service"] == []
    container = next(doc for doc in docs if doc["kind"] == "Deployment")
    container = container["spec"]["template"]["spec"]["containers"][0]
    assert "httpGet" not in container["livenessProbe"]
    assert container["readinessProbe"]["exec"]["command"] == ["/app/bin/broker-connected"]
    # Readiness is still there. Nothing routes to it, but it gates the rollout
    # and the PodDisruptionBudget counts it.


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_a_worker_replaces_in_place_and_a_service_surges_first(renders, env):
    worker = next(
        doc for doc in renders[env].per_service["embedding-worker"] if doc["kind"] == "Deployment"
    )
    rolling = worker["spec"]["strategy"]["rollingUpdate"]
    assert (rolling["maxUnavailable"], rolling["maxSurge"]) == (1, 0)
    served = next(
        doc
        for doc in renders[env].per_service["rag-agent"]
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "rag-agent"
    )
    rolling = served["spec"]["strategy"]["rollingUpdate"]
    assert (rolling["maxUnavailable"], rolling["maxSurge"]) == (0, 1)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_startup_probe_threshold_matches_the_declared_boot_budget(renders, env):
    for service in ("rag-agent", "model-serving", "hybrid-search"):
        values = read_values(service, env)
        budget = values["probes"]["startupSeconds"]
        period = values["probes"]["startupPeriodSeconds"]
        for doc in renders[env].per_service[service]:
            if doc["kind"] != "Deployment":
                continue
            startup = doc["spec"]["template"]["spec"]["containers"][0]["startupProbe"]
            assert startup["periodSeconds"] * startup["failureThreshold"] == budget
            assert startup["periodSeconds"] == period


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_canary_differs_from_stable_only_in_its_tag_and_its_track(renders, env):
    docs = renders[env].per_service["rag-agent"]
    stable = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "rag-agent"
    )
    canary = next(doc for doc in docs if doc["metadata"]["name"] == "rag-agent-canary")
    values = read_values("rag-agent", env)
    assert canary["spec"]["template"]["spec"]["containers"][0]["image"].endswith(
        values["canary"]["tag"]
    )
    assert stable["spec"]["template"]["spec"]["containers"][0]["image"].endswith(
        values["image"]["tag"]
    )

    def normalise(deployment):
        pod = deployment["spec"]["template"]
        spec = {key: value for key, value in pod["spec"].items() if key != "containers"}
        container = dict(pod["spec"]["containers"][0])
        container.pop("image")
        container["env"] = [item for item in container["env"] if item["name"] != "DEPLOY_TRACK"]
        return spec, container

    # Anything else differing would make the canary a test of the wrong thing.
    assert normalise(stable) == normalise(canary)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_canary_takes_pods_from_stable_rather_than_adding_them(renders, env):
    values = read_values("rag-agent", env)
    docs = renders[env].per_service["rag-agent"]
    stable = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "rag-agent"
    )
    canary = next(doc for doc in docs if doc["metadata"]["name"] == "rag-agent-canary")
    total = values["replicas"]
    assert stable["spec"]["replicas"] + canary["spec"]["replicas"] == total
    assert canary["spec"]["replicas"] * 100 // total == values["canary"]["weight"]


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_canary_carries_its_promotion_criteria_on_the_object(renders, env):
    canary = next(
        doc
        for doc in renders[env].per_service["rag-agent"]
        if doc["metadata"]["name"] == "rag-agent-canary"
    )
    annotations = canary["metadata"]["annotations"]
    assert annotations["deploy.sam-farooq.dev/promotion-criteria"] == "docs/canary.md#rag-agent"
    assert annotations["deploy.sam-farooq.dev/analysis-window"]
    assert annotations["deploy.sam-farooq.dev/canary-pods"] == str(canary["spec"]["replicas"])


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_an_hpa_owned_deployment_commits_no_replica_count(renders, env):
    for service in ("model-serving", "hybrid-search"):
        for doc in renders[env].per_service[service]:
            if doc["kind"] == "Deployment":
                assert "replicas" not in doc["spec"], f"{env}/{service}"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_migration_job_name_carries_the_content_hash(renders, env):
    values = read_values("rag-agent", env)
    job = next(doc for doc in renders[env].per_service["rag-agent"] if doc["kind"] == "Job")
    assert job["metadata"]["name"] == f"rag-agent-migrate-{values['migration']['sha'][:12]}"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_changing_the_migration_content_changes_the_job_name(renders, env):
    from gitopsvalidate.render import helm_template

    original = next(doc for doc in renders[env].per_service["rag-agent"] if doc["kind"] == "Job")
    edited = helm_template("rag-agent", env, {"migration.sha": "a" * 64})
    changed = next(doc for doc in edited if doc["kind"] == "Job")
    # A Job's pod template is immutable. Without this the sync fails with
    # "field is immutable" and nothing in the message mentions migrations.
    assert original["metadata"]["name"] != changed["metadata"]["name"]
    assert changed["metadata"]["name"].endswith("aaaaaaaaaaaa")


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_contract_migration_marks_its_deployment_unrollbackable(renders, env):
    docs = renders[env].per_service["hybrid-search"]
    job = next(doc for doc in docs if doc["kind"] == "Job")
    assert job["metadata"]["labels"]["deploy.sam-farooq.dev/migration-strategy"] == "contract"
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    annotations = deployment["metadata"]["annotations"]
    assert annotations["deploy.sam-farooq.dev/rollback"] == "manual"
    assert "not reversible" in annotations["deploy.sam-farooq.dev/rollback-reason"]


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_an_expand_migration_leaves_rollback_automatic(renders, env):
    deployment = next(
        doc
        for doc in renders[env].per_service["rag-agent"]
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "rag-agent"
    )
    assert deployment["metadata"]["annotations"]["deploy.sam-farooq.dev/rollback"] == "automatic"


@pytest.mark.parametrize(("env", "service"), CASES)
def test_the_config_checksum_rolls_the_pods_when_the_config_changes(renders, env, service):
    from gitopsvalidate.render import helm_template

    def checksums(docs):
        return {
            doc["metadata"]["name"]: doc["spec"]["template"]["metadata"]["annotations"][
                "deploy.sam-farooq.dev/config-checksum"
            ]
            for doc in docs
            if doc["kind"] == "Deployment"
        }

    before = checksums(renders[env].per_service[service])
    after = checksums(helm_template(service, env, {"logLevel": "warn"}))
    # Without this annotation a config-only commit syncs clean and changes
    # nothing, because the ConfigMap is mounted as env and env is read once.
    assert before and before.keys() == after.keys()
    assert all(before[name] != after[name] for name in before)


@pytest.mark.parametrize(("env", "service"), CASES)
def test_the_service_account_mounts_no_api_token(renders, env, service):
    account = next(
        doc for doc in renders[env].per_service[service] if doc["kind"] == "ServiceAccount"
    )
    assert account["automountServiceAccountToken"] is False
