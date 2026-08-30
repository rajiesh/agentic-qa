"""
Tests for Phase 6 — CostTracker and budget enforcement.

Covers:
- Token accumulation (all four buckets)
- Estimated cost calculation with known token counts
- budget_usd=None → no enforcement, no error
- check_budget() raises BudgetExceededError when over cap
- BudgetExceededError carries budget and cost fields
- Concurrent record() calls are safe (asyncio)
- summary() returns serialisable dict with remaining budget
- BaseAgent._run_loop calls record() and check_budget() after each response
- QAConfig.cost_budget_usd field present with None default
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_qa.core.cost_tracker import (
    BudgetExceededError,
    CostTracker,
    _CACHE_CREATE_USD_PER_M,
    _CACHE_READ_USD_PER_M,
    _INPUT_USD_PER_M,
    _OUTPUT_USD_PER_M,
)
from agentic_qa.config import QAConfig


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _tracker(budget: float | None = None) -> CostTracker:
    return CostTracker(budget_usd=budget)


def _usage(
    inp: int = 0,
    out: int = 0,
    cache_create: int = 0,
    cache_read: int = 0,
) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cache_create,
        "cache_read_input_tokens": cache_read,
    }


# ── CostTracker — basic accumulation ────────────────────────────────────────────


class TestCostTrackerAccumulation:
    @pytest.mark.asyncio
    async def test_initial_state_zero(self):
        t = _tracker()
        assert t.estimated_cost_usd == 0.0
        assert t.total_tokens == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    @pytest.mark.asyncio
    async def test_record_accumulates_input_tokens(self):
        t = _tracker()
        await t.record(_usage(inp=1_000_000))
        await t.record(_usage(inp=500_000))
        assert t.total_tokens["input_tokens"] == 1_500_000

    @pytest.mark.asyncio
    async def test_record_accumulates_all_buckets(self):
        t = _tracker()
        await t.record(_usage(inp=100, out=50, cache_create=200, cache_read=300))
        assert t.total_tokens["input_tokens"] == 100
        assert t.total_tokens["output_tokens"] == 50
        assert t.total_tokens["cache_creation_input_tokens"] == 200
        assert t.total_tokens["cache_read_input_tokens"] == 300

    @pytest.mark.asyncio
    async def test_record_missing_keys_default_to_zero(self):
        t = _tracker()
        await t.record({})   # empty dict — all zeros
        assert t.total_tokens["input_tokens"] == 0

    @pytest.mark.asyncio
    async def test_multiple_records_sum_correctly(self):
        t = _tracker()
        for _ in range(5):
            await t.record(_usage(inp=10, out=5))
        assert t.total_tokens["input_tokens"] == 50
        assert t.total_tokens["output_tokens"] == 25


# ── CostTracker — cost calculation ──────────────────────────────────────────────


class TestCostCalculation:
    @pytest.mark.asyncio
    async def test_input_tokens_only(self):
        t = _tracker()
        await t.record(_usage(inp=1_000_000))
        expected = _INPUT_USD_PER_M  # $3.00
        assert abs(t.estimated_cost_usd - expected) < 1e-9

    @pytest.mark.asyncio
    async def test_output_tokens_only(self):
        t = _tracker()
        await t.record(_usage(out=1_000_000))
        expected = _OUTPUT_USD_PER_M  # $15.00
        assert abs(t.estimated_cost_usd - expected) < 1e-9

    @pytest.mark.asyncio
    async def test_cache_create_tokens(self):
        t = _tracker()
        await t.record(_usage(cache_create=1_000_000))
        expected = _CACHE_CREATE_USD_PER_M  # $3.75
        assert abs(t.estimated_cost_usd - expected) < 1e-9

    @pytest.mark.asyncio
    async def test_cache_read_tokens(self):
        t = _tracker()
        await t.record(_usage(cache_read=1_000_000))
        expected = _CACHE_READ_USD_PER_M  # $0.30
        assert abs(t.estimated_cost_usd - expected) < 1e-9

    @pytest.mark.asyncio
    async def test_combined_cost(self):
        """Typical run: 100K input, 10K output, 50K cache reads."""
        t = _tracker()
        await t.record(_usage(inp=100_000, out=10_000, cache_read=50_000))
        expected = (
            100_000 * _INPUT_USD_PER_M / 1_000_000
            + 10_000 * _OUTPUT_USD_PER_M / 1_000_000
            + 50_000 * _CACHE_READ_USD_PER_M / 1_000_000
        )
        assert abs(t.estimated_cost_usd - expected) < 1e-9


# ── CostTracker — budget enforcement ────────────────────────────────────────────


class TestBudgetEnforcement:
    def test_no_budget_never_raises(self):
        t = _tracker(budget=None)
        t.check_budget()  # should not raise

    @pytest.mark.asyncio
    async def test_under_budget_no_error(self):
        t = _tracker(budget=5.00)
        await t.record(_usage(inp=100_000))  # tiny cost ≪ $5
        t.check_budget()  # should not raise

    @pytest.mark.asyncio
    async def test_over_budget_raises(self):
        t = _tracker(budget=0.001)  # $0.001 cap
        await t.record(_usage(inp=1_000_000))  # $3 — over cap
        with pytest.raises(BudgetExceededError):
            t.check_budget()

    @pytest.mark.asyncio
    async def test_budget_exceeded_error_fields(self):
        t = _tracker(budget=1.00)
        await t.record(_usage(out=1_000_000))  # $15 — way over
        try:
            t.check_budget()
            pytest.fail("Expected BudgetExceededError")
        except BudgetExceededError as exc:
            assert exc.budget_usd == 1.00
            assert exc.estimated_cost_usd > 1.00

    @pytest.mark.asyncio
    async def test_budget_exceeded_error_message(self):
        t = _tracker(budget=0.50)
        await t.record(_usage(out=100_000))
        with pytest.raises(BudgetExceededError, match=r"\$0\.50"):
            t.check_budget()

    def test_budget_property(self):
        t = _tracker(budget=3.50)
        assert t.budget_usd == 3.50

    def test_budget_none_property(self):
        t = _tracker()
        assert t.budget_usd is None


# ── CostTracker — concurrent safety ─────────────────────────────────────────────


class TestConcurrentRecord:
    @pytest.mark.asyncio
    async def test_concurrent_record_sums_correctly(self):
        t = _tracker()
        tasks = [t.record(_usage(inp=1000)) for _ in range(20)]
        await asyncio.gather(*tasks)
        assert t.total_tokens["input_tokens"] == 20_000

    @pytest.mark.asyncio
    async def test_concurrent_budget_check_after_record(self):
        """All 10 tasks record tokens; budget exceeded only after total crosses cap."""
        t = _tracker(budget=0.001)  # very tight: ~$0.001

        async def _task() -> None:
            await t.record(_usage(inp=100_000))  # $0.30 each

        # Run all 10 tasks; only the ones that push over the budget should raise
        tasks = [_task() for _ in range(10)]
        await asyncio.gather(*tasks)
        # After all, check once — it should raise because $3.00 >> $0.001
        with pytest.raises(BudgetExceededError):
            t.check_budget()


# ── CostTracker — summary ────────────────────────────────────────────────────────


class TestSummary:
    @pytest.mark.asyncio
    async def test_summary_keys_present(self):
        t = _tracker(budget=5.00)
        await t.record(_usage(inp=1000, out=500))
        s = t.summary()
        assert "input_tokens" in s
        assert "output_tokens" in s
        assert "cache_creation_input_tokens" in s
        assert "cache_read_input_tokens" in s
        assert "estimated_cost_usd" in s
        assert "budget_usd" in s
        assert "budget_remaining_usd" in s

    @pytest.mark.asyncio
    async def test_summary_remaining_decreases(self):
        t = _tracker(budget=5.00)
        await t.record(_usage(inp=1_000_000))  # $3.00
        s = t.summary()
        assert s["budget_remaining_usd"] is not None
        assert s["budget_remaining_usd"] < 5.00  # type: ignore[operator]

    def test_summary_no_budget_remaining_is_none(self):
        t = _tracker()
        s = t.summary()
        assert s["budget_usd"] is None
        assert s["budget_remaining_usd"] is None

    @pytest.mark.asyncio
    async def test_summary_cost_is_float(self):
        t = _tracker()
        await t.record(_usage(inp=50_000))
        s = t.summary()
        assert isinstance(s["estimated_cost_usd"], float)


# ── QAConfig Phase 6 field ───────────────────────────────────────────────────────


class TestQAConfigCostBudget:
    def test_cost_budget_default_none(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = QAConfig()  # type: ignore[call-arg]
        assert config.cost_budget_usd is None

    def test_cost_budget_env_override(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("COST_BUDGET_USD", "7.50")
        config = QAConfig()  # type: ignore[call-arg]
        assert config.cost_budget_usd == 7.50


# ── BaseAgent integration ────────────────────────────────────────────────────────


class TestBaseAgentCostTrackerIntegration:
    """
    Verify that BaseAgent._run_loop calls cost_tracker.record() and
    check_budget() on every iteration.
    """

    def _make_agent_class(self):
        """Return a minimal concrete BaseAgent subclass for testing."""
        from agentic_qa.agents.base_agent import BaseAgent

        class _TestAgent(BaseAgent):
            AGENT_ROLE = "test"

            def _build_system_prompt(self) -> str:
                return "test"

            def _setup_tools(self) -> None:
                pass

            async def run(self, **_kwargs: Any) -> Any:
                pass

        return _TestAgent

    def _fake_config(self) -> MagicMock:
        cfg = MagicMock()
        cfg.model = "claude-sonnet-4-6"
        cfg.max_tokens_specialist = 4096
        cfg.max_retries = 0
        cfg.retry_base_wait_secs = 0.0
        cfg.retry_max_wait_secs = 0.0
        cfg.max_context_tool_pairs = 10
        return cfg

    @pytest.mark.asyncio
    async def test_run_loop_records_usage_on_each_iteration(self):
        """_run_loop must call cost_tracker.record() once per API response."""
        AgentClass = self._make_agent_class()
        tracker = _tracker(budget=None)

        # Fake API response: end_turn immediately
        fake_response = MagicMock()
        fake_response.stop_reason = "end_turn"
        fake_response.content = []
        fake_usage = MagicMock()
        fake_usage.input_tokens = 1000
        fake_usage.output_tokens = 200
        fake_usage.cache_creation_input_tokens = 0
        fake_usage.cache_read_input_tokens = 0
        fake_response.usage = fake_usage

        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(return_value=fake_response)

        agent = AgentClass(
            client=client,
            config=self._fake_config(),
            cost_tracker=tracker,
        )

        await agent._run_loop("hello", max_iterations=1)

        assert tracker.total_tokens["input_tokens"] == 1000
        assert tracker.total_tokens["output_tokens"] == 200

    @pytest.mark.asyncio
    async def test_run_loop_raises_budget_exceeded(self):
        """_run_loop must raise BudgetExceededError when the tracker triggers it."""
        AgentClass = self._make_agent_class()
        tracker = _tracker(budget=0.0)  # $0 budget — raises on any tokens

        fake_response = MagicMock()
        fake_response.stop_reason = "end_turn"
        fake_response.content = []
        fake_usage = MagicMock()
        fake_usage.input_tokens = 1
        fake_usage.output_tokens = 1
        fake_usage.cache_creation_input_tokens = 0
        fake_usage.cache_read_input_tokens = 0
        fake_response.usage = fake_usage

        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(return_value=fake_response)

        agent = AgentClass(
            client=client,
            config=self._fake_config(),
            cost_tracker=tracker,
        )

        with pytest.raises(BudgetExceededError):
            await agent._run_loop("hello", max_iterations=5)

    @pytest.mark.asyncio
    async def test_run_loop_no_tracker_no_error(self):
        """Without a tracker, _run_loop must not call any cost methods."""
        AgentClass = self._make_agent_class()

        fake_response = MagicMock()
        fake_response.stop_reason = "end_turn"
        fake_response.content = []
        fake_usage = MagicMock()
        fake_usage.input_tokens = 5_000_000  # would blow any budget
        fake_usage.output_tokens = 1_000_000
        fake_usage.cache_creation_input_tokens = 0
        fake_usage.cache_read_input_tokens = 0
        fake_response.usage = fake_usage

        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(return_value=fake_response)

        agent = AgentClass(
            client=client,
            config=self._fake_config(),
            cost_tracker=None,
        )

        # Should complete without raising
        _text, usage = await agent._run_loop("hello", max_iterations=1)
        assert usage["input_tokens"] == 5_000_000
