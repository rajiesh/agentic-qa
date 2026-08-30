"""
platform_synthesizer.py — PlatformSynthesizerAgent

Receives compact ServiceSummary objects from all service scanners and reasons
about cross-service contracts without accessing any files. Emits a validated
PlatformArchitecture by calling `emit_platform_architecture`.

Context budget
--------------
50 services × ~600 tokens/summary ≈ 30K tokens  → well within 200K limit.
The agent loop is capped at synthesizer_max_iterations (default 8) because
there is nothing to explore — reasoning only.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import anthropic

from ..core.models import PlatformArchitecture, ServiceContract, ServiceDescriptor, ServiceSummary
from .base_agent import BaseAgent

if TYPE_CHECKING:
    from ..core.cost_tracker import CostTracker

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """
You are a platform contract synthesizer. You will receive compact summaries
of every service in a multi-service platform — each summary contains outbound
HTTP URLs, environment-variable references to other services, event topics,
proto files, OpenAPI specs, and exposed endpoints.

Your job is to reason across all these summaries and discover every
cross-service dependency (REST, gRPC, GraphQL, messaging, or shared DB).

For EACH dependency you discover:
- Identify the CONSUMER (the service that calls / depends on the other)
- Identify the PROVIDER (the service that exposes the interface)
- Determine the contract_type: "rest" | "grpc" | "graphql" | "message" | "database"
- List the specific endpoints / topics / methods / tables involved
- List any schema files (.proto, openapi.yaml, etc.) that describe the interface

Signal cross-referencing rules:
1. REST: env var `PAYMENT_SERVICE_URL` in service A + exposed endpoint `/v1/charge`
   in service B whose name matches "payment*" → REST contract A→B.
2. gRPC: a service with a .proto import + another service that owns that proto
   → gRPC contract.
3. Message: producer topic in service A + consumer subscription to same topic in
   service B → message contract A→B (or A↔B if bidirectional).
4. Database: two services referencing the same DATABASE_URL host/db, or using
   identical table names → database contract.
5. GraphQL: a service with graphql schema + another service fetching via GraphQL
   client → graphql contract.

Do NOT include a dependency if you cannot find a matching signal on both sides.
Be conservative: a false negative is better than a false positive.

