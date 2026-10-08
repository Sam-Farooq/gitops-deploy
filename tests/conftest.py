"""Fixtures. Everything renders once per session, because helm costs 200ms a go.

A missing helm or kustomize binary skips the tests that need it, with one
exception: GITOPS_REQUIRE_TOOLS=1, which CI sets, turns the skip into a
failure. A suite that quietly drops its real checks on the machine that matters
is a green tick that means nothing.
"""

from __future__ import annotations

import pytest

from gitopsvalidate import ENVIRONMENTS, SERVICES
from gitopsvalidate.render import (
    REPO_ROOT,
    ToolMissing,
    helm_lint,
    kustomize_build,
    read_cluster,
    read_values,
    read_yaml,
    render_environment,
    tools_required,
)
from gitopsvalidate.workload import schedulable, totals


def _unavailable(exc: ToolMissing):
    if tools_required():
        pytest.fail(f"GITOPS_REQUIRE_TOOLS=1 is set and {exc}")
    pytest.skip(f"{exc}; set GITOPS_REQUIRE_TOOLS=1 to make this a failure")


def _guarded(render, *args):
    """Run one helm or kustomize call with the missing-binary guard around it.

    Every binary call a test makes goes through a fixture that goes through
    here. A test that calls helm_lint or kustomize_build itself gets a failure
    or a setup error on a machine without the binary, which is the opposite of
    what the docstring above promises.
    """
    try:
        return render(*args)
    except ToolMissing as exc:
        _unavailable(exc)


@pytest.fixture(scope="session")
def renders() -> dict:
    return {env: _guarded(render_environment, env, SERVICES) for env in ENVIRONMENTS}


@pytest.fixture(scope="session")
def lints() -> dict:
    return {
        (env, service): _guarded(helm_lint, service, env)
        for env in ENVIRONMENTS
        for service in SERVICES
    }


@pytest.fixture(scope="session")
def overlays() -> dict:
    return {env: _guarded(kustomize_build, env) for env in ENVIRONMENTS}


@pytest.fixture(scope="session")
def capacities() -> dict:
    return {env: schedulable(read_cluster(env)) for env in ENVIRONMENTS}


@pytest.fixture(scope="session")
def worst_cases(renders) -> dict:
    return {env: totals(renders[env].per_service) for env in ENVIRONMENTS}


@pytest.fixture(scope="session")
def values_by_key() -> dict:
    return {
        (env, service): read_values(service, env) for env in ENVIRONMENTS for service in SERVICES
    }


@pytest.fixture(scope="session")
def project() -> dict:
    return read_yaml(REPO_ROOT / "argocd" / "projects" / "platform.yaml")


@pytest.fixture(scope="session")
def service_appset() -> dict:
    return read_yaml(REPO_ROOT / "argocd" / "applicationsets" / "services.yaml")


@pytest.fixture(scope="session")
def overlay_appset() -> dict:
    return read_yaml(REPO_ROOT / "argocd" / "applicationsets" / "platform-overlays.yaml")


@pytest.fixture(scope="session")
def root_app() -> dict:
    return read_yaml(REPO_ROOT / "argocd" / "applications" / "root.yaml")
