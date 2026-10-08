"""Shelling out to helm and kustomize, and nothing else.

Both render locally. Neither reads a kubeconfig, contacts a cluster, or needs a
credential, which is what makes every check in this repo runnable in CI on a
pull request from a fork.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART = REPO_ROOT / "charts" / "service"


class ToolMissing(RuntimeError):
    """A required binary is not on PATH."""


class RenderFailed(RuntimeError):
    """helm or kustomize exited non-zero. The message is its stderr."""


def tool(name: str) -> str:
    """Absolute path to a binary, or ToolMissing.

    GITOPS_REQUIRE_TOOLS=1 is set in CI. Locally the tests skip the rendered
    assertions when helm is absent; in CI a missing binary has to be a failure,
    because a suite that silently drops its real checks is a green tick that
    means nothing.
    """
    found = shutil.which(name)
    if found:
        return found
    raise ToolMissing(f"{name} is not on PATH")


def tools_required() -> bool:
    return os.environ.get("GITOPS_REQUIRE_TOOLS") == "1"


def _run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RenderFailed(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def _documents(text: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def helm_template(service: str, env: str, extra: dict[str, str] | None = None) -> list[dict]:
    """Render charts/service for one service in one environment."""
    argv = [
        tool("helm"),
        "template",
        service,
        str(CHART),
        "--values",
        str(REPO_ROOT / "envs" / env / f"{service}.yaml"),
        "--namespace",
        f"platform-{env}",
    ]
    for key, value in (extra or {}).items():
        argv += ["--set", f"{key}={value}"]
    return _documents(_run(argv))


def helm_template_raises(service: str, env: str, extra: dict[str, str]) -> str:
    """Render with overrides that should be refused, and return the message.

    Raises AssertionError if the render succeeded, because a guard that no
    longer fires is the failure being tested for.
    """
    try:
        helm_template(service, env, extra)
    except RenderFailed as exc:
        return str(exc)
    raise AssertionError(f"expected {service}/{env} with {extra} to be refused, it rendered")


def helm_lint(service: str, env: str) -> str:
    argv = [
        tool("helm"),
        "lint",
        str(CHART),
        "--values",
        str(REPO_ROOT / "envs" / env / f"{service}.yaml"),
        "--strict",
    ]
    return _run(argv)


def kustomize_build(env: str) -> list[dict]:
    argv = [tool("kustomize"), "build", str(REPO_ROOT / "overlays" / env)]
    return _documents(_run(argv))


def read_yaml(path: Path) -> dict:
    with path.open() as handle:
        return yaml.safe_load(handle)


def read_values(service: str, env: str) -> dict:
    return read_yaml(REPO_ROOT / "envs" / env / f"{service}.yaml")


def read_cluster(env: str) -> dict:
    return read_yaml(REPO_ROOT / "envs" / env / "cluster.yaml")


@dataclass
class EnvRender:
    """Everything one environment renders to, in one object."""

    env: str
    per_service: dict[str, list[dict]] = field(default_factory=dict)
    overlay: list[dict] = field(default_factory=list)

    @property
    def objects(self) -> list[dict]:
        out: list[dict] = []
        for docs in self.per_service.values():
            out.extend(docs)
        return out

    def of_kind(self, kind: str) -> list[dict]:
        return [doc for doc in self.objects if doc.get("kind") == kind]

    def overlay_of_kind(self, kind: str) -> list[dict]:
        return [doc for doc in self.overlay if doc.get("kind") == kind]


def render_environment(env: str, services: tuple[str, ...]) -> EnvRender:
    rendered = EnvRender(env=env)
    for service in services:
        rendered.per_service[service] = helm_template(service, env)
    rendered.overlay = kustomize_build(env)
    return rendered
