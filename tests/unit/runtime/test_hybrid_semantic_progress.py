"""Hybrid deadlines must not grow merely because a child spent more tokens."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.streams import closing_runtime_events
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, artifact


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_hybrid_child_semantic_progress_ignores_usage_only_changes(legacy: bool) -> None:
    class Dispatch(MultiArtifactRuntime):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            for index, progress in enumerate((0, 1, 1, 2, 2), 1):
                yield RunEvent(
                    kind=EventKind.CHECKPOINT_SAVED, sequence=index, run_id=context.run_id,
                    checkpoint=RuntimeCheckpoint(
                        id=uuid4(), runtime_type="test_dispatch", runtime_version="1",
                        run_id=context.run_id, tenant_id=context.tenant_id, mode=self.mode,
                        state={"usage": {"tokens": index}, "timeout_progress_units": progress,
                               **({"workspace_meaningful_progress": True} if not legacy else {})},
                    ),
                )
            yield RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=6, run_id=context.run_id,
                           artifact=artifact("worker", "Completed dispatch"))
            yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=7, run_id=context.run_id)

    runtime = HybridRuntime(
        Dispatch(TaskMode.DISPATCH, ()),
        MultiArtifactRuntime(TaskMode.DISCUSS, (artifact("discussion", "Reviewed"),)),
        MultiArtifactRuntime(TaskMode.DIRECT, (artifact("synthesis", "Completed"),)),
    )
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.HYBRID,
                          request="Build a project", token_budget=1000, timeout_seconds=600)
    events = [event async for event in runtime.run(context)]
    progress = [event.checkpoint.state["timeout_progress_units"] for event in events
                if event.checkpoint is not None
                and event.checkpoint.state.get("child_checkpoint") is not None]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert progress == ([1, 2, 3, 4, 5] if legacy else [0, 1, 1, 2, 2])


@pytest.mark.asyncio
async def test_hybrid_restore_does_not_recredit_same_child_progress() -> None:
    class Dispatch(MultiArtifactRuntime):
        def __init__(self, progress: tuple[int, ...], *, offset: int) -> None:
            super().__init__(TaskMode.DISPATCH, ())
            self.progress = progress
            self.offset = offset
            self.restored: list[RuntimeCheckpoint] = []

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            for index, progress in enumerate(self.progress, self.offset):
                yield RunEvent(
                    kind=EventKind.CHECKPOINT_SAVED, sequence=index, run_id=context.run_id,
                    checkpoint=RuntimeCheckpoint(
                        id=uuid4(), runtime_type="test_dispatch", runtime_version="1",
                        run_id=context.run_id, tenant_id=context.tenant_id, mode=self.mode,
                        state={"usage": {"tokens": index}, "timeout_progress_units": progress,
                               "workspace_meaningful_progress": True},
                    ),
                )
            yield RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=10, run_id=context.run_id,
                           artifact=artifact("worker", "Completed dispatch"))
            yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=11, run_id=context.run_id)

        async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
            self.restored.append(checkpoint)

    def hybrid(dispatch: Dispatch) -> HybridRuntime:
        return HybridRuntime(
            dispatch, MultiArtifactRuntime(TaskMode.DISCUSS, (artifact("discussion", "Reviewed"),)),
            MultiArtifactRuntime(TaskMode.DIRECT, (artifact("synthesis", "Completed"),)),
        )

    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.HYBRID,
                          request="Build a project", token_budget=1000, timeout_seconds=600)
    first = hybrid(Dispatch((1, 1, 2), offset=1))
    async with closing_runtime_events(first.run(context)) as stream:
        event = await anext(stream)
        checkpoint = event.checkpoint
        assert checkpoint is not None
        before = checkpoint.to_payload()
    child = Dispatch((1, 2, 2), offset=2)
    resumed = hybrid(child)
    await resumed.restore_checkpoint(checkpoint)
    events = [item async for item in resumed.run(context.model_copy(update={"checkpoint": checkpoint}))]
    progress = [item.checkpoint.state["timeout_progress_units"] for item in events
                if item.checkpoint is not None
                and item.checkpoint.state.get("child_checkpoint") is not None]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert progress == [1, 2, 2]
    assert len(child.restored) == 1
    assert checkpoint.to_payload() == before
