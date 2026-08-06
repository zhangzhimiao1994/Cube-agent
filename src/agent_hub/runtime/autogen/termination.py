"""Stable, budget-aware termination for AutoGen discussions."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from autogen_agentchat.base import TerminationCondition
from autogen_agentchat.messages import BaseAgentEvent, BaseChatMessage, StopMessage


@dataclass(slots=True)
class DiscussionUsage:
    tokens: int = 0
    cost_usd: Decimal = Decimal(0)


@dataclass(slots=True)
class CompositeDiscussionTermination(TerminationCondition):
    """Evaluate application budgets without persisting framework state."""

    usage: DiscussionUsage
    max_turns: int
    token_budget: int
    cost_budget_usd: Decimal
    wall_time_seconds: float
    consensus_votes: int
    cancelled: Callable[[], bool]
    monotonic: Callable[[], float] = time.monotonic
    completion_marker: str = "[COMPLETE]"
    consensus_marker: str = "[CONSENSUS]"
    reason: str | None = field(default=None, init=False)
    _started_at: float = field(default=0.0, init=False)
    _turns: int = field(default=0, init=False)
    _votes: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self._started_at = self.monotonic()

    @property
    def terminated(self) -> bool:
        return self.reason is not None

    async def __call__(
        self, messages: Sequence[BaseAgentEvent | BaseChatMessage]
    ) -> StopMessage | None:
        if self.terminated:
            return StopMessage(content=self.reason or "terminated", source="agent_hub")
        self._turns += sum(isinstance(message, BaseChatMessage) for message in messages)
        for message in messages:
            if not isinstance(message, BaseChatMessage):
                continue
            text = message.to_text()
            if self.consensus_marker in text:
                self._votes.add(message.source)
        if self.cancelled():
            self.reason = "cancelled"
        elif self.monotonic() - self._started_at >= self.wall_time_seconds:
            self.reason = "wall_time"
        elif self.usage.tokens >= self.token_budget or self.usage.cost_usd >= self.cost_budget_usd:
            self.reason = "budget_exhausted"
        elif any(
            isinstance(message, BaseChatMessage) and self.completion_marker in message.to_text()
            for message in messages
        ):
            self.reason = "explicit_completion"
        elif len(self._votes) >= self.consensus_votes:
            self.reason = "consensus"
        elif self._turns >= self.max_turns:
            self.reason = "max_turns"
        if self.reason is None:
            return None
        return StopMessage(content=self.reason, source="agent_hub")

    async def reset(self) -> None:
        self.reason = None
        self._turns = 0
        self._votes.clear()
        self._started_at = self.monotonic()
