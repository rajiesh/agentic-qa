from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RepoTarget(BaseModel):
    url: str
    doc_links: list[str] = []
    branch: str = "main"
    sparse_paths: list[str] = []


class TestTypeConfig(BaseModel):
    enabled: bool = True
    priority: Literal["critical", "high", "medium", "low"] = "medium"
    max_files: int = 5


class SpecialistConfig(BaseModel):
    functional: TestTypeConfig = Field(default_factory=TestTypeConfig)
    performance: TestTypeConfig = Field(default_factory=TestTypeConfig)
    security: TestTypeConfig = Field(default_factory=TestTypeConfig)
    integration: TestTypeConfig = Field(default_factory=lambda: TestTypeConfig(enabled=False))
    api: TestTypeConfig = Field(default_factory=lambda: TestTypeConfig(enabled=False))
    e2e: TestTypeConfig = Field(default_factory=TestTypeConfig)  # enabled by default; Strategist gates it


class QAConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", populate_by_name=True)

    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    model: str = "claude-sonnet-4-6"
    max_tokens_strategist: int = 8192
    max_tokens_specialist: int = 16384
    output_dir: str = "outputs"
    max_repo_size_mb: int = 500
    run_generated_tests: bool = False
    concurrency_limit: int = 3
    specialists: SpecialistConfig = Field(default_factory=SpecialistConfig)
