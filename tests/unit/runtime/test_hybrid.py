from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.hybrid import HybridRuntime


class MultiArtifactRuntime:
    def __init__(self, mode: TaskMode, outputs: tuple[Artifact, ...]) -> None:
        self.mode = mode
        self.outputs = outputs
        self.contexts: list[TaskContext] = []

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.contexts.append(context)
        sequence = 1
        for output in self.outputs:
            yield RunEvent(
                kind=EventKind.ARTIFACT_CREATED,
                sequence=sequence,
                run_id=context.run_id,
                artifact=output,
            )
            sequence += 1
        yield RunEvent(
            kind=EventKind.RUNTIME_COMPLETED,
            sequence=sequence,
            run_id=context.run_id,
            reason="explicit_completion",
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError(f"not used: {checkpoint.id}")

    async def cancel(self) -> None:
        return None


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


class RecordingArtifactRuntime(MultiArtifactRuntime):
    def __init__(self, mode: TaskMode, output: Artifact) -> None:
        super().__init__(mode, (output,))


def artifact(
    producer: str,
    text: str,
    *,
    artifact_type: str = "text",
    sources: tuple[str, ...] = (),
) -> Artifact:
    return Artifact(
        id=uuid4(),
        type=artifact_type,
        producer=producer,
        content={"text": text},
        source_ids=sources,
    )


@pytest.mark.asyncio
async def test_hybrid_discussion_handoff_drops_wrapped_model_response_duplicates() -> None:
    model_output = artifact("writer", "draft", artifact_type="model_response")
    text_output = artifact("writer", "draft", sources=(str(model_output.id),))
    discussion_output = artifact("critic", "review", sources=(str(text_output.id),))
    final_output = artifact("main", "answer", sources=(str(discussion_output.id),))
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, (model_output, text_output))
    discussion = RecordingArtifactRuntime(TaskMode.DISCUSS, discussion_output)
    synthesis = RecordingArtifactRuntime(TaskMode.DIRECT, final_output)
    runtime = HybridRuntime(dispatch, discussion, synthesis)

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.HYBRID,
                request="write a slogan",
            )
        )
    ]

    assert discussion.contexts[0].artifacts == (text_output,)
    started = next(event for event in events if event.kind is EventKind.DISCUSSION_STARTED)
    assert started.inputs == (text_output,)


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
async def test_hybrid_runtime_completes_partial_when_discussion_gateway_fails_after_dispatch() -> None:
    run_id = uuid4()
    dispatch_output = artifact("planner", "dispatch result")
    runtime = HybridRuntime(
        MultiArtifactRuntime(TaskMode.DISPATCH, (dispatch_output,)),
        FailingRuntime(TaskMode.DISCUSS, "model gateway failed: model transport failed"),
        UnusedRuntime(TaskMode.DIRECT, "unused"),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=run_id,
                tenant_id=uuid4(),
                mode=TaskMode.HYBRID,
                request="build a plan",
            )
        )
    ]

    assert any(
        event.kind is EventKind.ARTIFACT_CREATED and event.artifact == dispatch_output
        for event in events
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason == "partial_hybrid_after_discussion_failure"


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
