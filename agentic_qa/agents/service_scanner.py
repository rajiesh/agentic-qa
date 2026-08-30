"""
service_scanner.py — ServiceScannerAgent

One instance per service. Explores a single service repo, extracts
contract-signal fields (outbound HTTP URLs, env service refs, event patterns,
proto/openapi files, exposed endpoints), and calls `emit_service_summary`
to return a compact ServiceSummary.

Design goals
------------
- Fast: max 15 iterations (vs. 40 for the old monolithic strategist)
- Narrow: only extracts cross-service contract signals; does NOT attempt to
  produce a full TestPlan
- Parallel-safe: stateless; multiple instances run concurrently under a
  scanner_concurrency_limit semaphore in PlatformOrchestrator
"""
from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any

import anthropic

from ..core.models import ServiceDescriptor, ServiceSummary
from ..tools.repo_tools import async_list_directory, async_read_file, async_search_code
from .base_agent import BaseAgent

if TYPE_CHECKING:
    from ..core.cost_tracker import CostTracker

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """
You are a contract-signal extractor. Your job is to scan ONE service repository
and produce a compact ServiceSummary that describes every cross-service dependency
signal you find, without attempting to write test code or produce a full test plan.

Focus exclusively on:
1. Outbound HTTP/gRPC calls — look for URLs, base-URL env vars, SDK client constructors
   (requests.get, httpx.AsyncClient, axios, fetch, grpc.Channel)
2. Environment variables that reference other services — patterns like *_SERVICE_URL,
   *_HOST, *_ENDPOINT, *_BASE_URL, *_GRPC_ADDR
3. Message/event patterns — Kafka producer.send / consumer.subscribe topics,
   RabbitMQ exchange/queue names, SNS topic ARNs, SQS queue URLs
4. Proto files — any *.proto file paths and the service/package names inside them
5. OpenAPI/Swagger specs — openapi.yaml / swagger.json / openapi.json paths
6. Exposed HTTP endpoints — route definitions (app.get, @router.post, router.add_api_route,
   express.Router, etc.) to help the synthesizer understand what each service provides
7. Shared database hints — table or schema names that could be shared across services
   (especially if accessed from env vars like DATABASE_URL pointing at a shared host)
8. Tech stack — primary language, framework, DB drivers (just a hint list)

Exploration strategy:
1. list_directory("") to understand the top-level layout
2. Read package.json / pyproject.toml / requirements.txt / go.mod to get the stack
3. Read the main entry point (main.py / app.py / index.ts / cmd/main.go)
4. search_code for common outbound-call patterns:
   - "requests.get|httpx|axios|fetch(" — HTTP clients
   - "_URL|_HOST|_ENDPOINT|_BASE_URL" — env var patterns
   - "kafka|rabbitmq|sns|sqs|pubsub" — messaging
   - "grpc|proto" — gRPC
5. Read .proto files or openapi specs if found
6. Read a route/router file to catalogue exposed endpoints
7. Call emit_service_summary with everything you found

Stop after you have enough to fill in the summary fields. Do NOT read every file —
be selective and fast. You have at most 15 iterations.
"""


