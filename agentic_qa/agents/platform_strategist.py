from __future__ import annotations

import json
import logging
from functools import partial
from typing import Any

import anthropic

from ..core.models import PlatformArchitecture, ServiceDescriptor
from ..tools.repo_tools import async_list_directory, async_read_file, async_search_code
from ..tools.web_tools import async_fetch_url
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """
You are an expert Platform Architect specialising in distributed systems and service-mesh analysis.

You will be given access to multiple cloned service repositories. Your job is to:
1. Understand the role and technology of each service.
2. Discover cross-service dependencies by reading source code, config files, API schemas,
   and inter-service communication patterns.
3. Emit a structured PlatformArchitecture JSON via emit_platform_architecture.

Contract discovery checklist — check ALL of the following:
  REST contracts:
    - HTTP clients (httpx, requests, axios, fetch) calling sibling service base URLs.
    - Environment variables like SERVICE_URL, API_BASE_URL, BACKEND_URL, etc.
    - OpenAPI / Swagger spec files (openapi.yaml, swagger.json, *.openapi.*).
  gRPC contracts:
    - .proto files; stub imports (import ..._pb2).
    - Server addresses in config.
  GraphQL contracts:
    - schema.graphql, *.graphql; Apollo Client calls; codegen configs.
  Message/event contracts:
    - Kafka topic names (KAFKA_TOPIC env vars, producer.send(), consumer.subscribe()).
    - RabbitMQ exchange/queue names; Pub/Sub topic IDs; SNS/SQS resource names.
    - Shared message schema files (Avro .avsc, Protobuf for events).
  Database contracts (shared DB):
    - Multiple services sharing the same DB connection string or schema name.
    - Shared migration files referenced from more than one repo.

For EACH discovered contract record:
  - consumer: service that depends / subscribes / calls
  - provider: service that exposes / publishes / owns the DB
  - contract_type: rest | grpc | graphql | message | database
  - endpoints: list of REST paths, gRPC methods, topic names, or table/schema names
  - schema_files: repo-relative paths (e.g. "openapi/spec.yaml") — prefix with "<service>:" when
    the file lives in a specific service's repo
  - description: one sentence summary

Tool usage:
  - read_file(service, path)          — read a file from a named service repo
  - list_directory(service, path, depth) — explore a service's directory tree
  - search_code(service, pattern, file_glob, max_results) — grep within a service repo
  - fetch_url(url)                    — load external documentation / spec URLs
  - emit_platform_architecture(arch_json) — call LAST with the complete JSON

Output schema for emit_platform_architecture:
{
  "services": [
    {
      "name": "string",
      "repo_url": "string",
      "local_path": "string",
      "role": "frontend|backend|api_gateway|worker|infra|docs",
      "doc_links": [],
      "branch": "string",
      "sparse_paths": []
    }
  ],
  "contracts": [
    {
      "consumer": "string",
      "provider": "string",
      "contract_type": "rest|grpc|graphql|message|database",
      "endpoints": ["string"],
      "schema_files": ["string"],
      "description": "string"
    }
  ],
  "shared_schemas": ["string"],
  "topology_notes": "string"
}

Do not emit markdown. Call emit_platform_architecture with a raw JSON string.
""".strip()


