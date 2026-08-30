from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.hybrid import HybridPlan, HybridRuntime, HybridUpgrade


class RecordingRuntime:
    def __init__(self, mode: TaskMode, output: Artifact) -> None:
        self.mode = mode
        self.output = output
        self.contexts: list[TaskContext] = []
        self.cancelled = False

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.contexts.append(context)

        async def generate() -> AsyncIterator[RunEvent]:
            yield RunEvent(
                kind=EventKind.ARTIFACT_CREATED,
                sequence=1,
                run_id=context.run_id,
                artifact=self.output,
            )
            yield RunEvent(
                kind=EventKind.RUNTIME_COMPLETED,
                sequence=2,
                run_id=context.run_id,
                reason="explicit_completion",
            )

        return generate()

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise RuntimeError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise RuntimeError("not used")

    async def cancel(self) -> None:
        self.cancelled = True


def artifact(
    producer: str,
    text: str,
    *,
    sources: tuple[str, ...] = (),
) -> Artifact:
    return Artifact(
        id=uuid4(),
        type="text",
        producer=producer,
        content={"text": text},
        source_ids=sources,
    )


def context(inputs: tuple[Artifact, ...] = (), **changes: object) -> TaskContext:
    payload = {
        "run_id": uuid4(),
        "tenant_id": uuid4(),
        "mode": TaskMode.HYBRID,
        "request": "Resolve the question.",
        "artifacts": inputs,
        **changes,
    }
    return TaskContext.model_validate(payload)


async def collect(runtime: HybridRuntime, ctx: TaskContext) -> list[Any]:
    return [event async for event in runtime.run(ctx)]


async def test_hybrid_passes_only_artifacts_between_fresh_framework_contexts() -> None:
    dispatch_output = artifact("researcher", "evidence")
    discussion_output = artifact("critic", "review", sources=(str(dispatch_output.id),))
    final_output = artifact("main", "answer", sources=(str(discussion_output.id),))
    dispatch = RecordingRuntime(TaskMode.DISPATCH, dispatch_output)
    discussion = RecordingRuntime(TaskMode.DISCUSS, discussion_output)
    synthesis = RecordingRuntime(TaskMode.DIRECT, final_output)
    runtime = HybridRuntime(dispatch, discussion, synthesis)

    events = await collect(runtime, context())

    assert discussion.contexts[0].artifacts == (dispatch_output,)
    assert discussion.contexts[0].checkpoint is None
    assert synthesis.contexts[0].artifacts == (dispatch_output, discussion_output)
    assert synthesis.contexts[0].checkpoint is None
    started = next(event for event in events if event.kind is EventKind.DISCUSSION_STARTED)
    assert started.inputs == (dispatch_output,)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_hybrid_child_context_preserves_actor_identity_and_routing_decision() -> None:
    actor_id = uuid4()
    routing: dict[str, JsonValue] = {
        "source": "trusted-api",
        "selected_agent_ids": ("researcher",),
    }
    dispatch_output = artifact("researcher", "evidence")
    discussion_output = artifact("critic", "review", sources=(str(dispatch_output.id),))
    final_output = artifact("main", "answer", sources=(str(discussion_output.id),))
    dispatch = RecordingRuntime(TaskMode.DISPATCH, dispatch_output)
    discussion = RecordingRuntime(TaskMode.DISCUSS, discussion_output)
    synthesis = RecordingRuntime(TaskMode.DIRECT, final_output)
    runtime = HybridRuntime(dispatch, discussion, synthesis)

    await collect(
        runtime,
        context(actor_id=actor_id, actor_role=Role.OPERATOR, routing_decision=routing),
    )

    child_contexts = (
        dispatch.contexts[0],
        discussion.contexts[0],
        synthesis.contexts[0],
    )
    for child in child_contexts:
        assert child.actor_id == actor_id
        assert child.actor_role is Role.OPERATOR
        assert child.routing_decision == routing
        assert child.checkpoint is None


