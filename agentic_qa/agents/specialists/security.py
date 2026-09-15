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
You are an expert Application Security Test Engineer.

Given a test scope and tech stack, generate security test artifacts that cover:
1. OWASP Top 10 risks relevant to the application
2. Input validation and injection vulnerabilities (SQL, XSS, command injection)
3. Authentication and authorization boundary tests
4. Sensitive data exposure checks
5. Security misconfiguration detection

Output formats:
- Programmatic security probes in the target's own language/test framework (e.g.
  pytest + httpx for Python, Jest + supertest for Node, JUnit + RestAssured for Java) —
  use tech_stack.languages / tech_stack.test_frameworks_existing to pick the right one
- OWASP ZAP automation config YAML for active scanning (language-agnostic, always applicable)
- SAST rule hints as comments / regex patterns

Guidelines:
- Focus on the actual endpoints and data flows identified in the scope
- Include both positive (valid input accepted) and negative (malicious input rejected) tests
- Never generate working exploit payloads — generate detection/probe scripts only
- Include assertions that validate secure behavior (e.g., 400/403 responses to injection attempts)

Workflow:
1. Read authentication, route, and input validation code
2. Write security test files
3. Whenever you add or change test files, also create or update README.md in the same
   output directory (via write_security_test, artifact_type="doc") so it always reflects the
   current file set, exact install steps for whatever language/framework you used, how to
   run OWASP ZAP if you produced a zap_config, and the run command. Do this before calling
   report_complete.
4. Call report_complete
""".strip()


class SecurityTestAgent(BaseAgent):
    AGENT_ROLE = "security"

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
                "name": "write_security_test",
                "description": "Write a security test file (or README.md).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "content": {"type": "string"},
                        "description": {"type": "string"},
                        "artifact_type": {
                            "type": "string",
                            "enum": ["probe_script", "zap_config", "sast_rules", "doc"],
                            "description": (
                                "Category of security artifact. Use \"doc\" for README.md."
                            ),
                        },
                        "framework": {
                            "type": "string",
                            "description": (
                                "For artifact_type=probe_script: the language/framework "
                                "actually used, e.g. pytest+httpx, jest+supertest, "
                                "junit+restassured."
                            ),
                        },
                    },
                    "required": ["filename", "content", "description", "artifact_type"],
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
            "write_security_test": self._handle_write_security_test,
            "report_complete": self._handle_report_complete,
        }

    async def _handle_write_security_test(
        self,
        filename: str,
        content: str,
        description: str,
        artifact_type: str = "probe_script",
        framework: str = "",
    ) -> str:
        if artifact_type == "doc" or filename.rsplit("/", 1)[-1].lower() == "readme.md":
            framework = "markdown"
        elif artifact_type == "probe_script":
            framework = framework or "pytest"
        else:
            framework = artifact_type
        self._generated_files.append(
            GeneratedTestFile(
                filename=filename,
                content=content,
                test_type="security",
                framework=framework,
                description=description,
            )
        )
        assert self._output_manager is not None
        dest = await self._output_manager.write("security", filename, content)
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
            "Generate security tests for the following scope:\n\n"
            f"Test Scope:\n{plan_entry.scope.model_dump_json(indent=2)}\n\n"
            f"Tech Stack:\n{tech_stack.model_dump_json(indent=2)}\n\n"
            f"Rationale: {plan_entry.rationale}\n\n"
            "Read auth/route/validation code first, then write security test artifacts."
        )

        _, usage = await self._run_loop(user_message, max_iterations=15)
        usage = await self._ensure_readme(self._generated_files, "write_security_test", usage)

        return SpecialistResult(
            test_type="security",
            agent_id=self.agent_id,
            started_at=started_at,
            completed_at=datetime.utcnow(),
            generated_files=self._generated_files,
            token_usage=usage,
        )
