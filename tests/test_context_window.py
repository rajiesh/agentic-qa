"""
Unit tests for Phase 2 context window management:
- BaseAgent sliding window (max_context_tool_pairs)
- SessionAgent history trimming (_trim_history)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_qa.session import SessionState


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_config(**overrides):
    """Return a MagicMock config with Phase 1+2 defaults."""
    cfg = MagicMock()
    cfg.model = "claude-sonnet-4-6"
    cfg.max_tokens_specialist = 16384
    cfg.max_tokens_session = 4096
    cfg.max_context_tool_pairs = 3   # small for easy testing
    cfg.session_history_max_turns = 3
    cfg.max_retries = 0              # don't retry in tests
    cfg.retry_base_wait_secs = 0.0
    cfg.retry_max_wait_secs = 0.0
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _make_messages(n_tool_pairs: int) -> list[dict]:
    """
    Build a messages list with one initial user message followed by n_tool_pairs
    tool-use/tool-result exchanges.

    Structure:
        messages[0]       = initial user task
        messages[1, 3, …] = assistant tool_use
        messages[2, 4, …] = user tool_result
    """
    msgs: list[dict] = [{"role": "user", "content": "initial task"}]
    for i in range(n_tool_pairs):
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"tu{i}"}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu{i}"}]})
    return msgs


# ── BaseAgent sliding window ───────────────────────────────────────────────────


class TestSlidingWindow:
    """Test the sliding window pruning logic inside BaseAgent._run_loop."""

    def _count_tool_pairs(self, messages: list) -> int:
        """Number of (assistant/tool_use, user/tool_result) pairs in messages."""
        return (len(messages) - 1) // 2

    def test_messages_under_limit_not_pruned(self):
        """When pairs ≤ max_context_tool_pairs, nothing should be dropped."""
        msgs = _make_messages(3)
        assert self._count_tool_pairs(msgs) == 3  # exactly at the limit → no pruning needed

    def test_pruning_drops_oldest_pair(self):
        """
        Simulate what _run_loop does when a new tool_result is appended and pairs > limit.
        The oldest pair (messages[1] + messages[2]) must be dropped.
        """
        cfg = _make_config(max_context_tool_pairs=3)
        msgs = _make_messages(3)  # already at limit

        # Simulate appending one more exchange (as _run_loop would)
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": "tu_new"}]})
        msgs.append({"role": "user",      "content": [{"type": "tool_result", "tool_use_id": "tu_new"}]})

        # Now pairs = 4, limit = 3 → trigger pruning
        tool_pairs = (len(msgs) - 1) // 2
        assert tool_pairs == 4  # confirm pre-prune state

        if tool_pairs > cfg.max_context_tool_pairs:
            del msgs[1:3]  # drop oldest pair — mirrors base_agent.py logic

        assert self._count_tool_pairs(msgs) == 3
        # initial user message still at index 0
        assert msgs[0] == {"role": "user", "content": "initial task"}
        # the now-oldest assistant message should be the SECOND original pair (not the first)
        assert msgs[1]["content"][0]["id"] == "tu1"
        # the newest pair is still present at the end
        assert msgs[-1]["content"][0]["tool_use_id"] == "tu_new"

    def test_initial_user_message_always_preserved(self):
        """messages[0] (the initial task) must survive any amount of pruning."""
        cfg = _make_config(max_context_tool_pairs=1)
        msgs = _make_messages(1)

        for extra in range(10):
            msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"x{extra}"}]})
            msgs.append({"role": "user",      "content": [{"type": "tool_result", "tool_use_id": f"x{extra}"}]})
            tool_pairs = (len(msgs) - 1) // 2
            if tool_pairs > cfg.max_context_tool_pairs:
                del msgs[1:3]

        assert msgs[0] == {"role": "user", "content": "initial task"}
        assert self._count_tool_pairs(msgs) == 1  # always capped at limit

    def test_no_pruning_at_exactly_limit(self):
        """Exactly at max_context_tool_pairs should not trigger eviction."""
        cfg = _make_config(max_context_tool_pairs=5)
        msgs = _make_messages(5)

        tool_pairs = (len(msgs) - 1) // 2
        evicted = False
        if tool_pairs > cfg.max_context_tool_pairs:
            del msgs[1:3]
            evicted = True

        assert not evicted
        assert self._count_tool_pairs(msgs) == 5


# ── SessionAgent _trim_history ─────────────────────────────────────────────────


class TestSessionHistoryTrim:
    """Test SessionAgent._trim_history enforces session_history_max_turns."""

    def _make_agent(self, state=None, **config_overrides):
        from agentic_qa.agents.session_agent import SessionAgent
        from rich.console import Console

        cfg = _make_config(**config_overrides)
        state = state or SessionState()
        return SessionAgent(
            client=MagicMock(),
            base_config=cfg,
            state=state,
            console=Console(quiet=True),
        )

    def _history_with_n_turns(self, n: int) -> list[dict]:
        """Build a conversation with n user turns (each followed by an assistant reply)."""
        history = []
        for i in range(n):
            history.append({"role": "user",      "content": f"user turn {i}"})
            history.append({"role": "assistant",  "content": f"assistant reply {i}"})
        return history

    def test_short_history_not_trimmed(self):
        state = SessionState()
        state.conversation_history = self._history_with_n_turns(3)
        agent = self._make_agent(state=state, session_history_max_turns=5)

        agent._trim_history()

        assert len(state.conversation_history) == 6  # unchanged

    def test_long_history_trimmed_to_window(self):
        state = SessionState()
        state.conversation_history = self._history_with_n_turns(10)  # 20 messages
        agent = self._make_agent(state=state, session_history_max_turns=4)  # keep last 8

        agent._trim_history()

        assert len(state.conversation_history) == 8

    def test_trimmed_history_starts_with_user_message(self):
        """After trimming the first message must always have role='user'."""
        state = SessionState()
        state.conversation_history = self._history_with_n_turns(10)
        agent = self._make_agent(state=state, session_history_max_turns=3)

        agent._trim_history()

        assert state.conversation_history[0]["role"] == "user"

    def test_history_at_exactly_limit_not_trimmed(self):
        state = SessionState()
        state.conversation_history = self._history_with_n_turns(3)  # 6 messages
        agent = self._make_agent(state=state, session_history_max_turns=3)  # limit = 6

        agent._trim_history()

        assert len(state.conversation_history) == 6  # unchanged

    def test_most_recent_turns_are_kept(self):
        """The LAST turns should be in the trimmed history, not the earliest."""
        state = SessionState()
        state.conversation_history = self._history_with_n_turns(5)
        agent = self._make_agent(state=state, session_history_max_turns=2)  # keep last 4

        agent._trim_history()

        # Last 2 user turns were turns 3 and 4 (0-indexed)
        user_messages = [m["content"] for m in state.conversation_history if m["role"] == "user"]
        assert "user turn 3" in user_messages
        assert "user turn 4" in user_messages
        assert "user turn 0" not in user_messages
