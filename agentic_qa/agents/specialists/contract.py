from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ...core.models import (
    ContractTestEntry,
    GeneratedTestFile,
    SpecialistResult,
)
from ...core.output_manager import OutputManager
from ...tools.repo_tools import async_read_file, async_search_code
from ..base_agent import BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """
You are an expert Contract Test Engineer specialising in consumer-driven contract testing.

You will receive a ServiceContract describing a dependency between two services:
  - consumer: the service that calls / depends on the provider
  - provider: the service that exposes the interface

Your task is to generate THREE categories of test artefacts:

1. CONSUMER CONTRACT TEST
   File: test_<provider>_contract.py  (Python/pact-python v2)
        or  <provider>.contract.spec.ts  (TypeScript/@pact-foundation/pact)
   - Import and use the Pact DSL to define expected interactions.
   - Each interaction specifies: method, path, optional request body/headers,
     and the MINIMUM response fields the consumer actually uses (no more).
   - The test must produce a Pact JSON file the provider can replay.
   - Use pytest fixtures (Python) or Jest beforeAll/afterAll (TS).

2. PROVIDER VERIFICATION TEST
   File: test_<consumer>_provider_verify.py  or  <consumer>.provider.spec.ts
   - Starts (or points at) the provider application.
   - Loads the Pact JSON produced by the consumer test.
   - Replays every interaction and asserts the provider's actual responses match.
   - Include a provider state setup hook for any stateful interactions.

3. SCHEMA VALIDATION TEST  (only when schema_files are provided)
   File: test_<provider>_schema.py  or  <provider>.schema.spec.ts
   - Loads the OpenAPI / proto / Avro schema file.
   - For each endpoint in scope, fires a real request and validates the response
     body against the schema (use jsonschema for JSON, grpc_testing for proto).

Exploration workflow:
1. read_file / search_code on the CONSUMER service:
   - Find all HTTP calls, gRPC stubs, or event subscriptions targeting the provider.
   - Note the exact fields extracted from responses (those are the contract fields).
2. read_file / search_code on the PROVIDER service:
   - Find route definitions, response serialisers, schema files.
   - Confirm what the provider actually returns.
3. Read any schema_files listed in the contract.
4. Write test files using write_contract_file — one call per file.
5. Call report_complete with a summary.

Code standards:
- Python: use pytest, pact-python v2 (from pact import Consumer, Provider).
- TypeScript: use @pact-foundation/pact with Jest.
- Tests must be self-contained and runnable; include all imports and fixtures.
- Add a README.md to the contract output directory explaining how to run the tests.
""".strip()


