"""Bind trusted worker identity until its runtime stream has closed."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager, nullcontext
from contextvars import Context, copy_context
from typing import TYPE_CHECKING

from agent_hub.auth.models import Role
from agent_hub.runtime.contracts import ExecutionRuntime, RunEvent, TaskContext, _freeze_object
from agent_hub.runtime.streams import (
    _protect_runtime_stream_owner,
    _register_runtime_stream_owner,
    closing_runtime_events,
)

if TYPE_CHECKING:
    from agent_hub.runtime.review_assembly import ReviewAssemblyBroker


@asynccontextmanager
async def _scoped_events(
    stream: AsyncIterator[RunEvent],
    scope: Context,
    *,
    cancel: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[AsyncIterator[RunEvent]]:
    # One demand produces one advancement. Actual adapter streams bind their
    # consumer Task as well as Context; neither may change between yields.
    type Reply = tuple[RunEvent | None, BaseException | None]
    requests: asyncio.Queue[asyncio.Future[Reply] | None] = asyncio.Queue(maxsize=1)
    pending: asyncio.Future[Reply] | None = None
    advancing = False
    closing = False

    async def consume() -> None:
        nonlocal advancing
        try:
            while True:
                reply = await requests.get()
                if reply is None:
                    return
                if reply.cancelled():
                    continue
                try:
                    advancing = True
                    event = await anext(stream)
                except BaseException as error:  # noqa: BLE001 - transfer iterator/cancellation outcome unchanged.
                    if not reply.done():
                        reply.set_result((None, error))
                    return
                finally:
                    advancing = False
                if not reply.done():
                    reply.set_result((event, None))
                if closing:
                    return
        finally:
            try:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    await close()
            finally:
                if pending is not None and not pending.done():
                    pending.set_result((None, StopAsyncIteration()))

    async def run_owner() -> None:
        # Register inside the owner before its first await, and remove only after
        # consume's awaited aclose (including error cleanup) has really finished.
        with _register_runtime_stream_owner():
            await consume()

    owner = asyncio.create_task(run_owner(), context=scope, name="worker-runtime-owner")

    async def events() -> AsyncIterator[RunEvent]:
        nonlocal pending
        while True:
            if owner.done():
                owner.result()
                return
            pending = asyncio.get_running_loop().create_future()
            requests.put_nowait(pending)
            event, error = await pending
            if isinstance(error, StopAsyncIteration):
                return
            if error is not None:
                raise error
            assert event is not None
            yield event

    try:
        async with closing_runtime_events(events()) as forwarding:
            yield forwarding
    finally:
        # anext can already be inside an awaited natural/error finalizer. Never
        # inject cancellation into its owner. The runtime's own cancellation
        # protocol stops live work; the owner alone advances/closes its stream.
        closing = True
        caller = asyncio.current_task()
        cancellations = 0 if caller is None else caller.cancelling()
        stopping: asyncio.Task[None] | None = None
        if not owner.done():
            if advancing:
                if cancel is not None:

                    async def stop_work() -> None:
                        # Advancement may finish before this callback starts.
                        # In that case the owner closes itself via closing=True.
                        if advancing and not owner.done():
                            with _protect_runtime_stream_owner(owner):
                                await cancel()

                    stopping = asyncio.create_task(
                        stop_work(), context=scope.copy(), name="worker-runtime-cancel"
                    )
            else:
                # Even an unstarted owner must close an eagerly created stream.
                # Dropping an unprocessed demand never advances/prefetches it.
                if not requests.empty():
                    requests.get_nowait()
                requests.put_nowait(None)
        tasks = (owner,) if stopping is None else (owner, stopping)
        drained = asyncio.gather(*tasks, return_exceptions=True)
        while not drained.done():
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError:
                pass
        for task in tasks:
            if not task.cancelled():
                task.result()
        if caller is not None and caller.cancelling() > cancellations:
            raise asyncio.CancelledError


@asynccontextmanager
async def worker_runtime_events(
    runtime: ExecutionRuntime,
    context: TaskContext,
    *,
    worker_id: str | None = None,
    review_broker: ReviewAssemblyBroker | None = None,
) -> AsyncIterator[AsyncIterator[RunEvent]]:
    """Use only the driver's context; legacy streams acquire no verification identity."""
    # runs.__init__ imports RunService; defer this dependency until package initialization ends.
    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.models.physical_budget import _bind_run_budget
    from agent_hub.runtime.review_assembly import ReviewAssemblyUnavailable, _execution

    # Private DB locators/leases are not model context or continuation payloads.
    private = {"project_verification_receipt", "approval_checkpoint_worker_token"}
    if private.intersection(context.routing_decision):
        context = context.model_copy(
            update={
                "routing_decision": _freeze_object(
                    {
                        key: value
                        for key, value in context.routing_decision.items()
                        if key not in private
                    },
                    name="runtime-visible routing",
                )
            }
        )
    binding = (
        bind_execution_authority(context, worker_id=worker_id)
        if worker_id is not None
        and context.execution_token is not None
        and context.actor_id is not None
        and context.actor_role is not None
        and context.actor_role is not Role.VIEWER
        else nullcontext()
    )
    execution = _execution.get()
    owns_budget = execution is None
    if execution is not None:
        original = execution.budget
        if (
            context.tenant_id,
            context.run_id,
            context.execution_token,
            worker_id,
            context.actor_id,
            context.actor_role,
        ) != (
            original.tenant_id,
            original.run_id,
            original.lease.token,
            original.lease.worker_id,
            original.evidence.actor_id,
            original.evidence.actor_role,
        ):
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
            owned_cancel = getattr(runtime, "cancel_run_owned", None)
            cancel = getattr(runtime, "cancel", None)

            async def stop_runtime() -> None:
                if callable(owned_cancel):
                    await owned_cancel(context.run_id, context.execution_token)
                elif callable(cancel):
                    await cancel()

            async with _scoped_events(
                stream,
                scope,
                cancel=stop_runtime if callable(owned_cancel) or callable(cancel) else None,
            ) as events:
                yield events
        finally:
            scope.run(stack.close)
        return
    with binding:
        # Stream finalizers and their awaited children retain identity until close returns.
        async with closing_runtime_events(runtime.run(context)) as stream:
            yield stream
