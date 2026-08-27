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
You are an expert Performance Test Engineer.

Given a test scope and tech stack, generate COMPLETE, RUNNABLE performance test scripts that:
1. Simulate realistic user load patterns (ramp-up, steady state, ramp-down)
2. Test critical API endpoints and workflows identified in the scope
3. Include assertions for response time and error rate thresholds
4. Use Locust (Python) for Python-based services, k6 (JS) for other services

Locust guidelines:
- Define realistic User classes with task weights
- Set think time between requests
- Include response time assertions via catch_response context manager

k6 guidelines:
- Define stages for load shaping
- Use thresholds for p95 response time and error rate
- Group related requests with tags

Workflow:
1. Read entry point / route files to understand the API surface
2. Write performance test files
3. Call report_complete
""".strip()


class PerformanceTestAgent(BaseAgent):
    AGENT_ROLE = "performance"

    def _build_system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def _setup_tools(self) -> None:
        self._generated_files: list[GeneratedTestFile] = []
        self._output_manager: OutputManager | None = None

        self._tools = [
            {
                "name": "read_file",
                "description": "Read a source file from the repository.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_lines": {"type": "integer", "default": 300},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "search_code",
                "description": "Search for patterns in the repository.",
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
                "description": "Write a performance test file (locustfile.py or k6_script.js).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "content": {"type": "string"},
                        "description": {"type": "string"},
                        "framework": {
                            "type": "string",
                            "enum": ["locust", "k6"],
                            "description": "The performance testing framework used",
                        },
                    },
                    "required": ["filename", "content", "description", "framework"],
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

    async def _handle_write_test_file(
        self, filename: str, content: str, description: str, framework: str = "locust"
    ) -> str:
        self._generated_files.append(
            GeneratedTestFile(
                filename=filename,
                content=content,
                test_type="performance",
                framework=framework,
                description=description,
            )
        )
        assert self._output_manager is not None
        dest = await self._output_manager.write("performance", filename, content)
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
            "Generate performance tests for the following scope:\n\n"
            f"Test Scope:\n{plan_entry.scope.model_dump_json(indent=2)}\n\n"
            f"Tech Stack:\n{tech_stack.model_dump_json(indent=2)}\n\n"
            f"Rationale: {plan_entry.rationale}\n\n"
            "Read the relevant route/API files first, then write performance test scripts."
        )

        _, usage = await self._run_loop(user_message, max_iterations=15)

        return SpecialistResult(
            test_type="performance",
            agent_id=self.agent_id,
            started_at=started_at,
            completed_at=datetime.utcnow(),
            generated_files=self._generated_files,
            token_usage=usage,
        )
