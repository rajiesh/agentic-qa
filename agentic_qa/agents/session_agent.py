"""
session_agent.py — Claude-powered multi-turn intent router for the interactive session.

Maintains the full conversation history and dispatches tool calls to the real orchestrators.
Unlike BaseAgent (single-turn), this agent keeps a growing messages list across turns so
Claude has complete context of everything said and done in the session.

Tools exposed to Claude:
  add_repos, add_docs, configure,
  run_plan, run_analyze,
  run_platform_plan, run_platform_analyze,
  show_state, exit_session
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import anthropic

from ..config import QAConfig, RepoTarget, SpecialistConfig, TestTypeConfig
from ..core.models import QARun
from ..orchestrator import QAOrchestrator
from ..platform_orchestrator import PlatformOrchestrator

if TYPE_CHECKING:
    from rich.console import Console

    from ..session import SessionState

logger = logging.getLogger(__name__)

# ── Tool schema ───────────────────────────────────────────────────────────────

SESSION_TOOLS: list[dict[str, Any]] = [
    {
        "name": "add_repos",
        "description": (
            "Add repository URLs or local paths to the current session. "
            "Call this whenever the user mentions a GitHub/GitLab URL or a local directory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repository URLs (GitHub, GitLab, Bitbucket) or local paths",
                }
            },
            "required": ["urls"],
        },
    },
    {
        "name": "add_docs",
        "description": (
            "Add documentation URLs to the session. These are passed to all agents "
            "as additional context (API docs, wikis, architecture docs)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Documentation URLs",
                }
            },
            "required": ["urls"],
        },
    },
    {
        "name": "configure",
        "description": (
            "Enable or disable a specific test type for this session. "
            "Use this before running plan or analyze when the user says 'skip X' or 'disable Y'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "test_type": {
                    "type": "string",
                    "enum": [
                        "functional",
                        "performance",
                        "security",
                        "e2e",
                        "integration",
                        "api",
                        "contract",
                    ],
                    "description": "The test type to configure",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "true to enable, false to disable",
                },
            },
            "required": ["test_type", "enabled"],
        },
    },
    {
        "name": "run_plan",
        "description": (
            "Run the strategist agent to generate a test plan WITHOUT generating code. "
            "Use when the user wants to preview what tests would be created. "
            "Returns a structured summary of the plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repos to plan. Omit to use all repos added to the session.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "run_analyze",
        "description": (
            "Run the full QA analysis: strategist + all enabled specialist agents generate test code. "
            "Use when the user says 'analyze', 'generate tests', 'go ahead', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repos to analyze. Omit to use all repos in the session.",
                },
                "skip_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Test types to skip for this run only "
                        "(e.g. ['security', 'e2e']). Stacks on top of session configure() settings."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "run_platform_plan",
        "description": (
            "Discover platform architecture and contracts from a platform.yaml file "
            "WITHOUT generating test code. Use for multi-service / platform analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "platform_yaml": {
                    "type": "string",
                    "description": "Path to the platform.yaml descriptor file",
                }
            },
            "required": ["platform_yaml"],
        },
    },
    {
        "name": "run_platform_analyze",
        "description": (
            "Run full platform QA: per-service test generation + contract tests "
            "from a platform.yaml descriptor. Use for multi-service platforms."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "platform_yaml": {
                    "type": "string",
                    "description": "Path to the platform.yaml descriptor file",
                },
                "skip_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Test types to skip",
                },
            },
            "required": ["platform_yaml"],
        },
    },
    {
        "name": "show_state",
        "description": (
            "Return the current session state: repos, docs, config overrides, "
            "and a summary of the last run's results."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "exit_session",
        "description": "End the interactive session and exit the program.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

SESSION_SYSTEM_PROMPT = """\
You are agentic-qa, an AI-powered QA test generation assistant built on Claude.
Your role: help users generate comprehensive test suites for their software repositories.

You have tools to:
- Register repos and documentation links for the current session
- Configure which test types to include or skip (functional, performance, security, e2e, ...)
- Run a "plan" (strategist only — shows what tests would be created, no code generated)
- Run a full "analyze" (generates actual test code via specialist agents)
- Work with multi-service platform.yaml descriptors

