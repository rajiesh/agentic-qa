"""Tests for the agentic-qa entry point (agentic_qa/__main__.py)."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from agentic_qa.__main__ import main


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "interactive session" in capsys.readouterr().out


def test_subcommands_are_rejected():
    with pytest.raises(SystemExit) as exc:
        main(["analyze", "todo-app/backend"])
    assert exc.value.code == 2


def test_missing_api_key_returns_1(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    with patch("agentic_qa.__main__.InteractiveSession") as MockSession:
        assert main([]) == 1
    MockSession.assert_not_called()


def test_launches_interactive_session(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with patch("agentic_qa.__main__.InteractiveSession") as MockSession:
        MockSession.return_value.run = AsyncMock()
        assert main([]) == 0
    MockSession.return_value.run.assert_awaited_once()


def test_third_party_http_logs_quieted_unless_verbose(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with patch("agentic_qa.__main__.InteractiveSession") as MockSession:
        MockSession.return_value.run = AsyncMock()
        main([])
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("anthropic").level == logging.WARNING
