from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

TestType = Literal["functional", "performance", "security", "integration", "api", "e2e", "contract"]
Priority = Literal["critical", "high", "medium", "low"]
Framework = Literal["pytest", "jest", "vitest", "locust", "k6", "zap", "playwright", "httpx"]


class TechStack(BaseModel):
    languages: list[str] = []
    frameworks: list[str] = []
    databases: list[str] = []
    test_frameworks_existing: list[str] = []
    package_manager: str | None = None
    container: str | None = None
    api_style: str | None = None


class TestScope(BaseModel):
    description: str
    files_to_examine: list[str] = []
    entry_points: list[str] = []
    data_models: list[str] = []
    dependencies: list[str] = []


class TestPlanEntry(BaseModel):
    test_type: TestType
    priority: Priority
    scope: TestScope
    suggested_framework: Framework
    estimated_files: int = 1
    rationale: str


class TestPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    repo_url: str = ""
    tech_stack: TechStack
    entries: list[TestPlanEntry]
    overall_risk_areas: list[str] = []
    notes: str = ""


class GeneratedTestFile(BaseModel):
    filename: str
    content: str
    test_type: TestType
    framework: str
    description: str


class SpecialistResult(BaseModel):
    test_type: TestType
    agent_id: str
    started_at: datetime
    completed_at: datetime | None = None
    generated_files: list[GeneratedTestFile] = []
    execution_results: dict[str, Any] = {}
    errors: list[str] = []
    token_usage: dict[str, int] = {}


# ── Platform / multi-repo models ─────────────────────────────────────────────

ServiceRole = Literal["frontend", "backend", "api_gateway", "worker", "infra", "docs"]
ContractType = Literal["rest", "grpc", "graphql", "message", "database"]


class ServiceDescriptor(BaseModel):
    """One named service within a platform, backed by a repo (or repo sub-path)."""
    name: str
    repo_url: str
    local_path: str = ""          # filled by PlatformOrchestrator after clone
    role: ServiceRole = "backend"
    doc_links: list[str] = []
    branch: str = "main"
    sparse_paths: list[str] = []


class ServiceContract(BaseModel):
    """A discovered dependency between two services."""
    consumer: str                 # service name that calls / depends on the provider
    provider: str                 # service name that exposes the interface
    contract_type: ContractType
    endpoints: list[str] = []    # REST paths / gRPC methods / topic names / table names
    schema_files: list[str] = [] # repo-relative paths to .proto / openapi / graphql / avro
    description: str = ""


class PlatformArchitecture(BaseModel):
    """Cross-service dependency graph emitted by the PlatformStrategist."""
    services: list[ServiceDescriptor]
    contracts: list[ServiceContract]
    shared_schemas: list[str] = []   # paths to schemas referenced by >1 service
    topology_notes: str = ""         # free-text on deployment / network topology


class ContractTestEntry(BaseModel):
    """One contract test task, passed to ContractTestAgent."""
    contract: ServiceContract
    consumer_framework: str          # e.g. "pact-python", "@pact-foundation/pact"
    provider_framework: str
    estimated_files: int = 2
    rationale: str = ""


class PlatformTestPlan(BaseModel):
    """The combined plan for an entire multi-service platform."""
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    platform_name: str
    architecture: PlatformArchitecture
    per_service_plans: dict[str, TestPlan] = {}   # service name → TestPlan
    contract_entries: list[ContractTestEntry] = []


class PlatformRun(BaseModel):
    """Top-level result for a full platform QA run."""
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = Field(default_factory=datetime.utcnow)
    platform_name: str
    platform_plan: PlatformTestPlan | None = None
    service_runs: dict[str, QARun] = {}           # service name → QARun
    contract_results: list[SpecialistResult] = []
    output_directory: str = ""
    completed_at: datetime | None = None
    success: bool = False


class QARun(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = Field(default_factory=datetime.utcnow)
    repo_url: str
    test_plan: TestPlan | None = None
    specialist_results: list[SpecialistResult] = []
    output_directory: str = ""
    completed_at: datetime | None = None
    success: bool = False
