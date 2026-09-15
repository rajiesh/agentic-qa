"""
Parse a platform.yaml descriptor into a list of ServiceDescriptors.

Supported YAML shapes
─────────────────────
Multi-repo (one entry per service):

    name: my-platform
    services:
      - name: auth-service
        url: https://github.com/org/auth
        role: backend
        branch: main
        doc_links: [https://wiki/auth]
        sparse_paths: []
      - name: frontend
        url: https://github.com/org/web
        role: frontend
    docs:
      - https://github.com/org/api-specs

Monorepo (multiple services inside one repo, addressed by sub-path):

    name: mono-platform
    repos:
      - url: https://github.com/org/monorepo
        branch: main
        services:
          - name: auth
            path: services/auth
            role: backend
          - name: payments
            path: services/payments
          - name: web
            path: apps/web
            role: frontend
    docs:
      - https://confluence.internal/arch

Both shapes can be mixed freely in the same file.

Subsystems (optional) — a doc describing a cross-cutting concern that spans a
NAMED SUBSET of services (not just one, not necessarily all):

    subsystems:
      - name: payments-subsystem
        services: [payments, auth]   # must match names declared above
        docs:
          - https://wiki.internal/payments-architecture
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import DocSubsystem, PlatformDescriptor, ServiceDescriptor, ServiceRole


def load_platform(path: str) -> PlatformDescriptor:
    """Parse *path* into a PlatformDescriptor (services, system-wide docs, subsystems)."""
    raw = yaml.safe_load(Path(path).read_text())
    name: str = raw.get("name", Path(path).stem)
    global_docs: list[str] = raw.get("docs", [])
    services: list[ServiceDescriptor] = []

    # ── Multi-repo style ──────────────────────────────────────────────────────
    for svc in raw.get("services", []):
        services.append(
            ServiceDescriptor(
                name=svc["name"],
                repo_url=svc["url"],
                role=_role(svc.get("role", "backend")),
                doc_links=svc.get("doc_links", []),
                branch=svc.get("branch", "main"),
                sparse_paths=svc.get("sparse_paths", []),
            )
        )

    # ── Monorepo style ────────────────────────────────────────────────────────
    for repo_entry in raw.get("repos", []):
        repo_url: str = repo_entry["url"]
        repo_branch: str = repo_entry.get("branch", "main")
        for svc in repo_entry.get("services", []):
            svc_path: str = svc.get("path", "")
            services.append(
                ServiceDescriptor(
                    name=svc["name"],
                    repo_url=repo_url,
                    role=_role(svc.get("role", "backend")),
                    doc_links=svc.get("doc_links", []),
                    branch=repo_branch,
                    # Use sparse_paths to limit the clone to this service's sub-folder
                    sparse_paths=svc.get("sparse_paths", [svc_path] if svc_path else []),
                )
            )

    if not services:
        raise ValueError(f"platform.yaml '{path}' contains no services.")

    known_names = {s.name for s in services}
    subsystems: list[DocSubsystem] = []
    seen_subsystem_names: set[str] = set()
    for sub in raw.get("subsystems", []):
        sub_name: str = sub["name"]
        if sub_name in seen_subsystem_names:
            raise ValueError(f"Duplicate subsystem name '{sub_name}' in platform.yaml '{path}'.")
        seen_subsystem_names.add(sub_name)

        sub_services: list[str] = sub.get("services", [])
        if not sub_services:
            raise ValueError(f"Subsystem '{sub_name}' has no services listed.")
        unknown = [s for s in sub_services if s not in known_names]
        if unknown:
            raise ValueError(
                f"Subsystem '{sub_name}' references unknown service(s) {unknown}. "
                f"Valid services: {sorted(known_names)}"
            )
        subsystems.append(DocSubsystem(name=sub_name, services=sub_services, docs=sub.get("docs", [])))

    return PlatformDescriptor(
        platform_name=name, services=services, system_docs=global_docs, subsystems=subsystems,
    )


def _role(raw: str) -> ServiceRole:
    valid: set[str] = {"frontend", "backend", "api_gateway", "worker", "infra", "docs"}
    if raw not in valid:
        raise ValueError(f"Unknown service role '{raw}'. Valid: {valid}")
    return raw  # type: ignore[return-value]
