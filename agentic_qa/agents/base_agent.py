from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import anthropic

if TYPE_CHECKING:
    from ..core.cost_tracker import CostTracker

logger = logging.getLogger(__name__)

ToolHandler = Any  # Callable[..., Awaitable[str]]


class BaseAgent(ABC):
    """Abstract base for all QA agents. Manages the Claude tool_use agentic loop."""

    AGENT_ROLE: str = "base"

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        config: Any,  # QAConfig — avoid circular import
        agent_id: str | None = None,
        cost_tracker: "CostTracker | None" = None,
    ) -> None:
        self.client = client
        self.config = config
        self.agent_id = agent_id or f"{self.AGENT_ROLE}-{id(self)}"
        self._cost_tracker = cost_tracker
        self._tools: list[dict[str, Any]] = []
        self._tool_handlers: dict[str, ToolHandler] = {}
        self._setup_tools()

    @abstractmethod
    def _build_system_prompt(self) -> str: ...

    @abstractmethod
    def _setup_tools(self) -> None: ...

    @abstractmethod
    async def run(self, *args: Any, **kwargs: Any) -> Any: ...

    def _max_tokens(self) -> int:
        return self.config.max_tokens_specialist

    def _use_thinking(self) -> bool:
        return False

    async def _run_loop(
        self,
        user_message: str,
        max_iterations: int = 25,
        extra_messages: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, int]]:
        system: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": self._build_system_prompt(),
                "cache_control": {"type": "ephemeral"},
            }
        ]

        messages: list[dict[str, Any]] = list(extra_messages or [])
        messages.append({"role": "user", "content": user_message})

        usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

        for iteration in range(max_iterations):
            logger.debug("[%s] iteration %d/%d", self.agent_id, iteration + 1, max_iterations)

            kwargs: dict[str, Any] = dict(
                model=self.config.model,
                max_tokens=self._max_tokens(),
                system=system,
                messages=messages,
            )
            if self._tools:
                kwargs["tools"] = self._tools
            if self._use_thinking():
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": 5000}

            # Retry with exponential backoff on rate-limit / server errors
            response = None
            for attempt in range(self.config.max_retries + 1):
                try:
                    response = await self.client.messages.create(**kwargs)
                    break
                except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
                    if attempt == self.config.max_retries:
                        raise
                    wait = min(
                        self.config.retry_base_wait_secs * (2 ** attempt),
                        self.config.retry_max_wait_secs,
                    )
                    logger.warning(
                        "[%s] API error (attempt %d/%d), retrying in %.1fs: %s",
                        self.agent_id, attempt + 1, self.config.max_retries, wait, exc,
                    )
                    await asyncio.sleep(wait)
            assert response is not None  # guaranteed by loop above

            u = response.usage
            delta: dict[str, int] = {
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            }
            usage["input_tokens"] += delta["input_tokens"]
            usage["output_tokens"] += delta["output_tokens"]
            usage["cache_creation_input_tokens"] += delta["cache_creation_input_tokens"]
            usage["cache_read_input_tokens"] += delta["cache_read_input_tokens"]

            # Budget enforcement: record usage then raise if over cap
            if self._cost_tracker is not None:
                await self._cost_tracker.record(delta)
                self._cost_tracker.check_budget()  # raises BudgetExceededError if exceeded

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                text = next(
                    (b.text for b in response.content if hasattr(b, "text") and b.type == "text"),
                    "",
                )
                logger.info("[%s] done. tokens=%s", self.agent_id, usage)
                return text, usage

            if response.stop_reason == "tool_use":
                tool_results = await self._execute_tool_calls(response.content)
                messages.append({"role": "user", "content": tool_results})

                # Sliding window: keep at most max_context_tool_pairs tool-exchange pairs.
                # Each pair is (assistant/tool_use, user/tool_result) = 2 messages.
                # messages[0] is always the initial task prompt and is never evicted.
                tool_pairs = (len(messages) - 1) // 2
                if tool_pairs > self.config.max_context_tool_pairs:
                    logger.debug(
                        "[%s] context window: evicting oldest tool pair (%d > %d)",
                        self.agent_id, tool_pairs, self.config.max_context_tool_pairs,
                    )
                    del messages[1:3]  # drop oldest (assistant tool_use + user tool_result)

                continue

            logger.warning("[%s] unexpected stop_reason=%s", self.agent_id, response.stop_reason)
            break

        logger.warning("[%s] reached max_iterations=%d", self.agent_id, max_iterations)
        return "", usage

    async def _execute_tool_calls(self, content: list[Any]) -> list[dict[str, Any]]:
        tool_blocks = [b for b in content if hasattr(b, "type") and b.type == "tool_use"]

        async def _call_one(block: Any) -> dict[str, Any]:
            handler = self._tool_handlers.get(block.name)
            if not handler:
                return {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "is_error": True,
                    "content": f"Unknown tool: {block.name}",
                }
            try:
                result = await handler(**block.input)
                return {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result if isinstance(result, str) else str(result),
                }
            except Exception as exc:
                logger.error("[%s] tool %s failed: %s", self.agent_id, block.name, exc)
                return {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "is_error": True,
                    "content": f"Tool error: {exc}",
                }

        return list(await asyncio.gather(*[_call_one(b) for b in tool_blocks]))
