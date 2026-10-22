"""Bind trusted worker identity until its runtime stream has closed."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext

from agent_hub.auth.models import Role
from agent_hub.runtime.contracts import ExecutionRuntime, RunEvent, TaskContext
from agent_hub.runtime.streams import closing_runtime_events


@asynccontextmanager
async def worker_runtime_events(
    runtime: ExecutionRuntime, context: TaskContext, *, worker_id: str | None = None,
) -> AsyncIterator[AsyncIterator[RunEvent]]:
    """Use only the driver's context; legacy streams acquire no verification identity."""
    # runs.__init__ imports RunService; defer this dependency until package initialization ends.
    from agent_hub.capabilities.execution_authority import bind_execution_authority

    binding = (
        bind_execution_authority(context, worker_id=worker_id)
        if worker_id is not None and context.execution_token is not None
        and context.actor_id is not None and context.actor_role is not None
        and context.actor_role is not Role.VIEWER
        else nullcontext()
    )
    with binding:
        # Stream finalizers and their awaited children retain identity until close returns.
        async with closing_runtime_events(runtime.run(context)) as stream:
            yield stream