Call emit_platform_architecture once with the complete list of discovered contracts.
"""


def _summaries_to_message(summaries: list[ServiceSummary]) -> str:
    """Render all summaries into a single structured user message."""
    lines = ["Here are the service summaries:\n"]
    for s in summaries:
        lines.append(f"## Service: {s.name} (role={s.role})")
        if s.tech_stack_hint:
            lines.append(f"  Tech: {', '.join(s.tech_stack_hint)}")
        if s.exposed_endpoints:
            lines.append(f"  Exposes: {', '.join(s.exposed_endpoints[:20])}")
        if s.outbound_http_urls:
            lines.append(f"  Outbound URLs: {', '.join(s.outbound_http_urls[:15])}")
        if s.env_service_refs:
            lines.append(f"  Env service refs: {', '.join(s.env_service_refs)}")
        if s.event_patterns:
            lines.append(f"  Events: {', '.join(s.event_patterns)}")
        if s.proto_files:
            lines.append(f"  Proto files: {', '.join(s.proto_files)}")
        if s.openapi_files:
            lines.append(f"  OpenAPI files: {', '.join(s.openapi_files)}")
        if s.shared_db_hints:
            lines.append(f"  Shared DB hints: {', '.join(s.shared_db_hints)}")
        if s.raw_notes:
            lines.append(f"  Notes: {s.raw_notes}")
        lines.append("")

    lines.append(
        "Please analyse these summaries, discover all cross-service contracts, "
        "and call emit_platform_architecture with the results."
    )
    return "\n".join(lines)


class PlatformSynthesizerAgent(BaseAgent):
    """
    Synthesizes cross-service contracts from pre-computed ServiceSummary objects.
    Has NO file-access tools — it only reasons about the provided summaries.
    """

    AGENT_ROLE = "platform-synthesizer"

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        config: Any,
        agent_id: str | None = None,
        cost_tracker: "CostTracker | None" = None,
    ) -> None:
        self._architecture: PlatformArchitecture | None = None
        super().__init__(client=client, config=config, agent_id=agent_id, cost_tracker=cost_tracker)

    # ── BaseAgent contract ─────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        return _SYSTEM_PROMPT.strip()

    def _max_tokens(self) -> int:
        return self.config.max_tokens_strategist

    def _use_thinking(self) -> bool:
        return True  # extended thinking helps with cross-service correlation

    def _setup_tools(self) -> None:
        self._tools = [
            {
                "name": "emit_platform_architecture",
                "description": (
                    "Submit the discovered platform architecture and all cross-service contracts. "
                    "Call this ONCE when your analysis is complete."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "services": {
                            "type": "array",
                            "description": "List of service descriptors (pass through from the summaries).",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "repo_url": {"type": "string", "default": ""},
                                    "role": {"type": "string", "default": "backend"},
                                },
                                "required": ["name"],
                            },
                        },
                        "contracts": {
                            "type": "array",
                            "description": "All discovered cross-service contracts.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "consumer": {"type": "string"},
                                    "provider": {"type": "string"},
                                    "contract_type": {
                                        "type": "string",
                                        "enum": ["rest", "grpc", "graphql", "message", "database"],
                                    },
                                    "endpoints": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "schema_files": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "description": {"type": "string", "default": ""},
                                },
                                "required": ["consumer", "provider", "contract_type"],
                            },
                        },
                        "shared_schemas": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Schema file paths referenced by more than one service.",
                        },
                        "topology_notes": {
                            "type": "string",
                            "description": "Free-text summary of the deployment topology.",
                        },
                    },
                    "required": ["services", "contracts"],
                },
            }
        ]
        self._tool_handlers = {
            "emit_platform_architecture": self._handle_emit_architecture,
        }

    # ── Tool handler ───────────────────────────────────────────────────────────

    async def _handle_emit_architecture(
        self,
        services: list[dict[str, Any]],
        contracts: list[dict[str, Any]],
        shared_schemas: list[str] | None = None,
        topology_notes: str = "",
    ) -> str:
        svc_models = [
            ServiceDescriptor(
                name=s["name"],
                repo_url=s.get("repo_url", ""),
                role=s.get("role", "backend"),
            )
            for s in services
        ]
        contract_models = [ServiceContract(**c) for c in contracts]
        self._architecture = PlatformArchitecture(
            services=svc_models,
            contracts=contract_models,
            shared_schemas=shared_schemas or [],
            topology_notes=topology_notes,
        )
        logger.info(
            "[%s] PlatformArchitecture emitted: %d services, %d contracts",
            self.agent_id,
            len(svc_models),
            len(contract_models),
        )
        return "PlatformArchitecture recorded. You may now stop (end_turn)."

    # ── Public entry point ─────────────────────────────────────────────────────

    async def run(  # type: ignore[override]
        self,
        summaries: list[ServiceSummary],
        platform_name: str = "",
        **_kwargs: Any,
    ) -> PlatformArchitecture:
        """
        Synthesize a PlatformArchitecture from the provided service summaries.

        Parameters
        ----------
        summaries:
            One ServiceSummary per service (output of ServiceScannerAgent.run).
        platform_name:
            Optional label used only for logging.

        Returns
        -------
        PlatformArchitecture
        """
        logger.info(
            "[%s] Synthesizing architecture for '%s' from %d service summaries",
            self.agent_id,
            platform_name or "platform",
            len(summaries),
        )

        user_msg = _summaries_to_message(summaries)
        max_iters = getattr(self.config, "synthesizer_max_iterations", 8)
        _text, usage = await self._run_loop(user_msg, max_iterations=max_iters)

        if self._architecture is None:
            logger.warning(
                "[%s] synthesizer did not call emit_platform_architecture — returning empty architecture",
                self.agent_id,
            )
            self._architecture = PlatformArchitecture(
                services=[
                    ServiceDescriptor(name=s.name, repo_url="", role=s.role)
                    for s in summaries
                ],
                contracts=[],
            )

        logger.info("[%s] synthesis complete. tokens=%s", self.agent_id, usage)
        return self._architecture
