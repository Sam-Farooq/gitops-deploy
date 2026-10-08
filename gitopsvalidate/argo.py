"""Checks on the Argo CD objects, against the Applications they generate.

The AppProject's allow lists and the ApplicationSet's generator elements are
claims about the rest of the repo. Nothing enforces them at apply time except
Argo CD itself, at which point the failure is a sync error on a Sunday.

The eight Applications are produced here rather than described: the template is
rendered per element, templatePatch is rendered and merged, and the rules run
against the resulting objects.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from .gotemplate import merge, render
from .rules import Finding

CLUSTER_SCOPED = frozenset({"Namespace", "PriorityClass"})


def elements(appset: dict) -> list[dict]:
    out: list[dict] = []
    for generator in appset["spec"]["generators"]:
        out.extend(generator.get("list", {}).get("elements", []))
    return out


def generate(appset: dict) -> list[dict]:
    """The Applications this ApplicationSet produces, as objects.

    The template's string fields are Go templates; templatePatch is a Go
    template that produces YAML. Both go through gotemplate.render, so an
    action it does not support is an error here instead of a difference between
    this repo and the cluster.
    """
    spec = appset["spec"]
    patch_source = spec.get("templatePatch")
    applications = []
    for element in elements(appset):
        rendered = json.loads(render(json.dumps(spec["template"]), element))
        if patch_source:
            patch = yaml.safe_load(render(patch_source, element)) or {}
            rendered = merge(rendered, patch)
        rendered["apiVersion"] = "argoproj.io/v1alpha1"
        rendered["kind"] = "Application"
        applications.append(rendered)
    return applications


def check_generator_covers_values(appset: dict, envs_dir: Path) -> list[Finding]:
    """Every values file has an element, and every element has a values file.

    goTemplateOptions missingkey=error catches a field the template asks for
    and the element lacks. It cannot catch an element naming a values file that
    is not there, because that is a path, not a key: the Application is
    generated, syncs, and fails on a file read.
    """
    findings = []
    on_disk = {
        (path.parent.name, path.stem)
        for path in envs_dir.glob("*/*.yaml")
        if path.stem != "cluster"
    }
    declared: set[tuple[str, str]] = set()
    for element in elements(appset):
        key = (element["env"], element["service"])
        if key in declared:
            findings.append(
                Finding("appset.duplicate", f"{key[1]}-{key[0]}", "appears twice in the generator")
            )
        declared.add(key)
    for env, service in sorted(on_disk - declared):
        findings.append(
            Finding(
                "appset.unreferenced-values",
                f"envs/{env}/{service}.yaml",
                "no generator element, so nothing deploys it and nothing says so",
            )
        )
    for env, service in sorted(declared - on_disk):
        findings.append(
            Finding(
                "appset.missing-values",
                f"{service}-{env}",
                f"generated against envs/{env}/{service}.yaml, which does not exist",
            )
        )
    return findings


def check_applications(
    applications: list[dict], values_by_key: dict[tuple[str, str], dict]
) -> list[Finding]:
    """The generated Applications against the values files they point at."""
    findings = []
    for app in applications:
        name = app["metadata"]["name"]
        spec = app["spec"]
        labels = app["metadata"]["labels"]
        if "deploy.sam-farooq.dev/service" not in labels:
            # An overlay Application. It deploys a Kustomize directory, has no
            # values file and no HPA, and is checked by check_overlay_apps.
            continue
        key = (
            labels["deploy.sam-farooq.dev/environment"],
            labels["deploy.sam-farooq.dev/service"],
        )
        values = values_by_key.get(key)
        if values is None:
            continue
        env, service = key

        value_files = spec["source"]["helm"]["valueFiles"]
        expected = f"../../envs/{env}/{service}.yaml"
        if value_files != [expected]:
            findings.append(
                Finding("app.values-path", name, f"points at {value_files}, expected {expected}")
            )
        if spec["destination"]["namespace"] != f"platform-{env}":
            findings.append(
                Finding(
                    "app.namespace",
                    name,
                    f"destination {spec['destination']['namespace']} is not platform-{env}",
                )
            )

        automated = spec.get("syncPolicy", {}).get("automated")
        strategy = values.get("migration", {}).get("strategy", "none")
        if strategy == "contract" and automated:
            findings.append(
                Finding(
                    "app.autosync-contract",
                    name,
                    "automated sync on an Application carrying a contract migration: the "
                    "column is dropped the moment the commit lands, at whatever hour that is",
                )
            )
        if automated and automated.get("selfHeal") and not automated.get("prune"):
            findings.append(
                Finding(
                    "app.selfheal-without-prune",
                    name,
                    "selfHeal re-creates what the repo declares and prune false leaves what "
                    "it no longer declares, so a renamed object exists twice",
                )
            )

        hpa_on = bool(values.get("autoscaling", {}).get("enabled"))
        ignored = spec.get("ignoreDifferences", [])
        replicas_ignored = any(
            "/spec/replicas" in entry.get("jsonPointers", []) for entry in ignored
        )
        if hpa_on and not replicas_ignored:
            findings.append(
                Finding(
                    "app.hpa-not-ignored",
                    name,
                    "autoscaling is on and /spec/replicas is not ignored, so a manual scale "
                    "leaves the Application OutOfSync against a field no manifest sets",
                )
            )
        if replicas_ignored and not hpa_on:
            findings.append(
                Finding(
                    "app.ignores-without-hpa",
                    name,
                    "ignores /spec/replicas with no HPA, which hides a real drift in the "
                    "replica count",
                )
            )
        options = spec.get("syncPolicy", {}).get("syncOptions", [])
        if ignored and "RespectIgnoreDifferences=true" not in options:
            findings.append(
                Finding(
                    "app.ignore-not-respected",
                    name,
                    "declares ignoreDifferences without RespectIgnoreDifferences=true, so the "
                    "sync still applies the committed value and only the diff is hidden",
                )
            )
        for entry in ignored:
            if entry.get("name") and entry["name"] != service:
                findings.append(
                    Finding(
                        "app.ignore-wrong-target",
                        name,
                        f"ignoreDifferences names {entry['name']}, not {service}",
                    )
                )
    return findings


def check_project_allows(project: dict, kinds: set[str], namespaces: set[str]) -> list[Finding]:
    """The AppProject's allow lists against the kinds the repo actually renders."""
    findings = []
    namespaced = {entry["kind"] for entry in project["spec"].get("namespaceResourceWhitelist", [])}
    cluster = {entry["kind"] for entry in project["spec"].get("clusterResourceWhitelist", [])}
    for kind in sorted(kinds):
        bucket = cluster if kind in CLUSTER_SCOPED else namespaced
        if kind not in bucket:
            findings.append(
                Finding(
                    "project.kind-not-allowed",
                    kind,
                    "rendered by this repo and absent from the project's allow list, so the "
                    "sync is refused with a message about the project",
                )
            )
    declared = {entry["namespace"] for entry in project["spec"]["destinations"]}
    for namespace in sorted(namespaces - declared):
        findings.append(
            Finding("project.destination", namespace, "not a destination on the project")
        )
    return findings


