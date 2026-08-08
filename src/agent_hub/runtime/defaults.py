"""Default runtime registry used when no model gateway has been configured yet."""

from __future__ import annotations

from collections.abc import AsyncIterator

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry


class UnavailableRuntime:
    """Fail queued runs deterministically instead of leaving them stuck forever."""

    def __init__(self, mode: TaskMode) -> None:
        if mode is TaskMode.AUTO:
            raise ValueError("default runtime mode must be executable")
        self.mode: TaskMode = mode

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED,
            sequence=1,
            run_id=context.run_id,
            reason="runtime_not_configured",
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise RuntimeError("runtime checkpoint unavailable")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        return None


def default_runtime_registry() -> RuntimeRegistry:
    return RuntimeRegistry(
        UnavailableRuntime(mode)
        for mode in (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID)
    )


__all__ = ["UnavailableRuntime", "default_runtime_registry"]
