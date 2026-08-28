"""
session.py — Interactive REPL session for agentic-qa.

Running `agentic-qa` with no arguments starts this session. The user can:
  - Add repos and doc links conversationally
  - Ask for a test plan (strategist only, no code generated)
  - Ask for a full analysis (generates test code)
  - Use slash commands for quick state inspection

Session memory lives in SessionState for the duration of the process.
The conversation_history field is passed to Claude on every turn so the
assistant has full context of everything said and done in the session.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

import anthropic
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .agents.session_agent import SessionAgent
from .config import QAConfig

logger = logging.getLogger(__name__)

# ── Session state ─────────────────────────────────────────────────────────────


@dataclass
class SessionState:
    """In-memory state for one interactive agentic-qa session."""

    repos: list[str] = field(default_factory=list)
    """Repository URLs or local paths added during this session."""

    doc_links: list[str] = field(default_factory=list)
    """Documentation URLs passed to all agents as context."""

    config_overrides: dict[str, bool] = field(default_factory=dict)
    """Persistent test-type toggles, e.g. {"security": False}."""

    qa_runs: list[Any] = field(default_factory=list)
    """QARun results accumulated across all analyze calls this session."""

    platform_run: Any = None
    """Most recent PlatformRun result (if any)."""

    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    """Full Claude messages list — user turns, assistant replies, tool results."""


# ── Slash command handler ─────────────────────────────────────────────────────

_HELP_TEXT = """\
**Slash commands**

| Command    | Description                              |
|------------|------------------------------------------|
| /repos     | List repos in the current session        |
| /docs      | List doc links in the current session    |
| /config    | Show enabled / disabled test types       |
| /runs      | Summarise runs completed this session    |
| /clear     | Reset repos, docs, overrides (keep history) |
| /reset     | Full reset: state + conversation history |
| /help      | Show this message                        |
| /exit      | Exit agentic-qa                          |
| /quit      | Exit agentic-qa                          |