def check_source_repos(project: dict, applications: list[dict]) -> list[Finding]:
    """Every Application's repoURL has to be one the project permits."""
    findings = []
    allowed = set(project["spec"]["sourceRepos"])
    for app in applications:
        spec = app["spec"]
        if spec.get("project") == "default":
            continue
        source = spec.get("source")
        if source and source["repoURL"] not in allowed:
            findings.append(
                Finding(
                    "project.source-repo",
                    app["metadata"]["name"],
                    f"{source['repoURL']} is not in the project's sourceRepos",
                )
            )
    return findings


def check_root_app(root: dict) -> list[Finding]:
    """The root app must not manage the directory it lives in.

    A self-managing Application that goes wrong removes the thing that would
    have fixed it, and the way back is kubectl against the cluster, which is
    the state this whole repo exists to avoid.
    """
    directory = root["spec"]["source"].get("directory", {})
    if "applications" not in directory.get("exclude", ""):
        return [
            Finding(
                "root.self-managed",
                root["metadata"]["name"],
                "recurses into applications/, where its own manifest lives",
            )
        ]
    return []


def check_overlay_apps(applications: list[dict]) -> list[Finding]:
    """The Applications that own a namespace, rather than something inside one.

    These sync the namespace itself, its quota and its deny-all policy. Prune
    on one of them means a generator bug deletes the namespace and everything
    in it, and the service Applications then spend their retries re-creating
    objects into nothing.
    """
    findings = []
    for app in applications:
        if "deploy.sam-farooq.dev/service" in app["metadata"]["labels"]:
            continue
        name = app["metadata"]["name"]
        spec = app["spec"]
        env = app["metadata"]["labels"]["deploy.sam-farooq.dev/environment"]
        automated = spec.get("syncPolicy", {}).get("automated", {})
        if automated.get("prune"):
            findings.append(
                Finding(
                    "overlay.prune",
                    name,
                    "prune on the Application that owns the namespace: a dropped generator "
                    "element deletes the namespace and everything running in it",
                )
            )
        if spec["source"]["path"] != f"overlays/{env}":
            findings.append(
                Finding(
                    "overlay.path",
                    name,
                    f"syncs {spec['source']['path']} into platform-{env}",
                )
            )
    return findings
