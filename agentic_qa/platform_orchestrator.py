from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic

from .agents.platform_strategist import PlatformStrategistAgent
from .agents.platform_synthesizer import PlatformSynthesizerAgent
from .agents.service_scanner import ServiceScannerAgent
from .agents.specialists.contract import ContractTestAgent
from .config import QAConfig, RepoTarget
from .core.checkpoint import PlatformCheckpoint
from .core.checkpoint_manager import CheckpointManager
from .core.cost_tracker import BudgetExceededError, CostTracker
from .core.models import (
    ContractTestEntry,
    PlatformArchitecture,
    PlatformRun,
    PlatformTestPlan,
    QARun,
    ServiceContract,
    ServiceDescriptor,
    ServiceSummary,
    SpecialistResult,
)
from .core.output_manager import OutputManager
from .core.repo_ingestor import RepoIngestor
from .orchestrator import QAOrchestrator

logger = logging.getLogger(__name__)


def _choose_pact_framework(service: ServiceDescriptor) -> str:
    """Return the most likely Pact library name for this service based on its role / repo hints."""
    role = service.role
    if role == "frontend":
        return "@pact-foundation/pact"
    # Default to Python for backend services; the ContractTestAgent will inspect the actual stack
    return "pact-python"


def _make_contract_entry(
    contract: ServiceContract,
    services_by_name: dict[str, ServiceDescriptor],
) -> ContractTestEntry:
    consumer_svc = services_by_name.get(contract.consumer, ServiceDescriptor(name=contract.consumer, repo_url=""))
    provider_svc = services_by_name.get(contract.provider, ServiceDescriptor(name=contract.provider, repo_url=""))
    return ContractTestEntry(
        contract=contract,
        consumer_framework=_choose_pact_framework(consumer_svc),
        provider_framework=_choose_pact_framework(provider_svc),
        estimated_files=3 if contract.schema_files else 2,
        rationale=(
            f"{contract.contract_type.upper()} dependency: "
            f"{contract.consumer} → {contract.provider}. "
            f"{contract.description}"
        ),
    )


