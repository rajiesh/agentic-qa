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
    contract: TestTypeConfig = Field(default_factory=TestTypeConfig)  # enabled by default; PlatformStrategist gates it


class QAConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", populate_by_name=True)

    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    model: str = "claude-sonnet-4-6"
    max_tokens_strategist: int = 8192
    max_tokens_specialist: int = 16384
    output_dir: str = "outputs"
    max_repo_size_mb: int = 500
    run_generated_tests: bool = False   # reserved: run generated tests after generation
    lint_generated: bool = True         # run ruff/eslint on generated files after each specialist
    concurrency_limit: int = 3          # specialist parallelism within one repo
    repo_concurrency_limit: int = 5     # outer repo-level parallelism gate
    max_retries: int = 5                # API call retry attempts on rate-limit / server errors
    retry_base_wait_secs: float = 5.0   # exponential-backoff base wait (seconds)
    retry_max_wait_secs: float = 120.0  # cap on backoff wait (seconds)
    # ── Context window management ──────────────────────────────────────────────
    max_tokens_session: int = 4096              # max_tokens for the interactive session agent
    max_context_tool_pairs: int = 10            # sliding window: max tool-use/result pairs kept
    session_history_max_turns: int = 20         # interactive session conversation window (turns)
    # ── Checkpointing ─────────────────────────────────────────────────────────
    enable_checkpointing: bool = True           # save/load checkpoint.json during platform runs
    checkpoint_dir: str | None = None          # override checkpoint location (default: output_dir/platform_name)
    # ── Phase 5: Two-tier scanner/synthesizer ─────────────────────────────────
    scanner_concurrency_limit: int = 10         # parallel ServiceScannerAgents
    scanner_max_iterations: int = 15            # per-service scanner loop iterations
    synthesizer_max_iterations: int = 8         # synthesis loop (no file reads)
    # ── Phase 6: Cost budget enforcement ──────────────────────────────────────
    cost_budget_usd: float | None = None        # None = unlimited; e.g. 5.00 = $5 cap
    specialists: SpecialistConfig = Field(default_factory=SpecialistConfig)
