from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.hybrid import HybridRuntime


class FailingRuntime:
    def __init__(self, mode: TaskMode, reason: str) -> None:
        self.mode = mode
        self._reason = reason

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED,
            sequence=1,
            run_id=context.run_id,
            reason=self._reason,
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError(f"not used: {checkpoint.id}")

    async def cancel(self) -> None:
        return None


class UnusedRuntime(FailingRuntime):
    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        raise AssertionError(f"{self.mode.value} should not run for this test")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_hybrid_runtime_preserves_dispatch_child_failure_reason() -> None:
    run_id = uuid4()
    runtime = HybridRuntime(
        FailingRuntime(TaskMode.DISPATCH, "model gateway failed"),
        UnusedRuntime(TaskMode.DISCUSS, "unused"),
        UnusedRuntime(TaskMode.DIRECT, "unused"),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=run_id,
                tenant_id=uuid4(),
                mode=TaskMode.HYBRID,
                request="build a page",
            )
        )
    ]

    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "hybrid dispatch failed: model gateway failed"


@pytest.mark.asyncio
async def test_hybrid_runtime_redacts_sensitive_child_failure_reason() -> None:
    run_id = uuid4()
    runtime = HybridRuntime(
        FailingRuntime(TaskMode.DISPATCH, "Authorization Bearer sk-secret failed"),
        UnusedRuntime(TaskMode.DISCUSS, "unused"),
        UnusedRuntime(TaskMode.DIRECT, "unused"),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=run_id,
                tenant_id=uuid4(),
                mode=TaskMode.HYBRID,
                request="build a page",
            )
        )
    ]

    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "hybrid_failed"