class PlatformOrchestrator:
    """
    Coordinates a full platform QA run across multiple services.

    Flow:
      1. Clone all service repos (parallel, bounded by semaphore).
      2. Run PlatformStrategistAgent to discover cross-service contracts.
      3. Run per-service StrategistAgent + specialists (reuses QAOrchestrator._run_one).
      4. Run ContractTestAgent for each discovered contract (parallel, bounded).
      5. Persist output and return PlatformRun.

    Each phase saves a checkpoint after completion so a crashed run can be
    resumed by passing resume=True (the default). Pass resume=False to start fresh.
    """

    def __init__(self, config: QAConfig) -> None:
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
        self.ingestor = RepoIngestor(config)
        self._service_orchestrator = QAOrchestrator(config)

    async def run(
        self,
        platform_name: str,
        services: list[ServiceDescriptor],
        global_doc_links: list[str],
        run_per_service: bool = True,
        run_contracts: bool = True,
        resume: bool = True,
    ) -> PlatformRun:

        # ── Checkpoint setup ───────────────────────────────────────────────────
        ckpt_mgr: CheckpointManager | None = None
        checkpoint: PlatformCheckpoint | None = None

        if self.config.enable_checkpointing:
            ckpt_mgr = CheckpointManager(
                platform_name=platform_name,
                output_dir=self.config.output_dir,
                checkpoint_dir=self.config.checkpoint_dir,
            )
            if resume:
                checkpoint = ckpt_mgr.load()
                if checkpoint:
                    reset = checkpoint.reset_crashed_states()
                    if reset:
                        logger.info("Reset %d crashed task(s) to 'pending' for retry.", reset)
                    logger.info(
                        "Resuming platform run '%s' (run_id=%s)",
                        platform_name, checkpoint.run_id,
                    )
            else:
                ckpt_mgr.delete()

        # Reuse run_id from checkpoint so outputs land in the same directory
        if checkpoint:
            platform_run = PlatformRun(
                platform_name=platform_name,
                run_id=checkpoint.run_id,
            )
        else:
            platform_run = PlatformRun(platform_name=platform_name)
            if ckpt_mgr:
                checkpoint = PlatformCheckpoint(
                    platform_name=platform_name,
                    run_id=platform_run.run_id,
                )
                ckpt_mgr.save(checkpoint)

        logger.info(
            "Platform run %s for '%s' (%d services)",
            platform_run.run_id, platform_name, len(services),
        )

        # ── Cost tracker (shared across all agents in this run) ────────────────
        cost_tracker = CostTracker(budget_usd=getattr(self.config, "cost_budget_usd", None))

        # ── 1. Clone all service repos ─────────────────────────────────────────
        clone_sem = asyncio.Semaphore(self.config.concurrency_limit)
        service_paths: dict[str, str] = {}

        # Restore already-cloned paths from checkpoint
        if checkpoint:
            for svc_name, local_path in checkpoint.cloned_services.items():
                if Path(local_path).exists():
                    service_paths[svc_name] = local_path
                    # Re-attach local_path to the ServiceDescriptor
                    for svc in services:
                        if svc.name == svc_name:
                            svc.local_path = local_path
                            break
                    logger.info("Skipping clone for '%s' (already at %s)", svc_name, local_path)

        async def _clone(svc: ServiceDescriptor) -> None:
            if svc.name in service_paths:
                return  # already cloned and verified above
            async with clone_sem:
                target = RepoTarget(
                    url=svc.repo_url,
                    branch=svc.branch,
                    sparse_paths=svc.sparse_paths,
                    doc_links=svc.doc_links,
                )
                path = await self.ingestor.prepare(target)
                service_paths[svc.name] = str(path)
                svc.local_path = str(path)
                logger.info("Cloned '%s' → %s", svc.name, path)
                if ckpt_mgr and checkpoint:
                    ckpt_mgr.mark_clone_complete(checkpoint, svc.name, str(path))
                    ckpt_mgr.save(checkpoint)

        await asyncio.gather(*[_clone(s) for s in services])

        # ── 2. Platform strategy — two-tier scanner / synthesizer ─────────────
        arch: PlatformArchitecture | None = None

        if checkpoint and checkpoint.architecture_json:
            # Resume: deserialize the cached architecture — skip all scanning & synthesis
            try:
                arch = PlatformArchitecture.model_validate_json(checkpoint.architecture_json)
                logger.info(
                    "Restored architecture from checkpoint (%d services, %d contracts)",
                    len(arch.services), len(arch.contracts),
                )
            except Exception as exc:
                logger.warning(
                    "Could not restore architecture from checkpoint: %s — re-running strategy.",
                    exc,
                )
                arch = None

        if arch is None:
            # ── 2a. Parallel per-service scanning ─────────────────────────────
            scanner_sem = asyncio.Semaphore(
                getattr(self.config, "scanner_concurrency_limit", 10)
            )

            async def _scan_one(svc: ServiceDescriptor) -> tuple[str, ServiceSummary, dict]:
                # Resume: reuse cached scan if available
                if checkpoint and svc.name in checkpoint.scan_results:
                    cached = checkpoint.scan_results[svc.name]
                    summary = ServiceSummary.model_validate_json(cached.summary_json)
                    logger.info("Skipping scan for '%s' (cached in checkpoint).", svc.name)
                    return svc.name, summary, cached.token_usage

                async with scanner_sem:
                    scanner = ServiceScannerAgent(
                        client=self.client,
                        config=self.config,
                        service=svc,
                        agent_id=f"scanner-{svc.name[:12]}-{platform_run.run_id[:6]}",
                        cost_tracker=cost_tracker,
                    )
                    summary, usage = await scanner.run()
                    if ckpt_mgr and checkpoint:
                        ckpt_mgr.mark_scan_complete(
                            checkpoint, svc.name, summary.model_dump_json(), usage
                        )
                        ckpt_mgr.save(checkpoint)
                    return svc.name, summary, usage

            scan_results = await asyncio.gather(
                *[_scan_one(s) for s in services],
                return_exceptions=True,
            )

            summaries: list[ServiceSummary] = []
            for item in scan_results:
                if isinstance(item, Exception):
                    logger.error("Scanner failed: %s", item)
                else:
                    _name, summary, _usage = item
                    summaries.append(summary)

            if not summaries:
                logger.error("All service scanners failed — cannot synthesize architecture.")
                platform_run.completed_at = datetime.utcnow()
                platform_run.success = False
                return platform_run

            # ── 2b. Synthesis from compact summaries ───────────────────────────
            synthesizer = PlatformSynthesizerAgent(
                client=self.client,
                config=self.config,
                agent_id=f"synthesizer-{platform_run.run_id[:8]}",
                cost_tracker=cost_tracker,
            )
            try:
                arch = await synthesizer.run(
                    summaries=summaries,
                    platform_name=platform_name,
                )
                if ckpt_mgr and checkpoint:
                    ckpt_mgr.mark_architecture_complete(checkpoint, arch.model_dump_json())
                    ckpt_mgr.save(checkpoint)
            except BudgetExceededError:
                raise  # propagate so callers can log and abort
            except Exception as exc:
                logger.error("PlatformSynthesizerAgent failed: %s", exc)
                platform_run.completed_at = datetime.utcnow()
                platform_run.success = False
                return platform_run

        # ── 3. Build PlatformTestPlan ──────────────────────────────────────────
        services_by_name = {s.name: s for s in services}
        contract_entries = [
            _make_contract_entry(c, services_by_name) for c in arch.contracts
        ]

        platform_plan = PlatformTestPlan(
            platform_name=platform_name,
            architecture=arch,
            contract_entries=contract_entries,
        )
        platform_run.platform_plan = platform_plan
        logger.info("Platform plan: %d contracts to test.", len(contract_entries))

        # ── 4. Output directory ────────────────────────────────────────────────
        platform_out_root = Path(self.config.output_dir) / platform_name / platform_run.run_id
        platform_out_root.mkdir(parents=True, exist_ok=True)
        platform_run.output_directory = str(platform_out_root)

        contract_errors: list[Exception] = []

        # ── 5. Per-service QA runs ─────────────────────────────────────────────
        if run_per_service:
            svc_sem = asyncio.Semaphore(self.config.repo_concurrency_limit)

            async def _run_service(svc: ServiceDescriptor) -> tuple[str, QARun | None]:
                svc_state = checkpoint.service_qa_states.get(svc.name) if checkpoint else None
                if svc_state and svc_state.status == "completed":
                    logger.info("Skipping '%s' per-service QA (already completed).", svc.name)
                    return svc.name, None  # Signal: skipped

                async with svc_sem:
                    # Mark as running so a crash is detectable on next resume
                    if ckpt_mgr and checkpoint:
                        ckpt_mgr.mark_service_qa_running(checkpoint, svc.name)
                        ckpt_mgr.save(checkpoint)

                    target = RepoTarget(
                        url=svc.local_path or svc.repo_url,
                        doc_links=svc.doc_links,
                    )
                    run = await self._service_orchestrator._run_one(
                        target, cost_tracker=cost_tracker
                    )

                    if ckpt_mgr and checkpoint:
                        if run.success:
                            ckpt_mgr.mark_service_qa_complete(
                                checkpoint, svc.name,
                                run_id=run.run_id,
                                output_directory=run.output_directory,
                            )
                        else:
                            ckpt_mgr.mark_service_qa_failed(
                                checkpoint, svc.name, error="specialist(s) failed"
                            )
                        ckpt_mgr.save(checkpoint)

                    return svc.name, run

            svc_results = await asyncio.gather(
                *[_run_service(s) for s in services],
                return_exceptions=True,
            )
            for item in svc_results:
                if isinstance(item, Exception):
                    logger.error("Service QA run failed: %s", item)
                else:
                    svc_name, svc_run = item
                    if svc_run is not None:  # None means skipped (already completed)
                        platform_run.service_runs[svc_name] = svc_run

        # ── 6. Contract tests ──────────────────────────────────────────────────
        if run_contracts and contract_entries and self.config.specialists.contract.enabled:
            contract_out = OutputManager(
                base_dir=str(platform_out_root),
                repo_name="contracts",
                run_id="all",
            )

            contract_sem = asyncio.Semaphore(self.config.concurrency_limit)

            async def _run_contract(entry: ContractTestEntry) -> SpecialistResult | Exception:
                consumer = entry.contract.consumer
                provider = entry.contract.provider
                key = PlatformCheckpoint.contract_key(consumer, provider)

                contract_state = checkpoint.contract_states.get(key) if checkpoint else None
                if contract_state and contract_state.status == "completed":
                    logger.info(
                        "Skipping contract %s→%s (already completed).", consumer, provider
                    )
                    return Exception("skipped")  # excluded from results below

                async with contract_sem:
                    if ckpt_mgr and checkpoint:
                        ckpt_mgr.mark_contract_running(checkpoint, consumer, provider)
                        ckpt_mgr.save(checkpoint)

                    agent = ContractTestAgent(
                        client=self.client,
                        config=self.config,
                        agent_id=(
                            f"contract-{consumer[:6]}-"
                            f"{provider[:6]}-{platform_run.run_id[:6]}"
                        ),
                        cost_tracker=cost_tracker,
                    )
                    try:
                        result = await agent.run(
                            entry=entry,
                            service_paths=service_paths,
                            output_manager=contract_out,
                        )
                        if ckpt_mgr and checkpoint:
                            ckpt_mgr.mark_contract_complete(
                                checkpoint, consumer, provider,
                                output_directory=str(contract_out.root),
                            )
                            ckpt_mgr.save(checkpoint)
                        return result
                    except Exception as exc:
                        logger.error(
                            "ContractTestAgent failed for %s→%s: %s", consumer, provider, exc
                        )
                        if ckpt_mgr and checkpoint:
                            ckpt_mgr.mark_contract_failed(
                                checkpoint, consumer, provider, error=str(exc)
                            )
                            ckpt_mgr.save(checkpoint)
                        return exc

            contract_results = await asyncio.gather(
                *[_run_contract(e) for e in contract_entries],
                return_exceptions=True,
            )
            # Exclude "skipped" sentinels and real exceptions; keep only SpecialistResults
            platform_run.contract_results = [
                r for r in contract_results if isinstance(r, SpecialistResult)
            ]
            contract_errors = [
                r for r in contract_results
                if isinstance(r, Exception) and str(r) != "skipped"
            ]
            if contract_errors:
                logger.warning(
                    "%d contract agent(s) failed: %s", len(contract_errors), contract_errors
                )
        elif run_contracts and not contract_entries:
            logger.info("No contracts discovered — skipping ContractTestAgent.")

        # ── 7. Finalise ────────────────────────────────────────────────────────
        all_service_ok = all(r.success for r in platform_run.service_runs.values())
        platform_run.success = all_service_ok and len(contract_errors) == 0
        platform_run.completed_at = datetime.utcnow()

        cost_summary = cost_tracker.summary()
        logger.info(
            "Platform run %s complete. Estimated cost: $%.4f%s. Output: %s",
            platform_run.run_id,
            cost_summary["estimated_cost_usd"],
            f" (budget $%.2f)" % cost_tracker.budget_usd if cost_tracker.budget_usd else "",
            platform_run.output_directory,
        )

        await _write_platform_summary(platform_run, platform_out_root, cost_summary)
        return platform_run


async def _write_platform_summary(
    run: PlatformRun,
    out_root: Path,
    cost_summary: dict[str, object] | None = None,
) -> None:
    import json

    import aiofiles

    dest = out_root / "platform_run_summary.json"
    data = run.model_dump()
    if cost_summary:
        data["cost_summary"] = cost_summary
    async with aiofiles.open(dest, "w") as f:
        await f.write(json.dumps(data, indent=2, default=str))