async def test_dispatch_to_hybrid_upgrade_does_not_repeat_existing_artifacts() -> None:
    existing = artifact("researcher", "already complete")
    dispatch = RecordingRuntime(TaskMode.DISPATCH, artifact("researcher", "duplicate"))
    discussion = RecordingRuntime(TaskMode.DISCUSS, artifact("critic", "review"))
    synthesis = RecordingRuntime(TaskMode.DIRECT, artifact("main", "answer"))
    runtime = HybridRuntime(
        dispatch,
        discussion,
        synthesis,
        plan=HybridPlan(upgrade=HybridUpgrade.DISPATCH_TO_HYBRID),
    )

    await collect(runtime, context((existing,)))

    assert dispatch.contexts == []
    assert discussion.contexts[0].artifacts == (existing,)


async def test_direct_to_dispatch_and_discuss_dispatch_discuss_upgrades() -> None:
    existing = artifact("main", "direct result")
    dispatch = RecordingRuntime(TaskMode.DISPATCH, artifact("researcher", "evidence"))
    discussion = RecordingRuntime(TaskMode.DISCUSS, artifact("critic", "review"))
    synthesis = RecordingRuntime(TaskMode.DIRECT, artifact("main", "answer"))
    direct_upgrade = HybridRuntime(
        dispatch,
        discussion,
        synthesis,
        plan=HybridPlan(upgrade=HybridUpgrade.DIRECT_TO_DISPATCH),
    )
    await collect(direct_upgrade, context((existing,)))
    assert dispatch.contexts[0].artifacts == (existing,)

    dispatch2 = RecordingRuntime(TaskMode.DISPATCH, artifact("researcher", "new evidence"))
    discussion2 = RecordingRuntime(TaskMode.DISCUSS, artifact("critic", "review"))
    synthesis2 = RecordingRuntime(TaskMode.DIRECT, artifact("main", "answer"))
    loop = HybridRuntime(
        dispatch2,
        discussion2,
        synthesis2,
        plan=HybridPlan(upgrade=HybridUpgrade.DISCUSS_DISPATCH_DISCUSS),
    )
    await collect(loop, context())
    assert len(discussion2.contexts) == 2
    assert discussion2.contexts[1].artifacts[-1] == dispatch2.output


async def test_hybrid_resumes_after_dispatch_without_repeating_completed_stage() -> None:
    repository = InMemoryArtifactRepository()
    dispatch_output = artifact("researcher", "durable evidence")
    ctx = context()
    first_dispatch = RecordingRuntime(TaskMode.DISPATCH, dispatch_output)
    first = HybridRuntime(
        first_dispatch,
        RecordingRuntime(TaskMode.DISCUSS, artifact("critic", "unused")),
        RecordingRuntime(TaskMode.DIRECT, artifact("main", "unused")),
        artifact_repository=repository,
    )
    stream = first.run(ctx)
    checkpoint = None
    while checkpoint is None:
        event = await anext(stream)
        if event.kind is EventKind.CHECKPOINT_SAVED:
            checkpoint = event.checkpoint
    await cast(Any, stream).aclose()
    assert checkpoint is not None

    resumed_dispatch = RecordingRuntime(TaskMode.DISPATCH, artifact("researcher", "duplicate"))
    resumed_discussion = RecordingRuntime(TaskMode.DISCUSS, artifact("critic", "review"))
    resumed = HybridRuntime(
        resumed_dispatch,
        resumed_discussion,
        RecordingRuntime(TaskMode.DIRECT, artifact("main", "answer")),
        artifact_repository=repository,
    )
    await resumed.restore_checkpoint(checkpoint)
    resumed_context = TaskContext(
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        mode=ctx.mode,
        request=ctx.request,
        checkpoint=checkpoint,
    )
    events = await collect(resumed, resumed_context)

    assert resumed_dispatch.contexts == []
    assert resumed_discussion.contexts[0].artifacts == (dispatch_output,)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_hybrid_cancel_propagates_to_child_and_emits_one_terminal() -> None:
    class BlockingRuntime(RecordingRuntime):
        def __init__(self) -> None:
            super().__init__(TaskMode.DISPATCH, artifact("researcher", "unused"))
            self.started = asyncio.Event()

        def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            self.contexts.append(context)

            async def generate() -> AsyncIterator[RunEvent]:
                self.started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
                yield

            return generate()

    dispatch = BlockingRuntime()
    runtime = HybridRuntime(
        dispatch,
        RecordingRuntime(TaskMode.DISCUSS, artifact("critic", "review")),
        RecordingRuntime(TaskMode.DIRECT, artifact("main", "answer")),
    )
    stream = runtime.run(context())
    pending = asyncio.ensure_future(anext(stream))
    await dispatch.started.wait()
    await runtime.cancel()
    terminal = await pending
    assert dispatch.cancelled
    assert terminal.kind is EventKind.RUNTIME_CANCELLED
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)


