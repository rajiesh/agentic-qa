"""Tests for platform models, config parser, and contract entry derivation."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agentic_qa.core.models import (
    ContractTestEntry,
    PlatformArchitecture,
    PlatformRun,
    PlatformTestPlan,
    ServiceContract,
    ServiceDescriptor,
)
from agentic_qa.core.platform_config import load_platform


# ── Model tests ──────────────────────────────────────────────────────────────

def test_service_descriptor_defaults():
    svc = ServiceDescriptor(name="auth", repo_url="https://github.com/org/auth")
    assert svc.branch == "main"
    assert svc.role == "backend"
    assert svc.sparse_paths == []
    assert svc.local_path == ""


def test_platform_architecture_roundtrip():
    arch = PlatformArchitecture(
        services=[
            ServiceDescriptor(name="api", repo_url="https://github.com/org/api"),
            ServiceDescriptor(name="web", repo_url="https://github.com/org/web", role="frontend"),
        ],
        contracts=[
            ServiceContract(
                consumer="web",
                provider="api",
                contract_type="rest",
                endpoints=["/todos", "/todos/{id}"],
            )
        ],
        topology_notes="web calls api over HTTP",
    )
    restored = PlatformArchitecture.model_validate_json(arch.model_dump_json())
    assert len(restored.services) == 2
    assert len(restored.contracts) == 1
    assert restored.contracts[0].consumer == "web"


def test_platform_test_plan_ids_are_unique():
    def _plan():
        return PlatformTestPlan(
            platform_name="demo",
            architecture=PlatformArchitecture(services=[], contracts=[]),
        )
    p1, p2 = _plan(), _plan()
    assert p1.plan_id != p2.plan_id


def test_platform_run_defaults():
    run = PlatformRun(platform_name="my-platform")
    assert run.platform_plan is None
    assert run.service_runs == {}
    assert run.contract_results == []
    assert run.success is False


def test_contract_test_entry_roundtrip():
    entry = ContractTestEntry(
        contract=ServiceContract(
            consumer="frontend",
            provider="backend",
            contract_type="rest",
            endpoints=["/api/v1/todos"],
        ),
        consumer_framework="@pact-foundation/pact",
        provider_framework="pact-python",
        estimated_files=3,
        rationale="Frontend calls backend REST API",
    )
    restored = ContractTestEntry.model_validate_json(entry.model_dump_json())
    assert restored.contract.consumer == "frontend"
    assert restored.consumer_framework == "@pact-foundation/pact"


# ── platform_config.py parser tests ──────────────────────────────────────────

def _write_yaml(tmp_path: Path, content: str) -> str:
    f = tmp_path / "platform.yaml"
    f.write_text(textwrap.dedent(content))
    return str(f)


def test_load_platform_multi_repo(tmp_path):
    path = _write_yaml(tmp_path, """
        name: my-platform
        services:
          - name: auth
            url: https://github.com/org/auth
            role: backend
            branch: develop
            doc_links:
              - https://wiki/auth
          - name: web
            url: https://github.com/org/web
            role: frontend
        docs:
          - https://docs.example.com
    """)
    name, services, docs = load_platform(path)
    assert name == "my-platform"
    assert len(services) == 2
    assert services[0].name == "auth"
    assert services[0].branch == "develop"
    assert services[0].doc_links == ["https://wiki/auth"]
    assert services[1].role == "frontend"
    assert docs == ["https://docs.example.com"]


def test_load_platform_monorepo(tmp_path):
    path = _write_yaml(tmp_path, """
        name: mono
        repos:
          - url: https://github.com/org/monorepo
            branch: main
            services:
              - name: api
                path: services/api
                role: backend
              - name: worker
                path: services/worker
                role: worker
    """)
    name, services, docs = load_platform(path)
    assert name == "mono"
    assert len(services) == 2
    # sparse_paths should be set to the sub-path
    assert services[0].sparse_paths == ["services/api"]
    assert services[1].sparse_paths == ["services/worker"]
    # both share same repo_url
    assert services[0].repo_url == services[1].repo_url == "https://github.com/org/monorepo"
    assert docs == []


def test_load_platform_no_services_raises(tmp_path):
    path = _write_yaml(tmp_path, "name: empty\n")
    with pytest.raises(ValueError, match="no services"):
        load_platform(path)


def test_load_platform_invalid_role_raises(tmp_path):
    path = _write_yaml(tmp_path, """
        name: bad
        services:
          - name: svc
            url: https://github.com/org/svc
            role: unknown_role
    """)
    with pytest.raises(ValueError, match="Unknown service role"):
        load_platform(path)