class ContractTestAgent(BaseAgent):
    AGENT_ROLE = "contract"

    def _build_system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def _setup_tools(self) -> None:
        self._generated_files: list[GeneratedTestFile] = []
        self._output_manager: OutputManager | None = None
        self._entry: ContractTestEntry | None = None
        self._service_paths: dict[str, str] = {}

        self._tools = [
            {
                "name": "read_file",
                "description": (
                    "Read a file from a named service's repository. "
                    "Use the service name exactly as it appears in the contract."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Service name (consumer or provider)",
                        },
                        "path": {
                            "type": "string",
                            "description": "Repo-relative file path",
                        },
                        "max_lines": {"type": "integer", "default": 300},
                    },
                    "required": ["service", "path"],
                },
            },
            {
                "name": "search_code",
                "description": "Search for a pattern in a named service's source code.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string"},
                        "pattern": {"type": "string"},
                        "file_glob": {"type": "string", "default": "**/*"},
                        "max_results": {"type": "integer", "default": 20},
                    },
                    "required": ["service", "pattern"],
                },
            },
            {
                "name": "write_contract_file",
                "description": (
                    "Write a contract test file. "
                    "Use service='consumer' or service='provider' to route to the right subdir."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Which service this file belongs to: consumer or provider",
                        },
                        "filename": {"type": "string"},
                        "content": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["service", "filename", "content", "description"],
                },
            },
            {
                "name": "report_complete",
                "description": "Signal that all contract test files have been written.",
                "input_schema": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            },
        ]

    def _bind(
        self,
        entry: ContractTestEntry,
        service_paths: dict[str, str],
        output_manager: OutputManager,
    ) -> None:
        self._entry = entry
        self._service_paths = service_paths
        self._output_manager = output_manager
        self._generated_files = []

        self._tool_handlers = {
            "read_file": self._handle_read_file,
            "search_code": self._handle_search_code,
            "write_contract_file": self._handle_write_contract_file,
            "report_complete": self._handle_report_complete,
        }

    # ── Tool handlers ─────────────────────────────────────────────────────────

    async def _handle_read_file(
        self, service: str, path: str, max_lines: int = 300
    ) -> str:
        repo_root = self._service_paths.get(service)
        if not repo_root:
            return f"[error] Unknown service '{service}'. Known: {list(self._service_paths)}"
        return await async_read_file(path=path, repo_root=repo_root, max_lines=max_lines)

    async def _handle_search_code(
        self,
        service: str,
        pattern: str,
        file_glob: str = "**/*",
        max_results: int = 20,
    ) -> str:
        repo_root = self._service_paths.get(service)
        if not repo_root:
            return f"[error] Unknown service '{service}'. Known: {list(self._service_paths)}"
        return await async_search_code(
            pattern=pattern, repo_root=repo_root, file_glob=file_glob, max_results=max_results
        )

    async def _handle_write_contract_file(
        self, service: str, filename: str, content: str, description: str
    ) -> str:
        assert self._entry is not None
        assert self._output_manager is not None
        consumer = self._entry.contract.consumer
        provider = self._entry.contract.provider
        # Write to:  contracts/<consumer>-<provider>/<service>/<filename>
        subdir = f"contracts/{consumer}-{provider}/{service}"
        dest = await self._output_manager.write(subdir, filename, content)
        framework = _detect_framework(filename, service, self._entry)
        self._generated_files.append(
            GeneratedTestFile(
                filename=f"{subdir}/{filename}",
                content=content,
                test_type="contract",
                framework=framework,
                description=description,
            )
        )
        return f"Written: {dest}"

    async def _handle_report_complete(self, summary: str) -> str:
        logger.info("[%s] report_complete: %s", self.agent_id, summary)
        return "Acknowledged."

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(
        self,
        entry: ContractTestEntry,
        service_paths: dict[str, str],
        output_manager: OutputManager,
    ) -> SpecialistResult:
        self._bind(entry, service_paths, output_manager)
        started_at = datetime.utcnow()

        contract = entry.contract
        known_services = ", ".join(f"'{s}'" for s in service_paths)

        user_message = (
            f"Generate contract tests for the following service dependency:\n\n"
            f"Contract:\n{contract.model_dump_json(indent=2)}\n\n"
            f"Consumer framework: {entry.consumer_framework}\n"
            f"Provider framework: {entry.provider_framework}\n"
            f"Rationale: {entry.rationale}\n\n"
            f"Available services (use these exact names in tool calls): {known_services}\n\n"
            "Steps:\n"
            "1. Explore the consumer service to find how it calls the provider "
            "(HTTP client calls, env vars for provider URL, response field usage).\n"
            "2. Explore the provider service to find route definitions and response schemas.\n"
            "3. Read any schema_files listed in the contract.\n"
            "4. Write all contract test files using write_contract_file.\n"
            "5. Write a README.md (service='consumer') explaining how to run the tests.\n"
            "6. Call report_complete."
        )

        _, usage = await self._run_loop(user_message, max_iterations=20)

        return SpecialistResult(
            test_type="contract",
            agent_id=self.agent_id,
            started_at=started_at,
            completed_at=datetime.utcnow(),
            generated_files=self._generated_files,
            token_usage=usage,
        )


def _detect_framework(filename: str, service: str, entry: ContractTestEntry) -> str:
    """Infer the framework from the filename extension and service role."""
    if filename.endswith(".py"):
        return entry.consumer_framework if "consumer" in service else entry.provider_framework
    if filename.endswith((".ts", ".js", ".spec.ts", ".spec.js")):
        return entry.consumer_framework if "consumer" in service else entry.provider_framework
    return "pact"
