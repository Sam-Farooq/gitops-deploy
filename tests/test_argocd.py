"""The Argo CD objects, and the eight Applications the ApplicationSet produces.

The Applications are generated here rather than described: the template and its
templatePatch are rendered per element and merged, exactly as the controller
does it, so these are assertions on objects and not on text.
"""

import pytest

from gitopsvalidate import ENVIRONMENTS, SERVICES
from gitopsvalidate.argo import (
    check_applications,
    check_generator_covers_values,
    check_overlay_apps,
    check_project_allows,
    check_root_app,
    check_source_repos,
    elements,
    generate,
)
from gitopsvalidate.render import REPO_ROOT


@pytest.fixture(scope="module")
def applications(service_appset, overlay_appset):
    return generate(service_appset) + generate(overlay_appset)


def by_name(applications):
    return {app["metadata"]["name"]: app for app in applications}


def test_one_application_per_values_file(service_appset):
    assert len(elements(service_appset)) == len(ENVIRONMENTS) * len(SERVICES)
    findings = check_generator_covers_values(service_appset, REPO_ROOT / "envs")
    assert findings == [], "\n".join(str(finding) for finding in findings)


def test_every_generated_application_agrees_with_its_values_file(applications, values_by_key):
    findings = check_applications(applications, values_by_key)
    assert findings == [], "\n".join(str(finding) for finding in findings)


def test_the_overlay_applications_do_not_prune(applications):
    findings = check_overlay_apps(applications)
    assert findings == [], "\n".join(str(finding) for finding in findings)


def test_the_generated_names_are_service_and_environment(applications):
    names = set(by_name(applications))
    assert "rag-agent-prod" in names
    assert "platform-staging" in names
    assert len(names) == len(ENVIRONMENTS) * len(SERVICES) + len(ENVIRONMENTS)


def test_the_patch_adds_a_replica_ignore_only_where_an_hpa_exists(applications):
    apps = by_name(applications)
    assert apps["model-serving-prod"]["spec"]["ignoreDifferences"][0]["jsonPointers"] == [
        "/spec/replicas"
    ]
    # rag-agent has a fixed replica count and a canary. Ignoring the field
    # there would hide a real drift.
    assert "ignoreDifferences" not in apps["rag-agent-prod"]["spec"]


def test_the_ignore_names_the_services_own_deployment(applications):
    entry = by_name(applications)["hybrid-search-staging"]["spec"]["ignoreDifferences"][0]
    assert entry["name"] == "hybrid-search"
    assert entry["kind"] == "Deployment"


def test_ignore_differences_is_respected_rather_than_only_displayed(applications):
    # Without RespectIgnoreDifferences the sync still applies the committed
    # value: the UI goes quiet and the HPA keeps being overwritten, which is
    # worse than the visible version.
    for app in applications:
        if app["spec"].get("ignoreDifferences"):
            options = app["spec"]["syncPolicy"]["syncOptions"]
            assert "RespectIgnoreDifferences=true" in options


def test_the_contract_migration_applications_are_the_only_manual_ones(applications):
    manual = sorted(
        app["metadata"]["name"]
        for app in applications
        if "automated" not in app["spec"].get("syncPolicy", {})
    )
    assert manual == ["hybrid-search-prod", "hybrid-search-staging"]


def test_every_other_application_self_heals_and_prunes(applications):
    for app in applications:
        if "deploy.sam-farooq.dev/service" not in app["metadata"]["labels"]:
            continue
        automated = app["spec"].get("syncPolicy", {}).get("automated")
        if automated is None:
            continue
        assert automated["prune"] is True
        assert automated["selfHeal"] is True


def test_the_patch_always_sets_something_so_it_is_never_an_empty_spec(applications):
    # A patch where every key is behind an `if` renders as a bare `spec:` when
    # no branch fires, and a merge patch of spec: null empties the Application.
    for app in applications:
        assert app["spec"]["revisionHistoryLimit"] == 5


def test_no_application_creates_its_own_namespace(applications):
    # The namespace belongs to overlays/, which sets its Pod Security labels
    # and its quota. Two owners means whichever syncs second wins.
    for app in applications:
        assert "CreateNamespace=false" in app["spec"]["syncPolicy"]["syncOptions"]