class ServiceScannerAgent(BaseAgent):
    """
    Scans a single service repo and emits a ServiceSummary via the
    `emit_service_summary` tool.
    """

    AGENT_ROLE = "service-scanner"

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        config: Any,
        service: ServiceDescriptor,
        agent_id: str | None = None,
        cost_tracker: "CostTracker | None" = None,
    ) -> None:
        self._service = service
        self._summary: ServiceSummary | None = None
        super().__init__(client=client, config=config, agent_id=agent_id, cost_tracker=cost_tracker)

    # ── BaseAgent contract ─────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        return _SYSTEM_PROMPT.strip()

    def _max_tokens(self) -> int:
        # Scanner uses a lighter max_tokens — it only reads and calls one tool
        return self.config.max_tokens_strategist

    def _setup_tools(self) -> None:
        repo_root = self._service.local_path or ""

        self._tools = [
            {
                "name": "read_file",
                "description": "Read lines from a file in this service's repository.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Repo-relative path"},
                        "max_lines": {
                            "type": "integer",
                            "default": 300,
                            "description": "Maximum lines to return per call.",
                        },
                        "offset": {
                            "type": "integer",
                            "default": 0,
                            "description": "Start reading from this line (0-indexed). Use when truncated.",
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "list_directory",
                "description": "List files and directories in this service's repo.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": "", "description": "Repo-relative path"},
                    },
                    "required": [],
                },
            },
            {
                "name": "search_code",
                "description": "Grep the service's source for a pattern (regex).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Regex to search for"},
                        "file_pattern": {
                            "type": "string",
                            "default": "*",
                            "description": "Glob to restrict files (e.g. '*.py').",
                        },
                        "max_results": {"type": "integer", "default": 30},
                    },
                    "required": ["pattern"],
                },
            },
            {
                "name": "emit_service_summary",
                "description": (
                    "Submit the completed ServiceSummary for this service. "
                    "Call this ONCE when you have gathered all contract signals."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "role": {"type": "string"},
                        "tech_stack_hint": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "outbound_http_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Literal or template URLs found in the code.",
                        },
                        "env_service_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Env var names like PAYMENT_SERVICE_URL.",
                        },
                        "event_patterns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Kafka topics, RabbitMQ queues, SNS ARNs, etc.",
                        },
                        "proto_files": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "openapi_files": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "shared_db_hints": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "exposed_endpoints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "HTTP routes this service exposes.",
                        },
                        "raw_notes": {
                            "type": "string",
                            "description": "Anything else cross-service that doesn't fit above.",
                        },
                    },
                    "required": ["name", "role"],
                },
            },
        ]

        # Wire handlers
        read_fn = partial(async_read_file, repo_root=repo_root)
        list_fn = partial(async_list_directory, repo_root=repo_root)
        search_fn = partial(async_search_code, repo_root=repo_root)

        self._tool_handlers = {
            "read_file": read_fn,
            "list_directory": list_fn,
            "search_code": search_fn,
            "emit_service_summary": self._handle_emit_summary,
        }

    # ── Tool handler ───────────────────────────────────────────────────────────

    async def _handle_emit_summary(self, **kwargs: Any) -> str:
        kwargs.setdefault("name", self._service.name)
        kwargs.setdefault("role", self._service.role)
        self._summary = ServiceSummary(**kwargs)
        logger.info(
            "[%s] ServiceSummary emitted: %d outbound URLs, %d env refs, %d events",
            self.agent_id,
            len(self._summary.outbound_http_urls),
            len(self._summary.env_service_refs),
            len(self._summary.event_patterns),
        )
        return "ServiceSummary recorded. You may now stop (end_turn)."

    # ── Public entry point ─────────────────────────────────────────────────────

    async def run(  # type: ignore[override]
        self,
        **_kwargs: Any,
    ) -> tuple[ServiceSummary, dict[str, int]]:
        """
        Scan the service and return (ServiceSummary, token_usage).
        Falls back to a minimal summary if the agent loop fails to call
        emit_service_summary within max_iterations.
        """
        svc = self._service
        user_msg = (
            f"Service name: {svc.name}\n"
            f"Role: {svc.role}\n"
            f"Repository path: {svc.local_path or svc.repo_url}\n"
            f"Doc links: {', '.join(svc.doc_links) or 'none'}\n\n"
            "Please scan this service repository and call emit_service_summary "
            "when you have identified all cross-service contract signals."
        )

        max_iters = getattr(self.config, "scanner_max_iterations", 15)
        _text, usage = await self._run_loop(user_msg, max_iterations=max_iters)

        if self._summary is None:
            logger.warning(
                "[%s] scanner finished without calling emit_service_summary — using fallback",
                self.agent_id,
            )
            self._summary = ServiceSummary(name=svc.name, role=svc.role)

        return self._summary, usage
