from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

TestType = Literal["functional", "performance", "security", "integration", "api", "e2e"]
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


class QARun(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = Field(default_factory=datetime.utcnow)
    repo_url: str
    test_plan: TestPlan | None = None
    specialist_results: list[SpecialistResult] = []
    output_directory: str = ""
    completed_at: datetime | None = None
    success: bool = False