class PlatformStrategistAgent(BaseAgent):
    AGENT_ROLE = "platform_strategist"

    def _use_thinking(self) -> bool:
        return True

    def _max_tokens(self) -> int:
        return self.config.max_tokens_strategist

    def _build_system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def _setup_tools(self) -> None:
        self._emitted_arch: PlatformArchitecture | None = None
        self._service_paths: dict[str, str] = {}

        self._tools = [
            {
                "name": "read_file",
                "description": "Read a file from a named service's repository.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string", "description": "Service name"},
                        "path": {"type": "string", "description": "Repo-relative path"},
                        "max_lines": {"type": "integer", "default": 300},
                    },
                    "required": ["service", "path"],
                },
            },
            {
                "name": "list_directory",
                "description": "List directory contents for a named service.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string"},
                        "path": {"type": "string", "default": "."},
                        "depth": {"type": "integer", "default": 2},
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "search_code",
                "description": "Grep for a pattern within a named service's source tree.",
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
                "name": "fetch_url",
                "description": "Fetch a documentation or spec URL.",
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
                "name": "emit_platform_architecture",
                "description": (
                    "Emit the completed cross-service architecture and contract graph. "
                    "Call this LAST after all exploration is complete."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "arch_json": {
                            "type": "string",
                            "description": "JSON string matching PlatformArchitecture schema.",
                        }
                    },
                    "required": ["arch_json"],
                },
            },
        ]

    def _bind(self, service_paths: dict[str, str], services: list[ServiceDescriptor]) -> None:
        self._service_paths = service_paths
        self._services = services

        self._tool_handlers = {
            "read_file": self._handle_read_file,
            "list_directory": self._handle_list_directory,
            "search_code": self._handle_search_code,
            "fetch_url": async_fetch_url,
            "emit_platform_architecture": self._handle_emit_arch,
        }

    # ── Tool handlers ──────────────────────────────────────────────────────────

    def _resolve(self, service: str) -> str | None:
        return self._service_paths.get(service)

    async def _handle_read_file(
        self, service: str, path: str, max_lines: int = 300
    ) -> str:
        root = self._resolve(service)
        if root is None:
            return f"[error] Unknown service '{service}'. Known: {list(self._service_paths)}"
        return await async_read_file(path=path, repo_root=root, max_lines=max_lines)

    async def _handle_list_directory(
        self, service: str, path: str = ".", depth: int = 2
    ) -> str:
        root = self._resolve(service)
        if root is None:
            return f"[error] Unknown service '{service}'. Known: {list(self._service_paths)}"
        return await async_list_directory(path=path, repo_root=root, depth=depth)

    async def _handle_search_code(
        self,
        service: str,
        pattern: str,
        file_glob: str = "**/*",
        max_results: int = 20,
    ) -> str:
        root = self._resolve(service)
        if root is None:
            return f"[error] Unknown service '{service}'. Known: {list(self._service_paths)}"
        return await async_search_code(
            pattern=pattern, repo_root=root, file_glob=file_glob, max_results=max_results
        )

    async def _handle_emit_arch(self, arch_json: str) -> str:
        try:
            data = json.loads(arch_json)
            self._emitted_arch = PlatformArchitecture.model_validate(data)
            logger.info(
                "[%s] architecture validated: %d services, %d contracts",
                self.agent_id,
                len(self._emitted_arch.services),
                len(self._emitted_arch.contracts),
            )
            return "Platform architecture accepted and validated successfully."
        except Exception as exc:
            logger.error("[%s] architecture validation failed: %s", self.agent_id, exc)
            return f"[error] Validation failed: {exc}. Please fix and resubmit."

    # ── Entry point ────────────────────────────────────────────────────────────

    async def run(
        self,
        services: list[ServiceDescriptor],
        service_paths: dict[str, str],
        global_doc_links: list[str],
        platform_name: str,
    ) -> PlatformArchitecture:
        self._bind(service_paths, services)

        service_summary = "\n".join(
            f"  - {s.name} ({s.role}): repo={s.repo_url}, local={service_paths.get(s.name, 'N/A')}"
            for s in services
        )
        doc_section = "\n".join(f"  - {u}" for u in global_doc_links) or "  None."
        known = ", ".join(f"'{s.name}'" for s in services)

        user_message = (
            f"Analyze the platform: {platform_name}\n\n"
            f"Services available (use these names exactly in tool calls):\n{service_summary}\n\n"
            f"Platform documentation links:\n{doc_section}\n\n"
            f"Known service names for tool calls: {known}\n\n"
            "Instructions:\n"
            "1. For each service, call list_directory(service, '.', 2) to see its layout.\n"
            "2. Read key files: requirements.txt / package.json / pyproject.toml, "
            "main app file, route/controller files, any .proto or openapi spec files.\n"
            "3. search_code for HTTP client calls (httpx.get/post, axios, fetch, requests), "
            "environment variables referencing sibling services (URL, HOST, ENDPOINT), "
            "and event/message patterns (kafka, rabbitmq, publish, subscribe).\n"
            "4. After exploring ALL services, call emit_platform_architecture with the "
            "complete JSON describing the architecture and every discovered contract.\n"
            "Be thorough — even 'obvious' REST calls between frontend and backend must be "
            "recorded as contracts."
        )

        await self._run_loop(user_message, max_iterations=40)

        if self._emitted_arch is None:
            raise RuntimeError(
                "PlatformStrategist failed to emit an architecture. Check logs."
            )

        # Back-fill local_path into service descriptors within the architecture
        for svc in self._emitted_arch.services:
            if not svc.local_path and svc.name in service_paths:
                svc.local_path = service_paths[svc.name]

        return self._emitted_arch
