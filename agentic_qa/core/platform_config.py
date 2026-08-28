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
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import ServiceDescriptor, ServiceRole


def load_platform(path: str) -> tuple[str, list[ServiceDescriptor], list[str]]:
    """
    Parse *path* and return:
        (platform_name, list[ServiceDescriptor], global_doc_links)
    """
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

    return name, services, global_docs


def _role(raw: str) -> ServiceRole:
    valid: set[str] = {"frontend", "backend", "api_gateway", "worker", "infra", "docs"}
    if raw not in valid:
        raise ValueError(f"Unknown service role '{raw}'. Valid: {valid}")
    return raw  # type: ignore[return-value]
