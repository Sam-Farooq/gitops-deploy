"""`python -m gitopsvalidate --env prod`.

Renders every chart and overlay for an environment, runs every rule, prints the
capacity arithmetic, and exits non-zero on any finding. The same entry point CI
runs, so a clean local run means what a green pipeline means.
"""

from __future__ import annotations

import argparse
import sys

from . import ENVIRONMENTS, SERVICES, __version__
from .argo import (
    check_applications,
    check_generator_covers_values,
    check_overlay_apps,
    check_project_allows,
    check_root_app,
    check_source_repos,
    generate,
)
from .quantity import format_cores, format_memory, percent_of
from .render import REPO_ROOT, read_cluster, read_values, read_yaml, render_environment
from .rules import Finding, check_capacity, check_limit_range, check_quota, check_render
from .workload import schedulable, totals


def _argo_findings() -> list[Finding]:
    argo_dir = REPO_ROOT / "argocd"
    project = read_yaml(argo_dir / "projects" / "platform.yaml")
    services = read_yaml(argo_dir / "applicationsets" / "services.yaml")
    overlays = read_yaml(argo_dir / "applicationsets" / "platform-overlays.yaml")
    root = read_yaml(argo_dir / "applications" / "root.yaml")
    values_by_key = {
        (env, service): read_values(service, env) for env in ENVIRONMENTS for service in SERVICES
    }
    applications = generate(services) + generate(overlays)
    findings = []
    findings += check_generator_covers_values(services, REPO_ROOT / "envs")
    findings += check_applications(applications, values_by_key)
    findings += check_overlay_apps(applications)
    findings += check_source_repos(project, [*applications, root])
    findings += check_root_app(root)
    return findings


def _environment_findings(env: str) -> tuple[list[Finding], str]:
    rendered = render_environment(env, SERVICES)
    cluster = read_cluster(env)
    worst = totals(rendered.per_service)
    capacity = schedulable(cluster)

    findings: list[Finding] = []
    for service, docs in rendered.per_service.items():
        findings += [
            Finding(item.rule, f"{env}/{service} {item.target}", item.message)
            for item in check_render(docs)
        ]
    findings += check_capacity(worst, capacity)
    findings += check_quota(rendered.overlay, worst, capacity)
    findings += check_limit_range(rendered.overlay, rendered.per_service)

    kinds = {doc["kind"] for doc in rendered.objects} | {doc["kind"] for doc in rendered.overlay}
    project = read_yaml(REPO_ROOT / "argocd" / "projects" / "platform.yaml")
    findings += check_project_allows(project, kinds, {f"platform-{env}"})

    def row(label: str, pods: str, cpu: str, memory: str) -> str:
        return f"  {label:<36}{pods:>5}  {cpu:>7}  {memory:>9}"

    lines = [f"{env}: worst case requests across {len(SERVICES)} services"]
    lines.append(row("", "pods", "cores", "memory"))
    for group in sorted(worst.groups, key=lambda item: -item.cpu):
        lines.append(
            row(group.name, str(group.pods), format_cores(group.cpu), format_memory(group.memory))
        )
    lines.append(
        row("worst case", str(worst.pods), format_cores(worst.cpu), format_memory(worst.memory))
    )
    lines.append(
        row(
            "allocatable, untainted pools",
            "",
            format_cores(capacity.allocatable_cpu),
            format_memory(capacity.allocatable_memory),
        )
    )
    lines.append(
        row(
            f"schedulable, {capacity.headroom_percent}% held back",
            "",
            format_cores(capacity.cpu),
            format_memory(capacity.memory),
        )
    )
    lines.append(
        row(
            "worst case as a share of allocatable",
            "",
            f"{percent_of(worst.cpu, capacity.allocatable_cpu):.1f}%",
            f"{percent_of(worst.memory, capacity.allocatable_memory):.1f}%",
        )
    )
    if capacity.excluded_cpu:
        lines.append(
            row(
                "excluded, taint not tolerated",
                "",
                format_cores(capacity.excluded_cpu),
                format_memory(capacity.excluded_memory),
            )
        )
    return findings, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gitopsvalidate", description=__doc__)
    parser.add_argument("--env", choices=ENVIRONMENTS, action="append", dest="envs")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    envs = args.envs or list(ENVIRONMENTS)

    findings = _argo_findings()
    for env in envs:
        env_findings, table = _environment_findings(env)
        print(table)
        print()
        findings += env_findings

    if not findings:
        print(f"no findings across {', '.join(envs)}")
        return 0
    print(f"{len(findings)} finding(s)")
    for finding in findings:
        print(f"  {finding}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