@pytest.mark.parametrize("resume_after_stage", [2, 3])
async def test_hybrid_resumes_discussion_or_synthesis_boundary_without_repeating_children(
    resume_after_stage: int,
) -> None:
    repository = InMemoryArtifactRepository()
    ctx = context()
    first = HybridRuntime(
        RecordingRuntime(TaskMode.DISPATCH, artifact("researcher", "evidence")),
        RecordingRuntime(TaskMode.DISCUSS, artifact("critic", "review")),
        RecordingRuntime(TaskMode.DIRECT, artifact("main", "answer")),
        artifact_repository=repository,
    )
    stream = first.run(ctx)
    checkpoint = None
    while checkpoint is None:
        event = await anext(stream)
        if (
            event.kind is EventKind.CHECKPOINT_SAVED
            and event.checkpoint is not None
            and event.checkpoint.state["next_stage"] == resume_after_stage
        ):
            checkpoint = event.checkpoint
    await cast(Any, stream).aclose()

    dispatch = RecordingRuntime(TaskMode.DISPATCH, artifact("researcher", "duplicate"))
    discussion = RecordingRuntime(TaskMode.DISCUSS, artifact("critic", "duplicate"))
    synthesis = RecordingRuntime(TaskMode.DIRECT, artifact("main", "resumed answer"))
    resumed = HybridRuntime(
        dispatch,
        discussion,
        synthesis,
        artifact_repository=repository,
    )
    await resumed.restore_checkpoint(checkpoint)
    events = await collect(
        resumed,
        TaskContext(
            run_id=ctx.run_id,
            tenant_id=ctx.tenant_id,
            mode=ctx.mode,
            request=ctx.request,
            checkpoint=checkpoint,
        ),
    )

    assert dispatch.contexts == []
    assert discussion.contexts == []
    assert len(synthesis.contexts) == (1 if resume_after_stage == 2 else 0)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_hybrid_child_failure_emits_exactly_one_failed_terminal() -> None:
    class FailingRuntime(RecordingRuntime):
        def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            self.contexts.append(context)

            async def generate() -> AsyncIterator[RunEvent]:
                yield RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=1,
                    run_id=context.run_id,
                    reason="child_failed",
                )

            return generate()

    runtime = HybridRuntime(
        FailingRuntime(TaskMode.DISPATCH, artifact("researcher", "unused")),
        RecordingRuntime(TaskMode.DISCUSS, artifact("critic", "review")),
        RecordingRuntime(TaskMode.DIRECT, artifact("main", "answer")),
    )
    events = await collect(runtime, context())
    terminals = [
        event
        for event in events
        if event.kind
        in {
            EventKind.RUNTIME_COMPLETED,
            EventKind.RUNTIME_FAILED,
            EventKind.RUNTIME_CANCELLED,
        }
    ]
    assert len(terminals) == 1
    assert terminals[0].kind is EventKind.RUNTIME_FAILED


async def test_hybrid_stream_backpressure_defers_child_start_until_demand() -> None:
    dispatch = RecordingRuntime(TaskMode.DISPATCH, artifact("researcher", "evidence"))
    runtime = HybridRuntime(
        dispatch,
        RecordingRuntime(TaskMode.DISCUSS, artifact("critic", "review")),
        RecordingRuntime(TaskMode.DIRECT, artifact("main", "answer")),
    )
    stream = runtime.run(context())
    await asyncio.sleep(0)
    assert dispatch.contexts == []
    assert (await anext(stream)).kind is EventKind.ARTIFACT_CREATED
    assert len(dispatch.contexts) == 1
    await cast(Any, stream).aclose()
