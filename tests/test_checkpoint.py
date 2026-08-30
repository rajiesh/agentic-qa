"""
Tests for Phase 4 checkpointing infrastructure:
- PlatformCheckpoint model behaviour
- CheckpointManager save / load / delete / atomic write
- Resume skip logic (clone, strategy, per-service QA, contracts)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentic_qa.core.checkpoint import PlatformCheckpoint, ServiceRunState
from agentic_qa.core.checkpoint_manager import CheckpointManager


# ── Helpers ────────────────────────────────────────────────────────────────────


def _mgr(tmp_path: Path, platform_name: str = "test-platform") -> CheckpointManager:
    return CheckpointManager(
        platform_name=platform_name,
        output_dir=str(tmp_path),
    )


def _fresh(platform_name: str = "test-platform") -> PlatformCheckpoint:
    return PlatformCheckpoint(platform_name=platform_name)


# ── PlatformCheckpoint model ───────────────────────────────────────────────────


class TestPlatformCheckpoint:
    def test_defaults(self):
        ckpt = _fresh()
        assert ckpt.platform_name == "test-platform"
        assert ckpt.run_id  # non-empty UUID
        assert ckpt.cloned_services == {}
        assert ckpt.architecture_json == ""
        assert ckpt.service_qa_states == {}
        assert ckpt.contract_states == {}

    def test_contract_key_format(self):
        key = PlatformCheckpoint.contract_key("auth", "payments")
        assert key == "auth__payments"

    def test_reset_crashed_states_resets_running(self):
        ckpt = _fresh()
        ckpt.service_qa_states["svc-a"] = ServiceRunState(status="running")
        ckpt.service_qa_states["svc-b"] = ServiceRunState(status="completed")
        ckpt.contract_states["a__b"] = ServiceRunState(status="running")

        count = ckpt.reset_crashed_states()

        assert count == 2
        assert ckpt.service_qa_states["svc-a"].status == "pending"
        assert ckpt.service_qa_states["svc-b"].status == "completed"  # unchanged
        assert ckpt.contract_states["a__b"].status == "pending"

    def test_reset_crashed_states_nothing_to_reset(self):
        ckpt = _fresh()
        ckpt.service_qa_states["svc-a"] = ServiceRunState(status="completed")
        assert ckpt.reset_crashed_states() == 0

    def test_json_roundtrip(self):
        ckpt = _fresh()
        ckpt.cloned_services["svc-a"] = "/tmp/svc-a"
        ckpt.architecture_json = '{"services": []}'
        ckpt.service_qa_states["svc-a"] = ServiceRunState(status="completed", run_id="abc123")

        serialised = ckpt.model_dump_json()
        restored = PlatformCheckpoint.model_validate_json(serialised)

        assert restored.run_id == ckpt.run_id
        assert restored.cloned_services == ckpt.cloned_services
        assert restored.architecture_json == ckpt.architecture_json
        assert restored.service_qa_states["svc-a"].status == "completed"
        assert restored.service_qa_states["svc-a"].run_id == "abc123"


# ── CheckpointManager ──────────────────────────────────────────────────────────


class TestCheckpointManager:
    def test_load_returns_none_when_no_file(self, tmp_path):
        mgr = _mgr(tmp_path)
        assert mgr.load() is None

    def test_save_creates_file(self, tmp_path):
        mgr = _mgr(tmp_path)
        ckpt = _fresh()
        mgr.save(ckpt)
        assert mgr.path.exists()

    def test_save_and_load_roundtrip(self, tmp_path):
        mgr = _mgr(tmp_path)
        ckpt = _fresh()
        ckpt.cloned_services["svc"] = "/some/path"
        ckpt.architecture_json = '{"services": [], "contracts": []}'

        mgr.save(ckpt)
        loaded = mgr.load()

        assert loaded is not None
        assert loaded.run_id == ckpt.run_id
        assert loaded.cloned_services == {"svc": "/some/path"}
        assert loaded.architecture_json == ckpt.architecture_json

    def test_save_is_atomic_no_partial_file(self, tmp_path):
        """
        After save(), only the final .json file should exist; the .tmp file
        should have been replaced.
        """
        mgr = _mgr(tmp_path)
        mgr.save(_fresh())
        tmp_file = mgr.path.with_suffix(".json.tmp")
        assert not tmp_file.exists()
        assert mgr.path.exists()

    def test_load_returns_none_on_corrupt_file(self, tmp_path):
        mgr = _mgr(tmp_path)
        mgr.path.write_text("not valid json {{{{")
        result = mgr.load()
        assert result is None  # graceful, not an exception

    def test_delete_removes_file(self, tmp_path):
        mgr = _mgr(tmp_path)
        mgr.save(_fresh())
        assert mgr.path.exists()
        mgr.delete()
        assert not mgr.path.exists()

    def test_delete_no_op_when_no_file(self, tmp_path):
        mgr = _mgr(tmp_path)
        mgr.delete()  # should not raise

    def test_save_updates_updated_at(self, tmp_path):
        from datetime import datetime, timezone

        mgr = _mgr(tmp_path)
        ckpt = _fresh()
        original_updated = ckpt.updated_at
        mgr.save(ckpt)
        assert ckpt.updated_at >= original_updated

    def test_custom_checkpoint_dir(self, tmp_path):
        custom_dir = tmp_path / "custom_ckpts"
        mgr = CheckpointManager(
            platform_name="my-platform",
            output_dir=str(tmp_path),
            checkpoint_dir=str(custom_dir),
        )
        mgr.save(_fresh(platform_name="my-platform"))
        assert (custom_dir / "my-platform" / "checkpoint.json").exists()


# ── CheckpointManager mutators ─────────────────────────────────────────────────


class TestCheckpointManagerMutators:
    def test_mark_clone_complete(self, tmp_path):
        mgr = _mgr(tmp_path)
        ckpt = _fresh()
        mgr.mark_clone_complete(ckpt, "auth-service", "/repos/auth")
        assert ckpt.cloned_services["auth-service"] == "/repos/auth"

    def test_mark_architecture_complete(self, tmp_path):
        mgr = _mgr(tmp_path)
        ckpt = _fresh()
        mgr.mark_architecture_complete(ckpt, '{"contracts": []}')
        assert ckpt.architecture_json == '{"contracts": []}'

    def test_mark_service_qa_running(self, tmp_path):
        mgr = _mgr(tmp_path)
        ckpt = _fresh()
        mgr.mark_service_qa_running(ckpt, "payments")
        assert ckpt.service_qa_states["payments"].status == "running"

    def test_mark_service_qa_complete(self, tmp_path):
        mgr = _mgr(tmp_path)
        ckpt = _fresh()
        mgr.mark_service_qa_complete(ckpt, "payments", run_id="r1", output_directory="/out/payments")
        state = ckpt.service_qa_states["payments"]
        assert state.status == "completed"
        assert state.run_id == "r1"
        assert state.output_directory == "/out/payments"

    def test_mark_service_qa_failed(self, tmp_path):
        mgr = _mgr(tmp_path)
        ckpt = _fresh()
        mgr.mark_service_qa_failed(ckpt, "payments", error="strategist crashed")
        assert ckpt.service_qa_states["payments"].status == "failed"
        assert "strategist" in ckpt.service_qa_states["payments"].error

    def test_mark_contract_lifecycle(self, tmp_path):
        mgr = _mgr(tmp_path)
        ckpt = _fresh()

        mgr.mark_contract_running(ckpt, "frontend", "api")
        key = PlatformCheckpoint.contract_key("frontend", "api")
        assert ckpt.contract_states[key].status == "running"

        mgr.mark_contract_complete(ckpt, "frontend", "api", output_directory="/out/contracts")
        assert ckpt.contract_states[key].status == "completed"
        assert ckpt.contract_states[key].output_directory == "/out/contracts"

    def test_mark_contract_failed(self, tmp_path):
        mgr = _mgr(tmp_path)
        ckpt = _fresh()
        mgr.mark_contract_failed(ckpt, "frontend", "api", error="timeout")
        key = PlatformCheckpoint.contract_key("frontend", "api")
        assert ckpt.contract_states[key].status == "failed"
        assert ckpt.contract_states[key].error == "timeout"


# ── Resume skip logic ──────────────────────────────────────────────────────────


class TestResumeSkipLogic:
    """
    Verify the skip conditions that PlatformOrchestrator uses when resuming.
    These test the data layer only — no orchestrator instantiation needed.
    """

    def test_completed_service_is_skipped(self):
        ckpt = _fresh()
        ckpt.service_qa_states["auth"] = ServiceRunState(status="completed")
        state = ckpt.service_qa_states.get("auth")
        assert state is not None and state.status == "completed"

    def test_pending_service_is_not_skipped(self):
        ckpt = _fresh()
        ckpt.service_qa_states["auth"] = ServiceRunState(status="pending")
        state = ckpt.service_qa_states.get("auth")
        assert state is None or state.status != "completed"

    def test_missing_service_state_is_not_skipped(self):
        ckpt = _fresh()
        state = ckpt.service_qa_states.get("new-service")
        assert state is None  # treated as "pending"

    def test_completed_contract_is_skipped(self):
        ckpt = _fresh()
        key = PlatformCheckpoint.contract_key("frontend", "api")
        ckpt.contract_states[key] = ServiceRunState(status="completed")
        state = ckpt.contract_states.get(key)
        assert state is not None and state.status == "completed"

    def test_architecture_present_avoids_strategist_rerun(self):
        ckpt = _fresh()
        ckpt.architecture_json = '{"services": [], "contracts": [], "shared_schemas": [], "topology_notes": ""}'
        # Simulate the resume check in PlatformOrchestrator
        should_run_strategist = not bool(ckpt.architecture_json)
        assert not should_run_strategist

    def test_empty_architecture_triggers_strategist_run(self):
        ckpt = _fresh()
        assert ckpt.architecture_json == ""
        should_run_strategist = not bool(ckpt.architecture_json)
        assert should_run_strategist

    def test_run_id_preserved_across_save_load(self, tmp_path):
        """The run_id from the first run must survive a save/load cycle."""
        mgr = _mgr(tmp_path)
        ckpt = _fresh()
        original_run_id = ckpt.run_id
        mgr.save(ckpt)
        loaded = mgr.load()
        assert loaded is not None
        assert loaded.run_id == original_run_id