Everything else is sent to the AI assistant.
"""


def _handle_slash(cmd: str, state: SessionState, console: Console) -> bool:
    """
    Handle a slash command.
    Returns True if the session should exit, False otherwise.
    """
    parts = cmd.strip().split(maxsplit=1)
    token = parts[0].lower()

    if token in ("/exit", "/quit"):
        return True

    if token == "/help":
        console.print(Markdown(_HELP_TEXT))
        return False

    if token == "/repos":
        if state.repos:
            for r in state.repos:
                console.print(f"  [dim]•[/dim] {r}")
        else:
            console.print("[dim]  No repos added yet.[/dim]")
        return False

    if token == "/docs":
        if state.doc_links:
            for d in state.doc_links:
                console.print(f"  [dim]•[/dim] {d}")
        else:
            console.print("[dim]  No doc links added yet.[/dim]")
        return False

    if token == "/config":
        defaults = {
            "functional": True,
            "performance": True,
            "security": True,
            "e2e": True,
            "integration": False,
            "api": False,
            "contract": True,
        }
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("Test type")
        table.add_column("Status")
        for t_type, default_enabled in defaults.items():
            effective = state.config_overrides.get(t_type, default_enabled)
            status = "[green]enabled[/]" if effective else "[dim]disabled[/]"
            override = " [yellow](overridden)[/]" if t_type in state.config_overrides else ""
            table.add_row(t_type, status + override)
        console.print(table)
        return False

    if token == "/runs":
        if not state.qa_runs and state.platform_run is None:
            console.print("[dim]  No runs this session.[/dim]")
        else:
            for run in state.qa_runs:
                short = run.repo_url.rstrip("/").split("/")[-1]
                files = sum(len(r.generated_files) for r in run.specialist_results)
                status = "[green]✓[/]" if run.success else "[yellow]⚠[/]"
                console.print(f"  {status} {short} — {files} files — {run.output_directory}")
            if state.platform_run:
                pr = state.platform_run
                s = "[green]✓[/]" if pr.success else "[yellow]⚠[/]"
                console.print(f"  {s} platform:{pr.platform_name} — {pr.output_directory}")
        return False

    if token == "/clear":
        state.repos.clear()
        state.doc_links.clear()
        state.config_overrides.clear()
        state.qa_runs.clear()
        state.platform_run = None
        console.print("[dim]  Session state cleared (conversation history kept).[/dim]")
        return False

    if token == "/reset":
        state.repos.clear()
        state.doc_links.clear()
        state.config_overrides.clear()
        state.qa_runs.clear()
        state.platform_run = None
        state.conversation_history.clear()
        console.print("[dim]  Full reset: state and conversation history cleared.[/dim]")
        return False

    console.print(f"[yellow]Unknown command:[/yellow] {token}  (try /help)")
    return False


# ── Interactive session ───────────────────────────────────────────────────────


class InteractiveSession:
    """
    The outer REPL loop for `agentic-qa` interactive mode.

    1. Prints a welcome banner
    2. Reads user input (with readline support)
    3. Dispatches slash commands directly
    4. Routes everything else to SessionAgent (Claude-powered)
    5. Renders the agent's text response via Rich Markdown
    6. Repeats until exit
    """

    def __init__(self, config: QAConfig) -> None:
        self.config = config
        self.console = Console()
        self.state = SessionState()
        self.client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
        self.agent = SessionAgent(
            client=self.client,
            base_config=config,
            state=self.state,
            console=self.console,
        )

    async def run(self) -> None:
        self._print_banner()
        while True:
            try:
                user_input = await self._read_input()
            except EOFError:
                # Ctrl+D
                self.console.print("\n[dim]Goodbye![/dim]")
                break
            except KeyboardInterrupt:
                # Ctrl+C — ask once, then allow
                self.console.print("\n[dim](Ctrl+C — type /exit or /quit to leave)[/dim]")
                continue

            stripped = user_input.strip()
            if not stripped:
                continue

            # Slash commands are handled locally without calling Claude
            if stripped.startswith("/"):
                should_exit = _handle_slash(stripped, self.state, self.console)
                if should_exit:
                    self.console.print("[dim]Goodbye![/dim]")
                    break
                continue

            # Everything else → Claude
            try:
                response = await self.agent.handle_turn(stripped)
            except KeyboardInterrupt:
                self.console.print("\n[dim](Interrupted — run cancelled)[/dim]")
                continue
            except Exception as exc:
                logger.error("Session agent error: %s", exc, exc_info=True)
                self.console.print(f"[red]Error:[/red] {exc}")
                continue

            self._render_response(response)

            if self.agent.should_exit:
                self.console.print("[dim]Goodbye![/dim]")
                break

    # ── Rendering ──────────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        self.console.print(
            Panel(
                "[bold cyan]agentic-qa[/bold cyan]  [dim]Multi-agent QA Analyst[/dim]\n"
                "[dim]Type a repo URL, ask for a plan or analysis, or use /help[/dim]",
                border_style="cyan",
                padding=(0, 2),
            )
        )
        self.console.print()

    def _render_response(self, text: str) -> None:
        self.console.print()
        # Try Markdown rendering; fall back to plain text
        try:
            self.console.print(
                Panel(
                    Markdown(text),
                    border_style="dim",
                    title="[bold]Agent[/bold]",
                    title_align="left",
                    padding=(0, 1),
                )
            )
        except Exception:
            self.console.print(f"[bold]Agent ›[/bold] {text}")
        self.console.print()

    # ── Input ──────────────────────────────────────────────────────────────────

    async def _read_input(self) -> str:
        """Read a line of user input without blocking the event loop."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._prompt)

    @staticmethod
    def _prompt() -> str:
        """Blocking prompt — runs in a thread executor."""
        _enable_readline()
        return input("You › ")


def _enable_readline() -> None:
    """Best-effort readline history support — silently skipped if unavailable."""
    try:
        import readline  # noqa: F401  (side-effect import)
    except ImportError:
        pass  # Windows — no readline; input() still works
