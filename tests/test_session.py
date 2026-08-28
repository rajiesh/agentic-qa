"""Tests for the interactive session: SessionState, slash commands, tool handlers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from agentic_qa.session import SessionState, _handle_slash


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_state(**kwargs) -> SessionState:
    return SessionState(**kwargs)


def _null_console() -> Console:
    """A Console that discards output (quiet tests)."""
    return Console(quiet=True)


# ── SessionState defaults ──────────────────────────────────────────────────────


def test_session_state_defaults():
    state = SessionState()
    assert state.repos == []
    assert state.doc_links == []
    assert state.config_overrides == {}
    assert state.qa_runs == []
    assert state.platform_run is None
    assert state.conversation_history == []


def test_session_state_accumulates_repos():
    state = SessionState()
    state.repos.append("https://github.com/foo/bar")
    state.repos.append("https://github.com/foo/baz")
    assert len(state.repos) == 2


def test_session_state_dataclass_isolation():
    """Each instance should have its own mutable defaults."""
    a = SessionState()
    b = SessionState()
    a.repos.append("https://example.com/repo")
    assert b.repos == []


# ── Slash command: /exit and /quit ─────────────────────────────────────────────


def test_slash_exit_returns_true():
    state = _make_state()
    con = _null_console()
    assert _handle_slash("/exit", state, con) is True


def test_slash_quit_returns_true():
    state = _make_state()
    con = _null_console()
    assert _handle_slash("/quit", state, con) is True


def test_slash_exit_case_insensitive():
    assert _handle_slash("/EXIT", _make_state(), _null_console()) is True


# ── Slash command: /repos, /docs, /config, /runs ──────────────────────────────


def test_slash_repos_returns_false():
    state = _make_state(repos=["https://github.com/org/repo"])
    assert _handle_slash("/repos", state, _null_console()) is False


def test_slash_docs_returns_false():
    state = _make_state(doc_links=["https://docs.example.com"])
    assert _handle_slash("/docs", state, _null_console()) is False


def test_slash_config_returns_false():
    state = _make_state(config_overrides={"security": False})
    assert _handle_slash("/config", state, _null_console()) is False


def test_slash_runs_no_runs():
    assert _handle_slash("/runs", _make_state(), _null_console()) is False


# ── Slash command: /clear ──────────────────────────────────────────────────────


def test_slash_clear_resets_state_keeps_history():
    state = _make_state(
        repos=["https://github.com/x/y"],
        doc_links=["https://docs.example.com"],
        config_overrides={"security": False},
        conversation_history=[{"role": "user", "content": "hello"}],
    )
    result = _handle_slash("/clear", state, _null_console())
    assert result is False
    assert state.repos == []
    assert state.doc_links == []
    assert state.config_overrides == {}
    # conversation_history is preserved by /clear
    assert len(state.conversation_history) == 1


def test_slash_reset_clears_everything():
    state = _make_state(
        repos=["https://github.com/x/y"],
        conversation_history=[{"role": "user", "content": "hello"}],
    )
    _handle_slash("/reset", state, _null_console())
    assert state.repos == []
    assert state.conversation_history == []


# ── Slash command: unknown ─────────────────────────────────────────────────────


def test_slash_unknown_returns_false():
    result = _handle_slash("/notacommand", _make_state(), _null_console())
    assert result is False


# ── SessionAgent tool handlers (unit tests with no network) ───────────────────


@pytest.fixture
def agent_factory():
    """Return a factory that builds a SessionAgent with mocked client and config."""
    from agentic_qa.agents.session_agent import SessionAgent
    from agentic_qa.config import QAConfig, SpecialistConfig

    def _make(state=None):
        mock_client = MagicMock()
        mock_config = MagicMock(spec=QAConfig)
        mock_config.anthropic_api_key = "test-key"
        mock_config.model = "claude-sonnet-4-6"
        mock_config.output_dir = "outputs"
        mock_config.max_repo_size_mb = 500
        mock_config.lint_generated = True
        mock_config.concurrency_limit = 3
        if state is None:
            state = SessionState()
        agent = SessionAgent(
            client=mock_client,
            base_config=mock_config,
            state=state,
            console=_null_console(),
        )
        return agent

    return _make


@pytest.mark.asyncio
async def test_tool_add_repos_new(agent_factory):
    agent = agent_factory()
    result = await agent._tool_add_repos(["https://github.com/foo/bar"])
    assert "bar" in result
    assert "https://github.com/foo/bar" in agent.state.repos


@pytest.mark.asyncio
async def test_tool_add_repos_duplicate_not_added(agent_factory):
    state = SessionState(repos=["https://github.com/foo/bar"])
    agent = agent_factory(state=state)
    result = await agent._tool_add_repos(["https://github.com/foo/bar"])
    assert "already" in result.lower()
    assert len(agent.state.repos) == 1


@pytest.mark.asyncio
async def test_tool_add_docs(agent_factory):
    agent = agent_factory()
    result = await agent._tool_add_docs(["https://docs.example.com"])
    assert "1" in result
    assert "https://docs.example.com" in agent.state.doc_links


@pytest.mark.asyncio
async def test_tool_configure_disable(agent_factory):
    agent = agent_factory()
    result = await agent._tool_configure("security", False)
    assert agent.state.config_overrides["security"] is False
    assert "disabled" in result.lower()


@pytest.mark.asyncio
async def test_tool_configure_enable(agent_factory):
    agent = agent_factory()
    result = await agent._tool_configure("integration", True)
    assert agent.state.config_overrides["integration"] is True
    assert "enabled" in result.lower()


@pytest.mark.asyncio
async def test_tool_run_plan_no_repos(agent_factory):
    agent = agent_factory()
    result = await agent._tool_run_plan()
    assert "[error]" in result or "no repositories" in result.lower()


@pytest.mark.asyncio
async def test_tool_run_analyze_no_repos(agent_factory):
    agent = agent_factory()
    result = await agent._tool_run_analyze()
    assert "[error]" in result or "no repositories" in result.lower()


@pytest.mark.asyncio
async def test_tool_show_state(agent_factory):
    state = SessionState(
        repos=["https://github.com/foo/bar"],
        doc_links=["https://docs.example.com"],
        config_overrides={"security": False},
    )
    agent = agent_factory(state=state)
    result = await agent._tool_show_state()
    assert "foo/bar" in result
    assert "docs.example.com" in result
    assert "security" in result


@pytest.mark.asyncio
async def test_tool_exit_session(agent_factory):
    agent = agent_factory()
    assert not agent.should_exit
    result = await agent._tool_exit_session()
    assert agent.should_exit is True
    assert "exit" in result.lower() or "goodbye" in result.lower()


# ── _make_config applies session overrides ─────────────────────────────────────


def test_make_config_applies_config_overrides(agent_factory):
    state = SessionState(config_overrides={"security": False, "integration": True})
    agent = agent_factory(state=state)

    with patch("agentic_qa.agents.session_agent.QAConfig") as MockConfig:
        mock_cfg = MagicMock()
        mock_cfg.specialists.security.enabled = True
        mock_cfg.specialists.integration.enabled = False
        MockConfig.return_value = mock_cfg

        agent._make_config()

        # Overrides applied
        assert mock_cfg.specialists.security.enabled is False
        assert mock_cfg.specialists.integration.enabled is True


def test_make_plan_config_disables_all(agent_factory):
    agent = agent_factory()

    with patch("agentic_qa.agents.session_agent.QAConfig") as MockConfig:
        mock_cfg = MagicMock()
        for t in ["functional", "performance", "security", "integration", "api", "e2e", "contract"]:
            getattr(mock_cfg.specialists, t).enabled = True
        MockConfig.return_value = mock_cfg

        agent._make_plan_config()

        for t in ["functional", "performance", "security", "integration", "api", "e2e", "contract"]:
            assert getattr(mock_cfg.specialists, t).enabled is False
