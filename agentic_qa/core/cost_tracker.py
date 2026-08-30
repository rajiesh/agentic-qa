"""
cost_tracker.py — Asyncio-safe token accumulator with optional USD budget enforcement.

One CostTracker instance is shared across all agents within a single run (platform or
single-repo). Agents call ``await tracker.record(usage_delta)`` after every API response,
then ``tracker.check_budget()`` raises ``BudgetExceededError`` if the accumulated cost
has exceeded the configured cap.

Pricing (claude-sonnet-4-6 as of 2026)
---------------------------------------
  Input tokens      $3.00 / 1 M tokens
  Output tokens    $15.00 / 1 M tokens
  Cache creation    $3.75 / 1 M tokens
  Cache reads       $0.30 / 1 M tokens

All four token buckets contribute to the estimated cost so prompt-cache runs get
accurate estimates rather than overestimates.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# ── Pricing constants (USD per 1 M tokens) ────────────────────────────────────
_INPUT_USD_PER_M: float = 3.00
_OUTPUT_USD_PER_M: float = 15.00
_CACHE_CREATE_USD_PER_M: float = 3.75
_CACHE_READ_USD_PER_M: float = 0.30


class BudgetExceededError(Exception):
    """
    Raised synchronously (inside ``check_budget``) when the accumulated
    estimated cost exceeds the configured ``budget_usd`` cap.

    Attributes
    ----------
    budget_usd : float
        The configured cap that was exceeded.
    estimated_cost_usd : float
        The cost that tripped the check.
    """

    def __init__(self, budget_usd: float, estimated_cost_usd: float) -> None:
        self.budget_usd = budget_usd
        self.estimated_cost_usd = estimated_cost_usd
        super().__init__(
            f"Cost budget ${budget_usd:.2f} exceeded — "
            f"estimated spend so far: ${estimated_cost_usd:.4f}"
        )


class CostTracker:
    """
    Thread-safe (asyncio) token accumulator.

    Usage
    -----
    ::

        tracker = CostTracker(budget_usd=5.00)

        # Inside BaseAgent._run_loop after each API response:
        await tracker.record(usage_delta)
        tracker.check_budget()           # raises BudgetExceededError if over budget

        # After the run:
        print(tracker.summary())
    """

    def __init__(self, budget_usd: float | None = None) -> None:
        """
        Parameters
        ----------
        budget_usd:
            Maximum allowed spend in USD. ``None`` means unlimited.
        """
        self._budget_usd = budget_usd
        self._lock = asyncio.Lock()
        self._input_tokens: int = 0
        self._output_tokens: int = 0
        self._cache_creation_tokens: int = 0
        self._cache_read_tokens: int = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    async def record(self, usage: dict[str, int]) -> None:
        """
        Add one API response's token usage to the running totals.

        Parameters
        ----------
        usage:
            Dict with keys ``input_tokens``, ``output_tokens``,
            ``cache_creation_input_tokens``, ``cache_read_input_tokens``.
            Missing keys default to 0.
        """
        async with self._lock:
            self._input_tokens += usage.get("input_tokens", 0)
            self._output_tokens += usage.get("output_tokens", 0)
            self._cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
            self._cache_read_tokens += usage.get("cache_read_input_tokens", 0)

    def check_budget(self) -> None:
        """
        Raise ``BudgetExceededError`` if the current estimated cost exceeds
        the configured budget. No-op when ``budget_usd`` is ``None``.

        Safe to call without the lock — reads are GIL-protected and this is
        called immediately after ``await record()``.
        """
        if self._budget_usd is None:
            return
        cost = self.estimated_cost_usd
        if cost > self._budget_usd:
            logger.warning(
                "Budget exceeded: $%.4f > $%.2f — raising BudgetExceededError",
                cost, self._budget_usd,
            )
            raise BudgetExceededError(self._budget_usd, cost)

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def estimated_cost_usd(self) -> float:
        """Current estimated cost in USD (read without locking — approximate)."""
        return (
            self._input_tokens * _INPUT_USD_PER_M / 1_000_000
            + self._output_tokens * _OUTPUT_USD_PER_M / 1_000_000
            + self._cache_creation_tokens * _CACHE_CREATE_USD_PER_M / 1_000_000
            + self._cache_read_tokens * _CACHE_READ_USD_PER_M / 1_000_000
        )

    @property
    def total_tokens(self) -> dict[str, int]:
        """Snapshot of all accumulated token counts."""
        return {
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "cache_creation_input_tokens": self._cache_creation_tokens,
            "cache_read_input_tokens": self._cache_read_tokens,
        }

    @property
    def budget_usd(self) -> float | None:
        """The configured budget cap (``None`` = unlimited)."""
        return self._budget_usd

    # ── Convenience ────────────────────────────────────────────────────────────

    def summary(self) -> dict[str, object]:
        """
        Return a serialisable summary dict suitable for writing to a run report.

        Example output::

            {
                "input_tokens": 120000,
                "output_tokens": 8000,
                "cache_creation_input_tokens": 40000,
                "cache_read_input_tokens": 200000,
                "estimated_cost_usd": 0.1382,
                "budget_usd": 5.0,
                "budget_remaining_usd": 4.8618
            }
        """
        cost = self.estimated_cost_usd
        remaining = (self._budget_usd - cost) if self._budget_usd is not None else None
        return {
            **self.total_tokens,
            "estimated_cost_usd": round(cost, 6),
            "budget_usd": self._budget_usd,
            "budget_remaining_usd": round(remaining, 6) if remaining is not None else None,
        }

    def __repr__(self) -> str:
        return (
            f"CostTracker(estimated=${self.estimated_cost_usd:.4f}, "
            f"budget={self._budget_usd}, "
            f"tokens={self.total_tokens})"
        )
