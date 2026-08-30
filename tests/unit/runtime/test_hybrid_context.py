from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.hybrid import HybridRuntime


class RecordingRuntime:
    def __init__(self, mode: TaskMode, output: Artifact) -> None:
        self.mode = mode
        self.output = output
        self.contexts: list[TaskContext] = []

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.contexts.append(context)

        async def generate() -> AsyncIterator[RunEvent]:
            yield RunEvent(
                kind=EventKind.ARTIFACT_CREATED,
                sequence=1,
                run_id=context.run_id,
                artifact=self.output,
            )
            yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=2, run_id=context.run_id)

        return generate()

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError(f"not used: {checkpoint.id}")

    async def cancel(self) -> None:
        return None


def artifact(producer: str, text: str) -> Artifact:
    return Artifact(
        id=uuid4(),
        type="text",
        producer=producer,
        content={"text": text},
    )


async def test_hybrid_child_context_preserves_actor_identity_and_routing_decision() -> None:
    actor_id = UUID("33333333-3333-4333-8333-333333333333")
    routing: dict[str, JsonValue] = {
        "source": "trusted-api",
        "selected_agent_ids": ("researcher",),
    }
    dispatch = RecordingRuntime(TaskMode.DISPATCH, artifact("researcher", "evidence"))
    discussion = RecordingRuntime(TaskMode.DISCUSS, artifact("critic", "review"))
    synthesis = RecordingRuntime(TaskMode.DIRECT, artifact("main", "answer"))
    runtime = HybridRuntime(dispatch, discussion, synthesis)

    _ = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                actor_id=actor_id,
                actor_role=Role.OPERATOR,
                mode=TaskMode.HYBRID,
                request="Resolve the question.",
                routing_decision=routing,
            )
        )
    ]

    for child in (dispatch.contexts[0], discussion.contexts[0], synthesis.contexts[0]):
        assert child.actor_id == actor_id
        assert child.actor_role is Role.OPERATOR
        assert child.routing_decision == routing
        assert child.checkpoint is None