Guidelines:
- When the user mentions a repository URL or path, call add_repos immediately.
- When the user says "plan", "preview", "show me what", call run_plan.
- When the user says "analyze", "generate", "go ahead", "run it", call run_analyze.
- When the user says "skip security" or "no e2e", call configure before running.
- After each tool call, briefly summarize the result and offer clear next steps.
- Be concise. The user sees live orchestrator output; don't repeat it verbatim.
- Only report facts from tool results. Never fabricate test counts or file names.
- If no repos are in the session yet, ask the user to add one before running.
"""

_MAX_ITERS = 30  # max tool-use iterations per turn


class SessionAgent:
    """
    Multi-turn Claude agent that powers the interactive session.

    Maintains conversation_history in SessionState so Claude has full context
    across all turns. Each call to handle_turn() appends the user message,
    runs the agentic tool-use loop, appends the final assistant reply, and
    returns the display text.
    """

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        base_config: QAConfig,
        state: "SessionState",
        console: "Console",
    ) -> None:
        self.client = client
        self.base_config = base_config
        self.state = state
        self.console = console
        self._should_exit = False

    @property
    def should_exit(self) -> bool:
        return self._should_exit

    # ── Public API ─────────────────────────────────────────────────────────────

    async def handle_turn(self, user_input: str) -> str:
        """Process one user turn through the agentic loop. Returns final text reply."""
        self.state.conversation_history.append({"role": "user", "content": user_input})
        reply = await self._agentic_loop()
        # The loop already appended the final assistant message in history.
        return reply

    # ── Agentic loop ───────────────────────────────────────────────────────────

    async def _agentic_loop(self) -> str:
        for _ in range(_MAX_ITERS):
            response = await self.client.messages.create(
                model=self.base_config.model,
                max_tokens=4096,
                system=[
                    {
                        "type": "text",
                        "text": SESSION_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=SESSION_TOOLS,  # type: ignore[arg-type]
                messages=self.state.conversation_history,
            )

            # Always append assistant content to history
            self.state.conversation_history.append(
                {"role": "assistant", "content": response.content}
            )

            if response.stop_reason == "end_turn":
                text_parts = [
                    block.text
                    for block in response.content
                    if hasattr(block, "text") and block.text
                ]
                return "\n".join(text_parts) if text_parts else "(done)"

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.debug("Session tool call: %s %s", block.name, block.input)
                        result_text = await self._dispatch_tool(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_text,
                            }
                        )

                self.state.conversation_history.append(
                    {"role": "user", "content": tool_results}
                )

                if self._should_exit:
                    return "Goodbye! Session ended."
                continue  # let Claude respond to the tool results

            # Unexpected stop reason (max_tokens, etc.)
            logger.warning("Unexpected stop_reason: %s", response.stop_reason)
            break

        return "(Session reached iteration limit — try rephrasing your request.)"

    async def _dispatch_tool(self, name: str, inputs: dict[str, Any]) -> str:
        dispatch: dict[str, Any] = {
            "add_repos": lambda: self._tool_add_repos(inputs.get("urls", [])),
            "add_docs": lambda: self._tool_add_docs(inputs.get("urls", [])),
            "configure": lambda: self._tool_configure(
                inputs["test_type"], inputs["enabled"]
            ),
            "run_plan": lambda: self._tool_run_plan(inputs.get("repos")),
            "run_analyze": lambda: self._tool_run_analyze(
                inputs.get("repos"), inputs.get("skip_types", [])
            ),
            "run_platform_plan": lambda: self._tool_run_platform_plan(
                inputs["platform_yaml"]
            ),
            "run_platform_analyze": lambda: self._tool_run_platform_analyze(
                inputs["platform_yaml"], inputs.get("skip_types", [])
            ),
            "show_state": lambda: self._tool_show_state(),
            "exit_session": lambda: self._tool_exit_session(),
        }
        handler = dispatch.get(name)
        if handler is None:
            return f"[error] Unknown tool: {name}"
        try:
            return await handler()
        except Exception as exc:
            logger.error("Tool %s failed: %s", name, exc, exc_info=True)
            return f"[error] Tool {name} failed: {exc}"

    # ── Tool handlers ──────────────────────────────────────────────────────────

    async def _tool_add_repos(self, urls: list[str]) -> str:
        added = []
        for url in urls:
            if url not in self.state.repos:
                self.state.repos.append(url)
                added.append(url)
        if added:
            names = [u.rstrip("/").split("/")[-1] for u in added]
            return f"Added {len(added)} repo(s): {', '.join(names)}"
        return "All specified repos were already in the session."

    async def _tool_add_docs(self, urls: list[str]) -> str:
        added = []
        for url in urls:
            if url not in self.state.doc_links:
                self.state.doc_links.append(url)
                added.append(url)
        if added:
            return f"Added {len(added)} doc link(s)."
        return "All specified doc links were already in the session."

    async def _tool_configure(self, test_type: str, enabled: bool) -> str:
        self.state.config_overrides[test_type] = enabled
        verb = "enabled" if enabled else "disabled"
        return f"{test_type} tests {verb} for this session."

    async def _tool_run_plan(self, repos: list[str] | None = None) -> str:
        target_repos = repos or self.state.repos
        if not target_repos:
            return (
                "[error] No repositories in the session. "
                "Please add repos first with add_repos."
            )

        config = self._make_plan_config()
        targets = [RepoTarget(url=r, doc_links=self.state.doc_links) for r in target_repos]

        self.console.print(
            f"\n[dim]  ↳ Running strategist on {len(targets)} repo(s)...[/dim]"
        )
        orchestrator = QAOrchestrator(config)
        runs = await orchestrator.run(targets)
        self.state.qa_runs.extend(runs)

        lines: list[str] = []
        for run in runs:
            repo_short = run.repo_url.rstrip("/").split("/")[-1]
            if run.test_plan:
                plan = run.test_plan
                stack_parts = plan.tech_stack.languages + plan.tech_stack.frameworks
                lines.append(f"=== {repo_short} ===")
                lines.append(f"Tech stack: {', '.join(stack_parts) or 'unknown'}")
                lines.append(f"Risk areas: {', '.join(plan.overall_risk_areas) or 'none identified'}")
                lines.append(f"Test plan ({len(plan.entries)} entries):")
                for entry in plan.entries:
                    rationale = entry.rationale[:90] + "…" if len(entry.rationale) > 90 else entry.rationale
                    lines.append(
                        f"  • [{entry.priority}] {entry.test_type}"
                        f" ({entry.suggested_framework}) — {rationale}"
                    )
                if run.output_directory:
                    lines.append(f"Plan saved: {run.output_directory}/test_plan.json")
            else:
                lines.append(f"=== {repo_short} — no plan generated ===")

        return "\n".join(lines)

    async def _tool_run_analyze(
        self,
        repos: list[str] | None = None,
        skip_types: list[str] | None = None,
    ) -> str:
        target_repos = repos or self.state.repos
        if not target_repos:
            return (
                "[error] No repositories in the session. "
                "Please add repos first with add_repos."
            )

        config = self._make_config(skip_types=skip_types)
        targets = [RepoTarget(url=r, doc_links=self.state.doc_links) for r in target_repos]

        self.console.print(
            f"\n[dim]  ↳ Running full QA analysis on {len(targets)} repo(s)...[/dim]"
        )
        orchestrator = QAOrchestrator(config)
        runs = await orchestrator.run(targets)
        self.state.qa_runs.extend(runs)

        lines: list[str] = []
        for run in runs:
            repo_short = run.repo_url.rstrip("/").split("/")[-1]
            status = "✓" if run.success else "⚠ partial"
            lines.append(f"=== {repo_short} [{status}] ===")
            if run.output_directory:
                lines.append(f"Output: {run.output_directory}")
            for result in run.specialist_results:
                files_count = len(result.generated_files)
                errors = f", {len(result.errors)} error(s)" if result.errors else ""
                lines.append(f"  • {result.test_type}: {files_count} file(s) generated{errors}")
                for f in result.generated_files:
                    lines.append(f"      {f.filename}")

        return "\n".join(lines)

    async def _tool_run_platform_plan(self, platform_yaml: str) -> str:
        from pathlib import Path

        from ..core.platform_config import load_platform

        if not Path(platform_yaml).exists():
            return f"[error] Platform file not found: {platform_yaml}"

        try:
            platform_name, services, doc_links = load_platform(platform_yaml)
        except Exception as exc:
            return f"[error] Failed to parse {platform_yaml}: {exc}"

        config = self._make_config()
        self.console.print(
            f"\n[dim]  ↳ Discovering {platform_name} architecture ({len(services)} services)...[/dim]"
        )
        orchestrator = PlatformOrchestrator(config)
        run = await orchestrator.run(
            platform_name=platform_name,
            services=services,
            global_doc_links=doc_links,
            run_per_service=False,
            run_contracts=False,
        )
        self.state.platform_run = run

        if not run.platform_plan:
            return "[error] No platform plan generated."

        arch = run.platform_plan.architecture
        lines = [f"Platform: {platform_name}"]
        lines.append(f"Services ({len(arch.services)}): {', '.join(s.name for s in arch.services)}")
        if arch.contracts:
            lines.append(f"Contracts discovered ({len(arch.contracts)}):")
            for c in arch.contracts:
                lines.append(f"  • {c.consumer} → {c.provider} [{c.contract_type}]")
        else:
            lines.append("No inter-service contracts discovered.")
        if arch.topology_notes:
            lines.append(f"Topology notes: {arch.topology_notes[:200]}")
        return "\n".join(lines)

    async def _tool_run_platform_analyze(
        self, platform_yaml: str, skip_types: list[str] | None = None
    ) -> str:
        from pathlib import Path

        from ..core.platform_config import load_platform

        if not Path(platform_yaml).exists():
            return f"[error] Platform file not found: {platform_yaml}"

        try:
            platform_name, services, doc_links = load_platform(platform_yaml)
        except Exception as exc:
            return f"[error] Failed to parse {platform_yaml}: {exc}"

        config = self._make_config(skip_types=skip_types)
        self.console.print(
            f"\n[dim]  ↳ Running platform QA analysis: {platform_name} ({len(services)} services)...[/dim]"
        )
        orchestrator = PlatformOrchestrator(config)
        run = await orchestrator.run(
            platform_name=platform_name,
            services=services,
            global_doc_links=doc_links,
            run_per_service=True,
            run_contracts=True,
        )
        self.state.platform_run = run

        status = "✓" if run.success else "⚠ partial"
        lines = [f"Platform: {platform_name} [{status}]"]
        for svc_name, qa_run in run.service_runs.items():
            svc_status = "✓" if qa_run.success else "⚠"
            files = sum(len(r.generated_files) for r in qa_run.specialist_results)
            types = ", ".join(r.test_type for r in qa_run.specialist_results)
            lines.append(f"  {svc_status} {svc_name}: {files} files ({types})")
        if run.contract_results:
            lines.append(f"Contract tests: {len(run.contract_results)} generated")
        if run.output_directory:
            lines.append(f"Output: {run.output_directory}")
        return "\n".join(lines)

    async def _tool_show_state(self) -> str:
        lines: list[str] = ["=== Session State ==="]

        lines.append(f"Repos ({len(self.state.repos)}):")
        for r in self.state.repos:
            lines.append(f"  • {r}")
        if not self.state.repos:
            lines.append("  (none)")

        lines.append(f"Doc links ({len(self.state.doc_links)}):")
        for d in self.state.doc_links:
            lines.append(f"  • {d}")
        if not self.state.doc_links:
            lines.append("  (none)")

        if self.state.config_overrides:
            lines.append("Config overrides:")
            for k, v in self.state.config_overrides.items():
                lines.append(f"  • {k}: {'enabled' if v else 'disabled'}")
        else:
            lines.append("Config: defaults (all enabled except integration, api)")

        if self.state.qa_runs:
            last: QARun = self.state.qa_runs[-1]
            files = sum(len(r.generated_files) for r in last.specialist_results)
            lines.append(f"Last run: {last.run_id[:8]} — {files} files, success={last.success}")
        else:
            lines.append("No runs this session.")

        return "\n".join(lines)

    async def _tool_exit_session(self) -> str:
        self._should_exit = True
        return "Exiting session."

    # ── Config helpers ─────────────────────────────────────────────────────────

    def _make_config(self, skip_types: list[str] | None = None) -> QAConfig:
        """Return a fresh QAConfig with session config_overrides and skip_types applied."""
        config = QAConfig(  # type: ignore[call-arg]
            anthropic_api_key=self.base_config.anthropic_api_key,
            model=self.base_config.model,
            output_dir=self.base_config.output_dir,
            max_repo_size_mb=self.base_config.max_repo_size_mb,
            lint_generated=self.base_config.lint_generated,
            concurrency_limit=self.base_config.concurrency_limit,
        )
        # Apply persistent session overrides
        for test_type, enabled in self.state.config_overrides.items():
            specialist = getattr(config.specialists, test_type, None)
            if specialist is not None:
                specialist.enabled = enabled
        # Apply per-call skip_types (one-off, not stored in session)
        for test_type in skip_types or []:
            specialist = getattr(config.specialists, test_type, None)
            if specialist is not None:
                specialist.enabled = False
        return config

    def _make_plan_config(self) -> QAConfig:
        """Return a config with ALL specialists disabled (strategist-only run)."""
        config = self._make_config()
        all_types = ["functional", "performance", "security", "integration", "api", "e2e", "contract"]
        for test_type in all_types:
            getattr(config.specialists, test_type).enabled = False
        return config
