"""Live, bounded token allowances without changing validated execution contexts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeGuard

from agent_hub.runtime.contracts import RunEvent, TaskContext
from agent_hub.runtime.streams import closing_runtime_events

_MAX_TOKENS = 10_000_000


class TokenBudgetExhausted(ValueError):
    def __init__(self) -> None:
        super().__init__("runtime token budget exhausted")


def _legal_tokens(value: object) -> TypeGuard[int]:
    return type(value) is int and 1 <= value <= _MAX_TOKENS


@dataclass(frozen=True, slots=True)
class TokenBudgetSource:
    """Observe one verified source; descendants subtract already accounted usage."""

    ceiling: int
    source: TaskContext | None = None
    parent: TokenBudgetSource | None = None
    spent: int = 0
    checkpoint_baseline: int = 0
    identity: tuple[object, ...] = ()

    @classmethod
    def from_context(
        cls, source: TaskContext, *, validated: TaskContext | None = None,
    ) -> TokenBudgetSource:
        binding = _TOKEN_BUDGET.get()
        if binding is not None and binding[0] is source:
            return binding[1]
        snapshot = validated if validated is not None else source.validated_internal_clone()
        decision = snapshot.routing_decision
        soft = decision.get("runtime_token_soft_base_tokens")
        absolute = decision.get("runtime_token_absolute_tokens")
        plan = decision.get("runtime_plan_token_budget")
        scale = decision.get("project_scale")
        ceiling = snapshot.token_budget
        if (
            type(scale) is str and scale in {"small", "medium", "large", "ultra"}
            and _legal_tokens(soft) and _legal_tokens(absolute) and _legal_tokens(plan)
        ):
            ceiling = min(absolute, plan)
        if _legal_tokens(absolute):
            ceiling = min(ceiling, absolute)
        if _legal_tokens(plan):
            ceiling = min(ceiling, plan)
        return cls(
            ceiling=ceiling, source=source,
            identity=(snapshot.run_id, snapshot.tenant_id, snapshot.actor_id, snapshot.mode),
        )

    @property
    def limit(self) -> int:
        if self.parent is not None:
            remaining = max(0, self.parent.limit - self.spent)
            if remaining == 0:
                return 0
            return min(
                self.ceiling,
                remaining + self.checkpoint_baseline,
            )
        if self.source is None:
            return 0
        if (
            self.source.run_id, self.source.tenant_id, self.source.actor_id, self.source.mode,
        ) != self.identity:
            return 0
        value = self.source.token_budget
        if not _legal_tokens(value):
            return 0
        return min(value, self.ceiling)

    def remaining(self, *, spent: int, checkpoint_baseline: int = 0) -> TokenBudgetSource:
        if type(spent) is not int or spent < 0:
            raise ValueError("accounted token usage is invalid")
        if type(checkpoint_baseline) is not int or checkpoint_baseline < 0:
            raise ValueError("checkpoint token baseline is invalid")
        return TokenBudgetSource(
            ceiling=_MAX_TOKENS, parent=self, spent=spent,
            checkpoint_baseline=checkpoint_baseline,
        )


_TOKEN_BUDGET: ContextVar[tuple[TaskContext, TokenBudgetSource] | None] = ContextVar(
    "runtime_token_budget", default=None,
)


@contextmanager
def token_budget_scope(context: TaskContext, budget: TokenBudgetSource) -> Iterator[None]:
    token = _TOKEN_BUDGET.set((context, budget))
    try:
        yield
    finally:
        _TOKEN_BUDGET.reset(token)


def current_token_budget(context: TaskContext) -> int:
    binding = _TOKEN_BUDGET.get()
    if binding is not None and binding[0] is context:
        return binding[1].limit
    return context.token_budget


def rebind_token_budget(source: TaskContext, validated_clone: TaskContext) -> None:
    """A coordinator's verified hydration clone retains its task-local allowance."""
    binding = _TOKEN_BUDGET.get()
    if binding is not None and binding[0] is source:
        _TOKEN_BUDGET.set((validated_clone, binding[1]))


async def token_budget_events(
    factory: Callable[[], AsyncIterator[RunEvent]],
    context: TaskContext,
    budget: TokenBudgetSource,
) -> AsyncIterator[RunEvent]:
    """Bind only while driving a child, never across a yield to its caller."""
    with token_budget_scope(context, budget):
        _sync_context_token_budget(context, budget)
        stream = factory()
    async with closing_runtime_events(stream) as events:
        while True:
            with token_budget_scope(context, budget):
                _sync_context_token_budget(context, budget)
                try:
                    event = await anext(events)
                except StopAsyncIteration:
                    return
            yield event


def _sync_context_token_budget(context: TaskContext, budget: TokenBudgetSource) -> None:
    limit = budget.limit
    if limit <= 0:
        raise TokenBudgetExhausted()
    object.__setattr__(context, "token_budget", limit)
