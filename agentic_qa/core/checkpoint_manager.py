"""
checkpoint_manager.py — Load, save, and delete platform run checkpoints.

Writes are atomic: content is written to a .tmp file, then os.replace()
swaps it in. This prevents a partial write from leaving a corrupt checkpoint.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .checkpoint import PlatformCheckpoint

logger = logging.getLogger(__name__)


class CheckpointManager:
    """
    Manages one checkpoint file for a given platform run.

    The checkpoint lives at:
        {base_dir}/{platform_name}/checkpoint.json

    where base_dir is checkpoint_dir (if provided) or output_dir.
    """

    def __init__(
        self,
        platform_name: str,
        output_dir: str,
        checkpoint_dir: str | None = None,
    ) -> None:
        self.platform_name = platform_name
        # Always place under <base>/<platform_name>/ so multiple platforms can
        # share the same checkpoint_dir without colliding.
        base_root = Path(checkpoint_dir) if checkpoint_dir else Path(output_dir)
        base = base_root / platform_name
        base.mkdir(parents=True, exist_ok=True)
        self.path: Path = base / "checkpoint.json"

    # ── Public API ─────────────────────────────────────────────────────────────

    def load(self) -> PlatformCheckpoint | None:
        """
        Load and return the checkpoint. Returns None if the file doesn't exist
        or cannot be parsed (logs a warning in that case so the caller can start fresh).
        """
        if not self.path.exists():
            return None
        try:
            data = self.path.read_text(encoding="utf-8")
            checkpoint = PlatformCheckpoint.model_validate_json(data)
            logger.info(
                "Loaded checkpoint for '%s' (run_id=%s, created=%s)",
                checkpoint.platform_name,
                checkpoint.run_id,
                checkpoint.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
            return checkpoint
        except Exception as exc:
            logger.warning(
                "Checkpoint at %s is unreadable (%s) — starting fresh.", self.path, exc
            )
            return None

    def save(self, checkpoint: PlatformCheckpoint) -> None:
        """Atomically persist the checkpoint to disk."""
        from datetime import datetime

        checkpoint.updated_at = datetime.utcnow()
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")
            os.replace(str(tmp), str(self.path))  # atomic on POSIX and Windows
        except Exception as exc:
            logger.error("Failed to save checkpoint to %s: %s", self.path, exc)
            # Non-fatal — the run continues; worst case is no resume on next crash.

    def delete(self) -> None:
        """Remove the checkpoint file so the next run starts fresh."""
        if self.path.exists():
            self.path.unlink()
            logger.info("Checkpoint deleted: %s", self.path)

    # ── Convenience mutators (call save() after each) ─────────────────────────

    def mark_clone_complete(
        self, checkpoint: PlatformCheckpoint, service_name: str, local_path: str
    ) -> None:
        checkpoint.cloned_services[service_name] = local_path

    def mark_scan_complete(
        self,
        checkpoint: PlatformCheckpoint,
        service_name: str,
        summary_json: str,
        token_usage: dict[str, int] | None = None,
    ) -> None:
        from .checkpoint import ServiceScanResult

        checkpoint.scan_results[service_name] = ServiceScanResult(
            service_name=service_name,
            summary_json=summary_json,
            token_usage=token_usage or {},
        )

    def mark_architecture_complete(
        self, checkpoint: PlatformCheckpoint, arch_json: str
    ) -> None:
        checkpoint.architecture_json = arch_json

    def mark_service_qa_running(
        self, checkpoint: PlatformCheckpoint, service_name: str
    ) -> None:
        from .checkpoint import ServiceRunState

        checkpoint.service_qa_states.setdefault(service_name, ServiceRunState())
        checkpoint.service_qa_states[service_name].status = "running"

    def mark_service_qa_complete(
        self,
        checkpoint: PlatformCheckpoint,
        service_name: str,
        run_id: str = "",
        output_directory: str = "",
    ) -> None:
        from .checkpoint import ServiceRunState

        checkpoint.service_qa_states[service_name] = ServiceRunState(
            status="completed",
            run_id=run_id,
            output_directory=output_directory,
        )

    def mark_service_qa_failed(
        self, checkpoint: PlatformCheckpoint, service_name: str, error: str = ""
    ) -> None:
        from .checkpoint import ServiceRunState

        checkpoint.service_qa_states[service_name] = ServiceRunState(
            status="failed",
            error=error,
        )

    def mark_contract_running(
        self, checkpoint: PlatformCheckpoint, consumer: str, provider: str
    ) -> None:
        from .checkpoint import ServiceRunState

        key = PlatformCheckpoint.contract_key(consumer, provider)
        checkpoint.contract_states.setdefault(key, ServiceRunState())
        checkpoint.contract_states[key].status = "running"

    def mark_contract_complete(
        self, checkpoint: PlatformCheckpoint, consumer: str, provider: str, output_directory: str = ""
    ) -> None:
        from .checkpoint import ServiceRunState

        key = PlatformCheckpoint.contract_key(consumer, provider)
        checkpoint.contract_states[key] = ServiceRunState(
            status="completed",
            output_directory=output_directory,
        )

    def mark_contract_failed(
        self, checkpoint: PlatformCheckpoint, consumer: str, provider: str, error: str = ""
    ) -> None:
        from .checkpoint import ServiceRunState

        key = PlatformCheckpoint.contract_key(consumer, provider)
        checkpoint.contract_states[key] = ServiceRunState(
            status="failed",
            error=error,
        )
