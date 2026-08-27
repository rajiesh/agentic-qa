from __future__ import annotations

import logging
from datetime import datetime
from functools import partial
from typing import Any

from ...core.models import GeneratedTestFile, SpecialistResult, TechStack, TestPlanEntry
from ...core.output_manager import OutputManager
from ...tools.repo_tools import async_read_file, async_search_code
from ..base_agent import BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """
You are an expert Software Test Engineer specializing in functional testing.

Given a test scope and tech stack, your task is to generate COMPLETE, EXECUTABLE test code that:
1. Covers all major happy paths and critical edge cases
2. Uses the appropriate framework (pytest for Python, Jest/Vitest for JS/TS)
3. Includes fixtures, mocks, and setup/teardown where appropriate
4. Is immediately runnable without modification

Guidelines:
- For Python backends: use pytest, pytest-asyncio for async, httpx for API tests
- For JavaScript/TypeScript: use Jest or Vitest with appropriate utilities
- Include import statements, fixtures, and conftest.py entries as needed
- Test one concern per test function
- Use descriptive test names that explain what is being tested

Workflow:
1. Use read_file and search_code to understand the code under test
2. Generate test files using write_test_file
3. Call report_complete when all files are written
""".strip()


class FunctionalTestAgent(BaseAgent):
    AGENT_ROLE = "functional"

    def _build_system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def _setup_tools(self) -> None:
        self._generated_files: list[GeneratedTestFile] = []
        self._output_manager: OutputManager | None = None
        self._complete_summary: str = ""

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
                "description": "Write a test file to the output directory.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string", "description": "File name (e.g. test_auth.py)"},
                        "content": {"type": "string", "description": "Full file content"},
                        "description": {"type": "string", "description": "What this test file covers"},
                    },
                    "required": ["filename", "content", "description"],
                },
            },
            {
                "name": "report_complete",
                "description": "Signal that all test files have been written.",
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
        self, filename: str, content: str, description: str
    ) -> str:
        framework = "pytest" if filename.endswith(".py") else "jest"
        self._generated_files.append(
            GeneratedTestFile(
                filename=filename,
                content=content,
                test_type="functional",
                framework=framework,
                description=description,
            )
        )
        assert self._output_manager is not None
        dest = await self._output_manager.write("functional", filename, content)
        return f"Written: {dest}"

    async def _handle_report_complete(self, summary: str) -> str:
        self._complete_summary = summary
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
            "Generate functional tests for the following scope:\n\n"
            f"Test Scope:\n{plan_entry.scope.model_dump_json(indent=2)}\n\n"
            f"Tech Stack:\n{tech_stack.model_dump_json(indent=2)}\n\n"
            f"Rationale: {plan_entry.rationale}\n\n"
            "Explore relevant files first, then write complete, runnable test files."
        )

        _, usage = await self._run_loop(user_message, max_iterations=20)

        return SpecialistResult(
            test_type="functional",
            agent_id=self.agent_id,
            started_at=started_at,
            completed_at=datetime.utcnow(),
            generated_files=self._generated_files,
            token_usage=usage,
        )
