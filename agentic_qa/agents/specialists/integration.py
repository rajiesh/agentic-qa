from __future__ import annotations

import logging
from datetime import datetime
from functools import partial

from ...core.models import GeneratedTestFile, SpecialistResult, TechStack, TestPlanEntry
from ...core.output_manager import OutputManager
from ...tools.repo_tools import async_read_file, async_search_code
from ..base_agent import BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """
You are an expert Integration Test Engineer.

Generate COMPLETE integration tests that verify multiple components work together correctly:
1. Database integration (ORM, raw queries, migrations)
2. External service integration (APIs, message queues, caches)
3. End-to-end data flows across service boundaries
4. Contract tests for inter-service communication

Use real dependencies where possible (testcontainers, docker-compose fixtures).
For Python: pytest with testcontainers-python or pytest-docker.
For JS: Jest with test containers or mock servers.

Include setup that spins up required infrastructure and teardown that cleans up.
""".strip()


class IntegrationTestAgent(BaseAgent):
    AGENT_ROLE = "integration"

    def _build_system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def _setup_tools(self) -> None:
        self._generated_files: list[GeneratedTestFile] = []
        self._output_manager: OutputManager | None = None

        self._tools = [
            {
                "name": "read_file",
                "description": "Read a source file.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "max_lines": {"type": "integer", "default": 300}},
                    "required": ["path"],
                },
            },
            {
                "name": "search_code",
                "description": "Search for patterns.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "file_glob": {"type": "string", "default": "**/*"},
                        "max_results": {"type": "integer", "default": 20},
                    },
                    "required": ["pattern"],
                },
            },
            {
                "name": "write_test_file",
                "description": "Write an integration test file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "content": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["filename", "content", "description"],
                },
            },
            {
                "name": "report_complete",
                "description": "Signal completion.",
                "input_schema": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            },
        ]

    def _bind(self, repo_local_path: str, output_manager: OutputManager) -> None:
        self._output_manager = output_manager
        self._tool_handlers = {
            "read_file": partial(async_read_file, repo_root=repo_local_path),
            "search_code": partial(async_search_code, repo_root=repo_local_path),
            "write_test_file": self._handle_write_test_file,
            "report_complete": self._handle_report_complete,
        }

    async def _handle_write_test_file(self, filename: str, content: str, description: str) -> str:
        framework = "pytest" if filename.endswith(".py") else "jest"
        self._generated_files.append(
            GeneratedTestFile(
                filename=filename, content=content,
                test_type="integration", framework=framework, description=description,
            )
        )
        assert self._output_manager is not None
        dest = await self._output_manager.write("integration", filename, content)
        return f"Written: {dest}"

    async def _handle_report_complete(self, summary: str) -> str:
        return "Acknowledged."

    async def run(
        self,
        plan_entry: TestPlanEntry,
        tech_stack: TechStack,
        repo_local_path: str,
        output_manager: OutputManager,
    ) -> SpecialistResult:
        self._bind(repo_local_path, output_manager)
        started_at = datetime.utcnow()
        user_message = (
            "Generate integration tests for the following scope:\n\n"
            f"Test Scope:\n{plan_entry.scope.model_dump_json(indent=2)}\n\n"
            f"Tech Stack:\n{tech_stack.model_dump_json(indent=2)}\n\n"
            f"Rationale: {plan_entry.rationale}"
        )
        _, usage = await self._run_loop(user_message, max_iterations=15)
        return SpecialistResult(
            test_type="integration", agent_id=self.agent_id,
            started_at=started_at, completed_at=datetime.utcnow(),
            generated_files=self._generated_files, token_usage=usage,
        )
