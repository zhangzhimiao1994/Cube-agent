"""Bind trusted worker identity until its runtime stream has closed."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import ExitStack, asynccontextmanager, nullcontext
from contextvars import Context, copy_context
from typing import TYPE_CHECKING

from agent_hub.auth.models import Role
from agent_hub.runtime.contracts import ExecutionRuntime, RunEvent, TaskContext, _freeze_object
from agent_hub.runtime.streams import closing_runtime_events

if TYPE_CHECKING:
    from agent_hub.runtime.review_assembly import ReviewAssemblyBroker

async def _in_context[T](context: Context, awaitable: Awaitable[T]) -> T:
    async def receive() -> T:
        return await awaitable
    # Sequential advancements reuse the EXACT Context: runtime-owned tokens
    # spanning yields must be reset in their creation Context at finalization.
    return await asyncio.create_task(receive(), context=context)


async def _scoped_events(stream: AsyncIterator[RunEvent], scope: Context) -> AsyncIterator[RunEvent]:
    try:
        while True:
            try:
                event = await _in_context(scope, anext(stream))
            except StopAsyncIteration:
                return
            yield event
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            await _in_context(scope, close())


@asynccontextmanager
async def worker_runtime_events(
    runtime: ExecutionRuntime, context: TaskContext, *, worker_id: str | None = None,
    review_broker: ReviewAssemblyBroker | None = None,
) -> AsyncIterator[AsyncIterator[RunEvent]]:
    """Use only the driver's context; legacy streams acquire no verification identity."""
    # runs.__init__ imports RunService; defer this dependency until package initialization ends.
    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.models.physical_budget import _bind_run_budget
    from agent_hub.runtime.review_assembly import ReviewAssemblyUnavailable, _execution

    # Private DB locators/leases are not model context or continuation payloads.
    private = {'project_verification_receipt', 'approval_checkpoint_worker_token'}
    if private.intersection(context.routing_decision):
        context = context.model_copy(update={'routing_decision': _freeze_object(
            {key: value for key, value in context.routing_decision.items() if key not in private},
            name='runtime-visible routing',
        )})
    binding = (
        bind_execution_authority(context, worker_id=worker_id)
        if worker_id is not None and context.execution_token is not None
        and context.actor_id is not None and context.actor_role is not None
        and context.actor_role is not Role.VIEWER
        else nullcontext()
    )
    execution = _execution.get()
    owns_budget = execution is None
    if execution is not None:
        original = execution.budget
        if ((context.tenant_id, context.run_id, context.execution_token, worker_id,
             context.actor_id, context.actor_role) !=
                (original.tenant_id, original.run_id, original.lease.token, original.lease.worker_id,
                 original.evidence.actor_id, original.evidence.actor_role)):
            raise ReviewAssemblyUnavailable("derived_run_pool_continuity_unsupported")
        original.check()
    elif review_broker is not None and worker_id is not None:
        execution = await review_broker.prepare(context, worker_id=worker_id)
    elif "approval_review_profile" in context.routing_decision:
        raise ReviewAssemblyUnavailable("review_broker_missing")
    if execution is not None:
        # Enter the actual owner bind once in an isolated Context, restore that
        # context for construction, every lazy advancement and finalization.
        # No privileged state leaks to the consumer while it handles a yield.
        scope, stack = copy_context(), ExitStack()

        def enter() -> AsyncIterator[RunEvent]:
            stack.enter_context(binding)
            if owns_budget:
                stack.enter_context(_bind_run_budget(execution.budget))
            _execution.set(execution)
            return runtime.run(context)

        try:
            stream = scope.run(enter)
            async with closing_runtime_events(_scoped_events(stream, scope)) as events:
                yield events
        finally:
            scope.run(stack.close)
        return
    with binding:
        # Stream finalizers and their awaited children retain identity until close returns.
        async with closing_runtime_events(runtime.run(context)) as stream:
            yield stream
