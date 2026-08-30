"""
checkpoint.py — Persistent state for resumable platform runs.

A PlatformCheckpoint is written to disk after each significant unit of work
completes (clone, strategy, per-service QA, contract test). If the process
crashes or is killed mid-run, the next invocation can load the checkpoint and
skip everything that already finished.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ServiceRunState(BaseModel):
    """Tracks the lifecycle of one per-service QA run or contract test run."""

    status: Literal["pending", "running", "completed", "failed"] = "pending"
    run_id: str = ""              # QARun / SpecialistResult run_id for reference
    output_directory: str = ""   # where the output landed on disk
    error: str = ""              # last error message if status == "failed"


class ServiceScanResult(BaseModel):
    """
    Stores the output of one ServiceScannerAgent pass so the scanner is not
    re-run when the platform run is resumed from a checkpoint.
    """

    service_name: str
    summary_json: str          # ServiceSummary serialised as JSON
    token_usage: dict[str, int] = {}


class PlatformCheckpoint(BaseModel):
    """
    Full checkpoint state for a single platform run.

    Fields progress monotonically: once a phase is marked complete, its data
    is never overwritten by a later resume unless --no-resume explicitly clears
    the checkpoint.
    """

    checkpoint_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    platform_name: str
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # ── Phase 1: clone ──────────────────────────────────────────────────────
    # service_name → local_path. Present iff that service was cloned successfully.
    cloned_services: dict[str, str] = {}

    # ── Phase 2a: per-service scans (Phase 5 two-tier flow) ─────────────────
    # service_name → scan result. Present iff ServiceScannerAgent completed.
    scan_results: dict[str, ServiceScanResult] = {}

    # ── Phase 2b: strategy / synthesis ─────────────────────────────────────
    # Non-empty iff PlatformStrategist / PlatformSynthesizerAgent completed.
    architecture_json: str = ""

    # ── Phase 3: per-service QA ─────────────────────────────────────────────
    # service_name → state
    service_qa_states: dict[str, ServiceRunState] = {}

    # ── Phase 4: contract tests ─────────────────────────────────────────────
    # key is "{consumer}__{provider}" (double-underscore separator)
    contract_states: dict[str, ServiceRunState] = {}

    def reset_crashed_states(self) -> int:
        """
        Any "running" state means the process died mid-task. Reset those to
        "pending" so they are retried. Returns the count of states reset.
        """
        count = 0
        for state in list(self.service_qa_states.values()) + list(self.contract_states.values()):
            if state.status == "running":
                state.status = "pending"
                count += 1
        return count

    @staticmethod
    def contract_key(consumer: str, provider: str) -> str:
        return f"{consumer}__{provider}"
