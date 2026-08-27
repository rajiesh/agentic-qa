from __future__ import annotations

import json
import logging
from functools import partial
from typing import Any

import anthropic

from ..core.models import TestPlan
from ..tools.repo_tools import async_list_directory, async_read_file, async_search_code
from ..tools.web_tools import async_fetch_url
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """
You are an expert Software Quality Architect. Your job is to analyze a software repository and
produce a structured, actionable Test Plan in JSON format.

You will be given repository metadata and access to tools for exploring the codebase.

Your analysis should identify:
- Tech stack and frameworks in use
- API surface (endpoints, contracts)
- Data models and persistence layer
- Business logic hotspots (high-complexity / high-risk areas)
- External dependencies and integration points

Exploration strategy:
1. Start with list_directory to understand the project structure
2. Read key config files (package.json, pyproject.toml, requirements.txt, Dockerfile, etc.)
3. Read entry point files (main.py, app.py, index.ts, server.js, etc.)
4. Read route/controller files to understand the API surface
5. Read model/schema files to understand data structures
6. Use search_code to find patterns (e.g., "@app.route", "class.*Model", "test_")
7. Look for frontend indicators: React/Vue/Angular/Next.js/Svelte imports, HTML templates,
   router configs (react-router, vue-router, Next.js pages/, app/), or a dedicated frontend
   directory (src/components, src/pages, frontend/, client/, web/)
8. Fetch any documentation URLs provided
9. Once you have sufficient context, call emit_test_plan with the complete JSON

Web E2E test guidance:
- Include an "e2e" entry ONLY when the repository contains a web frontend (browser-rendered UI).
  Indicators: React/Vue/Angular/Next.js/Svelte/Nuxt dependencies, HTML templates served by the
  app, a components/ or pages/ directory, or a bundler config (vite.config, webpack.config).
- For pure API-only backends (no frontend assets), do NOT include an "e2e" entry.
- When including "e2e", set suggested_framework to "playwright" and list the key user-facing
  routes/pages in scope.entry_points (e.g. "/login", "/dashboard", "/checkout").

The test plan JSON must match this schema exactly:
{
  "tech_stack": {
    "languages": ["string"],
    "frameworks": ["string"],
    "databases": ["string"],
    "test_frameworks_existing": ["string"],
    "package_manager": "string or null",
    "container": "string or null",
    "api_style": "string or null"
  },
  "entries": [
    {
      "test_type": "functional|performance|security|integration|api|e2e",
      "priority": "critical|high|medium|low",
      "scope": {
        "description": "string",
        "files_to_examine": ["string"],
        "entry_points": ["string"],
        "data_models": ["string"],
        "dependencies": ["string"]
      },
      "suggested_framework": "pytest|jest|vitest|locust|k6|zap|playwright|httpx",
      "estimated_files": 1,
      "rationale": "string"
    }
  ],
  "overall_risk_areas": ["string"],
  "notes": "string"
}

Do NOT wrap the JSON in markdown code fences. Output raw JSON only in the emit_test_plan call.
""".strip()


class StrategistAgent(BaseAgent):
    AGENT_ROLE = "strategist"

    def _use_thinking(self) -> bool:
        return True

    def _max_tokens(self) -> int:
        return self.config.max_tokens_strategist

    def _build_system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def _setup_tools(self) -> None:
        self._emitted_plan: TestPlan | None = None

        self._tools = [
            {
                "name": "read_file",
                "description": "Read the contents of a file in the repository.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Repo-relative file path"},
                        "max_lines": {"type": "integer", "default": 300},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "list_directory",
                "description": "List files and subdirectories at a path.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": "."},
                        "depth": {"type": "integer", "default": 2},
                    },
                },
            },
            {
                "name": "search_code",
                "description": "Search for a pattern across source files (grep-style).",
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
                "name": "fetch_url",
                "description": "Fetch a documentation URL and return the text content.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 8000},
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "emit_test_plan",
                "description": (
                    "Finalize and emit the structured test plan. "
                    "Call this LAST, after all exploration is complete. "
                    "The plan_json must be a valid JSON string matching the TestPlan schema."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "plan_json": {
                            "type": "string",
                            "description": "JSON string matching the TestPlan schema.",
                        }
                    },
                    "required": ["plan_json"],
                },
            },
        ]

    def _bind_repo(self, repo_local_path: str) -> None:
        """Bind tool handlers to the cloned repo path."""
        self._tool_handlers = {
            "read_file": partial(async_read_file, repo_root=repo_local_path),
            "list_directory": partial(async_list_directory, repo_root=repo_local_path),
            "search_code": partial(async_search_code, repo_root=repo_local_path),
            "fetch_url": async_fetch_url,
            "emit_test_plan": self._handle_emit_test_plan,
        }

    async def _handle_emit_test_plan(self, plan_json: str) -> str:
        try:
            data = json.loads(plan_json)
            self._emitted_plan = TestPlan.model_validate(data)
            logger.info("[%s] test plan validated: %d entries", self.agent_id, len(self._emitted_plan.entries))
            return "Test plan accepted and validated successfully."
        except Exception as exc:
            logger.error("[%s] test plan validation failed: %s", self.agent_id, exc)
            return f"[error] Test plan validation failed: {exc}. Please fix and resubmit."

    async def run(
        self,
        repo_local_path: str,
        repo_url: str,
        doc_links: list[str],
    ) -> TestPlan:
        self._bind_repo(repo_local_path)

        doc_section = "\n".join(f"- {u}" for u in doc_links) if doc_links else "None provided."
        user_message = (
            f"Analyze the repository: {repo_url}\n"
            f"Local path for tool calls: {repo_local_path}\n\n"
            f"Documentation links:\n{doc_section}\n\n"
            "Use the tools to explore the repository thoroughly, then call emit_test_plan "
            "with the complete JSON test plan."
        )

        await self._run_loop(user_message, max_iterations=30)

        if self._emitted_plan is None:
            raise RuntimeError(
                "Strategist agent failed to emit a test plan. Check logs for details."
            )

        self._emitted_plan.repo_url = repo_url
        return self._emitted_plan
