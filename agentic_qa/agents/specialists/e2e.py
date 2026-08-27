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
You are an expert Web E2E Test Engineer specializing in Playwright.

Given a test scope describing a web application, generate COMPLETE, RUNNABLE Playwright test
scripts (TypeScript) that cover real user journeys end-to-end in a browser.

What to cover:
1. Critical user flows (sign up, login, checkout, form submission, navigation)
2. Page load and rendering verification
3. Interactive element behaviour (buttons, modals, dropdowns, infinite scroll)
4. Cross-page state (session persistence, cart retention, redirects after auth)
5. Accessibility checks via axe-core where appropriate

Code standards:
- Use Playwright's Page Object Model (POM) — one class per page in a `pages/` subdirectory
- Use `test.describe` blocks to group related scenarios
- Use `expect` locator assertions (not `waitForSelector`)
- Parameterise base URL via `process.env.BASE_URL || 'http://localhost:3000'`
- Include a `playwright.config.ts` if one isn't already present
- Use `data-testid` attributes in selectors when present; fall back to role/label
- Each test must be fully independent (no shared state between tests)

File layout to produce:
  playwright.config.ts          — project config (chromium + webkit, base URL, retries)
  pages/<PageName>.page.ts      — Page Object classes
  tests/e2e/<feature>.spec.ts   — test specs

Workflow:
1. Use read_file to read route files, component files, and any existing e2e tests
2. Use search_code to find page components, router config, and form elements
3. Write files using write_e2e_file — one call per file
4. Call report_complete when done
""".strip()


class E2ETestAgent(BaseAgent):
    AGENT_ROLE = "e2e"

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
                "name": "write_e2e_file",
                "description": (
                    "Write a Playwright E2E test file or config to the output directory. "
                    "Use sub-paths like 'pages/Login.page.ts' or 'tests/e2e/auth.spec.ts'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "Relative path within the e2e output dir, e.g. 'tests/e2e/login.spec.ts'",
                        },
                        "content": {"type": "string", "description": "Full file content"},
                        "description": {"type": "string", "description": "What this file covers"},
                    },
                    "required": ["filename", "content", "description"],
                },
            },
            {
                "name": "report_complete",
                "description": "Signal that all E2E test files have been written.",
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
            "write_e2e_file": self._handle_write_e2e_file,
            "report_complete": self._handle_report_complete,
        }

    async def _handle_write_e2e_file(
        self, filename: str, content: str, description: str
    ) -> str:
        self._generated_files.append(
            GeneratedTestFile(
                filename=filename,
                content=content,
                test_type="e2e",
                framework="playwright",
                description=description,
            )
        )
        assert self._output_manager is not None
        # Preserve sub-paths (e.g. tests/e2e/login.spec.ts, pages/Login.page.ts)
        parts = filename.split("/", 1)
        if len(parts) == 2:
            dest = await self._output_manager.write(f"e2e/{parts[0]}", parts[1], content)
        else:
            dest = await self._output_manager.write("e2e", filename, content)
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
            "Generate Playwright E2E tests for the following web application scope:\n\n"
            f"Test Scope:\n{plan_entry.scope.model_dump_json(indent=2)}\n\n"
            f"Tech Stack:\n{tech_stack.model_dump_json(indent=2)}\n\n"
            f"Rationale: {plan_entry.rationale}\n\n"
            "Read the route config, page components, and any existing tests first. "
            "Then write: playwright.config.ts, Page Object files under pages/, "
            "and spec files under tests/e2e/. Each spec must be fully independent."
        )

        _, usage = await self._run_loop(user_message, max_iterations=20)

        return SpecialistResult(
            test_type="e2e",
            agent_id=self.agent_id,
            started_at=started_at,
            completed_at=datetime.utcnow(),
            generated_files=self._generated_files,
            token_usage=usage,
        )