def test_every_values_path_resolves_from_the_chart_directory(applications):
    for app in applications:
        helm = app["spec"]["source"].get("helm")
        if not helm:
            continue
        for relative in helm["valueFiles"]:
            resolved = (REPO_ROOT / "charts" / "service" / relative).resolve()
            assert resolved.is_file(), relative


def test_the_release_name_is_the_service_name(applications):
    # Which is what keeps the object names readable: the chart is called
    # "service" and that word appears in nothing it renders.
    for app in applications:
        helm = app["spec"]["source"].get("helm")
        if helm:
            assert helm["releaseName"] == app["metadata"]["labels"]["deploy.sam-farooq.dev/service"]


def test_the_project_allows_every_kind_the_repo_renders(renders, project):
    kinds = set()
    for env in ENVIRONMENTS:
        kinds |= {doc["kind"] for doc in renders[env].objects}
        kinds |= {doc["kind"] for doc in renders[env].overlay}
    findings = check_project_allows(project, kinds, {f"platform-{env}" for env in ENVIRONMENTS})
    assert findings == [], "\n".join(str(finding) for finding in findings)


def test_the_project_does_not_allow_secrets(project):
    # Nothing here creates one. The database password is generated by Terraform
    # into Secrets Manager and read through IRSA, so a Secret appearing in a
    # sync would be a committed one.
    allowed = {entry["kind"] for entry in project["spec"]["namespaceResourceWhitelist"]}
    assert "Secret" not in allowed


def test_the_project_warns_about_orphans_rather_than_deleting_them(project):
    orphaned = project["spec"]["orphanedResources"]
    assert orphaned["warn"] is True
    # The first thing an automatic cleanup finds is something a human created
    # during an incident.
    assert "delete" not in orphaned


def test_every_application_sources_a_repo_the_project_permits(applications, project, root_app):
    findings = check_source_repos(project, [*applications, root_app])
    assert findings == [], "\n".join(str(finding) for finding in findings)


def test_the_root_app_does_not_manage_itself(root_app):
    assert check_root_app(root_app) == []
    assert root_app["spec"]["source"]["directory"]["recurse"] is True


def test_the_root_app_is_in_the_default_project(root_app):
    # It creates the platform project, and a project cannot be the project of
    # the Application that creates it.
    assert root_app["spec"]["project"] == "default"


def test_the_root_app_source_path_holds_the_project_and_the_sets(root_app):
    path = REPO_ROOT / root_app["spec"]["source"]["path"]
    assert (path / "projects" / "platform.yaml").is_file()
    assert sorted(item.name for item in (path / "applicationsets").iterdir()) == [
        "platform-overlays.yaml",
        "services.yaml",
    ]


def test_a_broken_generator_element_is_caught(service_appset):
    import copy

    broken = copy.deepcopy(service_appset)
    broken["spec"]["generators"][0]["list"]["elements"][0]["service"] = "nonexistent"
    findings = check_generator_covers_values(broken, REPO_ROOT / "envs")
    rules = {finding.rule for finding in findings}
    assert rules == {"appset.missing-values", "appset.unreferenced-values"}


def test_an_autosynced_contract_migration_is_caught(service_appset, values_by_key):
    import copy

    broken = copy.deepcopy(service_appset)
    for element in broken["spec"]["generators"][0]["list"]["elements"]:
        element["autoSync"] = True
    findings = check_applications(generate(broken), values_by_key)
    offenders = {finding.target for finding in findings if finding.rule == "app.autosync-contract"}
    assert offenders == {"hybrid-search-prod", "hybrid-search-staging"}


def test_dropping_respect_ignore_differences_is_caught(service_appset, values_by_key):
    import copy

    broken = copy.deepcopy(service_appset)
    options = broken["spec"]["template"]["spec"]["syncPolicy"]["syncOptions"]
    options.remove("RespectIgnoreDifferences=true")
    findings = check_applications(generate(broken), values_by_key)
    assert any(finding.rule == "app.ignore-not-respected" for finding in findings)


def test_pruning_the_overlay_application_is_caught(overlay_appset):
    import copy

    broken = copy.deepcopy(overlay_appset)
    broken["spec"]["template"]["spec"]["syncPolicy"]["automated"]["prune"] = True
    findings = check_overlay_apps(generate(broken))
    assert [finding.rule for finding in findings] == ["overlay.prune"] * len(ENVIRONMENTS)
