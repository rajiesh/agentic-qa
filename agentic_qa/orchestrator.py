from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import anthropic

from .agents.specialists.api import ApiTestAgent
from .agents.specialists.e2e import E2ETestAgent
from .agents.specialists.functional import FunctionalTestAgent
from .agents.specialists.integration import IntegrationTestAgent
from .agents.specialists.performance import PerformanceTestAgent
from .agents.specialists.security import SecurityTestAgent
from .agents.strategist import StrategistAgent
from .config import QAConfig, RepoTarget
from .core.cost_tracker import BudgetExceededError, CostTracker
from .core.models import QARun, SpecialistResult, TestType
from .core.output_manager import OutputManager
from .core.repo_ingestor import RepoIngestor
from .core.validator import PostGenerationValidator

logger = logging.getLogger(__name__)

_SPECIALIST_MAP: dict[TestType, type[Any]] = {
    "functional": FunctionalTestAgent,
    "performance": PerformanceTestAgent,
    "security": SecurityTestAgent,
    "integration": IntegrationTestAgent,
    "api": ApiTestAgent,
    "e2e": E2ETestAgent,
}


class QAOrchestrator:
    """Top-level coordinator: ingest → strategize → fan-out to specialists."""

    def __init__(self, config: QAConfig) -> None:
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
        self.ingestor = RepoIngestor(config)

    async def run(self, targets: list[RepoTarget]) -> list[QARun]:
        # One cost tracker shared across the entire batch
        cost_tracker = CostTracker(budget_usd=self.config.cost_budget_usd)

        # Bound outer repo-level parallelism to avoid saturating the API
        sem = asyncio.Semaphore(self.config.repo_concurrency_limit)

        async def _bounded(t: RepoTarget) -> QARun:
            async with sem:
                return await self._run_one(t, cost_tracker=cost_tracker)

        results = list(await asyncio.gather(*[_bounded(t) for t in targets]))
        logger.info("Batch complete. %s", cost_tracker)
        return results

    async def _run_one(
        self,
        target: RepoTarget,
        cost_tracker: CostTracker | None = None,
    ) -> QARun:
        qa_run = QARun(repo_url=target.url)
        logger.info("Starting QA run %s for %s", qa_run.run_id, target.url)

        # 1. Clone / prepare repo
        repo_path = await self.ingestor.prepare(target)
        logger.info("Repo ready at %s", repo_path)

        # 2. Strategist analysis
        strategist = StrategistAgent(
            client=self.client,
            config=self.config,
            agent_id=f"strategist-{qa_run.run_id[:8]}",
            cost_tracker=cost_tracker,
        )
        try:
            test_plan = await strategist.run(
                repo_local_path=str(repo_path),
                repo_url=target.url,
                doc_links=target.doc_links,
            )
        except BudgetExceededError:
            raise  # propagate so the outer gather sees it
        except Exception as exc:
            logger.error("Strategist failed: %s", exc)
            qa_run.completed_at = datetime.utcnow()
            qa_run.success = False
            return qa_run

        qa_run.test_plan = test_plan
        logger.info(
            "Test plan ready: %d entries — %s",
            len(test_plan.entries),
            [e.test_type for e in test_plan.entries],
        )

        # 3. Output directory
        repo_name = repo_path.name
        output_manager = OutputManager(
            base_dir=self.config.output_dir,
            repo_name=repo_name,
            run_id=qa_run.run_id,
        )
        qa_run.output_directory = str(output_manager.root)
        await output_manager.write_test_plan(test_plan.model_dump_json(indent=2))

        # 4. Determine which specialists to run
        enabled_entries = [
            entry for entry in test_plan.entries
            if self._is_enabled(entry.test_type)
        ]
        skipped_entries = [
            entry for entry in test_plan.entries
            if not self._is_enabled(entry.test_type)
        ]
        for entry in skipped_entries:
            logger.warning(
                "Skipping '%s' tests — disabled in config. "
                "Pass --no-%s to suppress, or check config.specialists.%s.enabled.",
                entry.test_type, entry.test_type, entry.test_type,
            )
        if not enabled_entries:
            logger.warning("No enabled specialist types matched the test plan entries.")

        # 5. Fan out with bounded concurrency
        semaphore = asyncio.Semaphore(self.config.concurrency_limit)

        async def _run_specialist(entry: Any) -> SpecialistResult | Exception:
            async with semaphore:
                agent_class = _SPECIALIST_MAP.get(entry.test_type)
                if not agent_class:
                    logger.warning("No specialist for test_type=%s", entry.test_type)
                    return Exception(f"No specialist for {entry.test_type}")
                agent = agent_class(
                    client=self.client,
                    config=self.config,
                    agent_id=f"{entry.test_type}-{qa_run.run_id[:8]}",
                    cost_tracker=cost_tracker,
                )
                try:
                    return await agent.run(
                        plan_entry=entry,
                        tech_stack=test_plan.tech_stack,
                        repo_local_path=str(repo_path),
                        output_manager=output_manager,
                    )
                except BudgetExceededError:
                    raise  # bubble up so the gather sees it
                except Exception as exc:
                    logger.error("Specialist %s failed: %s", entry.test_type, exc)
                    return exc

        results = await asyncio.gather(
            *[_run_specialist(e) for e in enabled_entries],
            return_exceptions=True,
        )

        validator = PostGenerationValidator()
        for item in results:
            if isinstance(item, SpecialistResult):
                validator.validate(item, str(output_manager.root), self.config.lint_generated)
                qa_run.specialist_results.append(item)
            else:
                logger.warning("Specialist failed: %s", item)

        errors = [r for r in results if isinstance(r, Exception)]
        qa_run.success = len(errors) == 0 and not any(
            r.errors for r in qa_run.specialist_results
        )
        qa_run.completed_at = datetime.utcnow()

        if errors:
            logger.warning("%d specialist(s) failed: %s", len(errors), errors)

        await output_manager.write_run_summary(qa_run)
        logger.info(
            "QA run %s complete. Output: %s", qa_run.run_id, qa_run.output_directory
        )
        return qa_run

    def _is_enabled(self, test_type: TestType) -> bool:
        cfg = getattr(self.config.specialists, test_type, None)
        return bool(cfg and cfg.enabled)
