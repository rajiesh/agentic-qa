from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic

from .agents.platform_strategist import PlatformStrategistAgent
from .agents.specialists.contract import ContractTestAgent
from .agents.strategist import StrategistAgent
from .config import QAConfig, RepoTarget
from .core.models import (
    ContractTestEntry,
    PlatformArchitecture,
    PlatformRun,
    PlatformTestPlan,
    QARun,
    ServiceContract,
    ServiceDescriptor,
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
    ) -> PlatformRun:
        platform_run = PlatformRun(platform_name=platform_name)
        logger.info(
            "Starting platform run %s for '%s' (%d services)",
            platform_run.run_id,
            platform_name,
            len(services),
        )

        # ── 1. Clone all service repos ─────────────────────────────────────────
        clone_sem = asyncio.Semaphore(self.config.concurrency_limit)
        service_paths: dict[str, str] = {}

        async def _clone(svc: ServiceDescriptor) -> None:
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

        await asyncio.gather(*[_clone(s) for s in services])

        # ── 2. Platform strategy (cross-service contract discovery) ────────────
        strategist = PlatformStrategistAgent(
            client=self.client,
            config=self.config,
            agent_id=f"platform-strategist-{platform_run.run_id[:8]}",
        )
        try:
            arch = await strategist.run(
                services=services,
                service_paths=service_paths,
                global_doc_links=global_doc_links,
                platform_name=platform_name,
            )
        except Exception as exc:
            logger.error("PlatformStrategist failed: %s", exc)
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

        logger.info(
            "Platform plan: %d contracts to test.",
            len(contract_entries),
        )

        # ── 4. Per-service QA runs ─────────────────────────────────────────────
        platform_out_root = Path(self.config.output_dir) / platform_name / platform_run.run_id
        platform_out_root.mkdir(parents=True, exist_ok=True)
        platform_run.output_directory = str(platform_out_root)

        contract_errors: list[Exception] = []

        if run_per_service:
            svc_sem = asyncio.Semaphore(self.config.concurrency_limit)

            async def _run_service(svc: ServiceDescriptor) -> tuple[str, QARun]:
                async with svc_sem:
                    # Use local_path (already cloned) to avoid a redundant clone
                    target = RepoTarget(
                        url=svc.local_path or svc.repo_url,
                        doc_links=svc.doc_links,
                    )
                    run = await self._service_orchestrator._run_one(target)
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
                    platform_run.service_runs[svc_name] = svc_run

        # ── 5. Contract tests ──────────────────────────────────────────────────
        if run_contracts and contract_entries and self.config.specialists.contract.enabled:
            contract_out = OutputManager(
                base_dir=str(platform_out_root),
                repo_name="contracts",
                run_id="all",
            )

            contract_sem = asyncio.Semaphore(self.config.concurrency_limit)

            async def _run_contract(entry: ContractTestEntry) -> SpecialistResult | Exception:
                async with contract_sem:
                    agent = ContractTestAgent(
                        client=self.client,
                        config=self.config,
                        agent_id=(
                            f"contract-{entry.contract.consumer[:6]}-"
                            f"{entry.contract.provider[:6]}-{platform_run.run_id[:6]}"
                        ),
                    )
                    try:
                        return await agent.run(
                            entry=entry,
                            service_paths=service_paths,
                            output_manager=contract_out,
                        )
                    except Exception as exc:
                        logger.error(
                            "ContractTestAgent failed for %s→%s: %s",
                            entry.contract.consumer,
                            entry.contract.provider,
                            exc,
                        )
                        return exc

            contract_results = await asyncio.gather(
                *[_run_contract(e) for e in contract_entries],
                return_exceptions=True,
            )
            platform_run.contract_results = [
                r for r in contract_results if isinstance(r, SpecialistResult)
            ]
            contract_errors = [r for r in contract_results if isinstance(r, Exception)]
            if contract_errors:
                logger.warning(
                    "%d contract agent(s) failed: %s", len(contract_errors), contract_errors
                )
        elif run_contracts and not contract_entries:
            logger.info("No contracts discovered — skipping ContractTestAgent.")

        # ── 6. Finalise ────────────────────────────────────────────────────────
        all_service_ok = all(
            r.success for r in platform_run.service_runs.values()
        )
        platform_run.success = all_service_ok and len(contract_errors) == 0
        platform_run.completed_at = datetime.utcnow()

        await _write_platform_summary(platform_run, platform_out_root)
        logger.info(
            "Platform run %s complete. Output: %s",
            platform_run.run_id,
            platform_run.output_directory,
        )
        return platform_run


async def _write_platform_summary(run: PlatformRun, out_root: Path) -> None:
    import aiofiles

    dest = out_root / "platform_run_summary.json"
    async with aiofiles.open(dest, "w") as f:
        await f.write(run.model_dump_json(indent=2))
