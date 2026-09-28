from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult, JsonValue
from agent_hub.runtime import hybrid as hybrid_module
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.project_scale_artifact import ProjectScaleArtifactPreseedRuntime


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


class UsageRecordingRuntime(MultiArtifactRuntime):
    def __init__(self, mode: TaskMode, output: Artifact, *, tokens_used: int) -> None:
        super().__init__(mode, (output,))
        self.tokens_used = tokens_used

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.contexts.append(context)
        yield RunEvent(
            kind=EventKind.ARTIFACT_CREATED,
            sequence=1,
            run_id=context.run_id,
            artifact=self.outputs[0],
        )
        yield RunEvent(
            kind=EventKind.CHECKPOINT_SAVED,
            sequence=2,
            run_id=context.run_id,
            checkpoint=RuntimeCheckpoint(
                id=uuid4(),
                runtime_type=f"test_{self.mode.value}",
                runtime_version="1",
                run_id=context.run_id,
                tenant_id=context.tenant_id,
                mode=self.mode,
                state={"usage": {"tokens": self.tokens_used}},
            ),
        )
        yield RunEvent(
            kind=EventKind.RUNTIME_COMPLETED,
            sequence=3,
            run_id=context.run_id,
            reason="explicit_completion",
        )


class AdvancingUsageRuntime(UsageRecordingRuntime):
    def __init__(
        self,
        mode: TaskMode,
        output: Artifact,
        *,
        tokens_used: int,
        clock: list[float],
        elapsed_seconds: float,
    ) -> None:
        super().__init__(mode, output, tokens_used=tokens_used)
        self.clock = clock
        self.elapsed_seconds = elapsed_seconds

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        async for event in super().run(context):
            yield event
        self.clock[0] += self.elapsed_seconds


class ResumableCheckpointRuntime:
    def __init__(
        self,
        mode: TaskMode,
        *,
        initial_tokens: int,
        resumed_tokens: int,
        clock: list[float],
        initial_elapsed_seconds: float,
        resumed_elapsed_seconds: float,
    ) -> None:
        self.mode = mode
        self.initial_tokens = initial_tokens
        self.resumed_tokens = resumed_tokens
        self.clock = clock
        self.initial_elapsed_seconds = initial_elapsed_seconds
        self.resumed_elapsed_seconds = resumed_elapsed_seconds
        self.contexts: list[TaskContext] = []
        self.restored_checkpoints: list[RuntimeCheckpoint] = []

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.contexts.append(context)
        resumed = bool(self.restored_checkpoints)
        self.clock[0] += (
            self.resumed_elapsed_seconds if resumed else self.initial_elapsed_seconds
        )
        tokens = self.resumed_tokens if resumed else self.initial_tokens
        checkpoint = RuntimeCheckpoint(
            id=uuid4(),
            runtime_type=f"test_{self.mode.value}",
            runtime_version="1",
            run_id=context.run_id,
            tenant_id=context.tenant_id,
            mode=self.mode,
            state={"usage": {"tokens": tokens}},
        )
        yield RunEvent(
            kind=EventKind.CHECKPOINT_SAVED,
            sequence=1,
            run_id=context.run_id,
            checkpoint=checkpoint,
        )
        yield RunEvent(
            kind=EventKind.RUNTIME_COMPLETED,
            sequence=2,
            run_id=context.run_id,
            reason="explicit_completion",
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        self.restored_checkpoints.append(checkpoint)

    async def cancel(self) -> None:
        return None


class ProcessRuntime:
    def __init__(self, mode: TaskMode, output: Artifact) -> None:
        self.mode = mode
        self.output = output

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.STEP_STARTED,
            sequence=1,
            run_id=context.run_id,
            actor="planner",
            step_id="planner_step",
            payload={"task": "Plan the work.", "logical_model": "main"},
        )
        yield RunEvent(
            kind=EventKind.MODEL_STARTED,
            sequence=2,
            run_id=context.run_id,
            actor="planner",
            payload={"logical_model": "main", "task": "Plan the work."},
        )
        yield RunEvent(
            kind=EventKind.MESSAGE_CREATED,
            sequence=3,
            run_id=context.run_id,
            actor="planner",
            session_id=str(context.run_id),
            message="Planner received the work.",
        )
        yield RunEvent(
            kind=EventKind.ARTIFACT_CREATED,
            sequence=4,
            run_id=context.run_id,
            actor="planner",
            artifact=self.output,
            payload={"artifact_id": str(self.output.id), "output": "dispatch result"},
        )
        yield RunEvent(
            kind=EventKind.RUNTIME_COMPLETED,
            sequence=5,
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


class RecordingHarnessToolGateway:
    def __init__(self) -> None:
        self.calls: list[HarnessToolCallRequest] = []
        self.user_ids: list[object] = []
        self.roles: list[object] = []
        self.artifact_id = str(uuid4())

    async def invoke(
        self,
        tenant_id: object,
        request: HarnessToolCallRequest,
        *,
        user_id: object = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        del tenant_id
        self.calls.append(request)
        self.user_ids.append(user_id)
        self.roles.append(role)
        raw_files = request.arguments.get("files")
        workspace_files: tuple[Mapping[str, JsonValue], ...] = ()
        if isinstance(raw_files, Mapping):
            workspace_files = tuple(
                {"path": path}
                for path in sorted(raw_files)
                if isinstance(path, str)
            )
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="succeeded",
            payload={
                "artifact_id": self.artifact_id,
                "file": {
                    "artifact_id": self.artifact_id,
                    "filename": "project-scale-artifact-production.zip",
                    "mime_type": "application/zip",
                    "size_bytes": 2048,
                    "sha256": "0" * 64,
                    "download_url": f"/api/v1/runs/{request.run_id}/artifacts/{self.artifact_id}/download",
                },
                "presentation": "final_attachment",
                "summary": "Generated project ZIP artifact.",
                "workspace_files": workspace_files,
            },
        )


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


def final_zip_artifact() -> Artifact:
    artifact_id = str(uuid4())
    return Artifact(
        id=uuid4(),
        type="tool_result",
        producer="implementer",
        content={
            "result": {
                "artifact_id": artifact_id,
                "file": {
                    "artifact_id": artifact_id,
                    "filename": "main.py.zip",
                    "mime_type": "application/zip",
                    "download_url": f"/api/v1/runs/{uuid4()}/artifacts/{artifact_id}/download",
                },
                "presentation": "final_attachment",
            }
        },
    )


@pytest.mark.asyncio
async def test_hybrid_stages_share_remaining_parent_token_budget() -> None:
    dispatch = UsageRecordingRuntime(
        TaskMode.DISPATCH,
        artifact("researcher", "evidence"),
        tokens_used=30,
    )
    discussion = UsageRecordingRuntime(
        TaskMode.DISCUSS,
        artifact("critic", "review"),
        tokens_used=40,
    )
    synthesis = UsageRecordingRuntime(
        TaskMode.DIRECT,
        artifact("main", "answer"),
        tokens_used=20,
    )
    runtime = HybridRuntime(dispatch, discussion, synthesis)
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.HYBRID,
        request="Resolve the question.",
        token_budget=120,
    )

    events = [event async for event in runtime.run(context)]

    assert [
        dispatch.contexts[0].token_budget,
        discussion.contexts[0].token_budget,
        synthesis.contexts[0].token_budget,
    ] == [120, 90, 50]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.asyncio
async def test_hybrid_stages_share_parent_timeout_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(hybrid_module, "monotonic", lambda: clock[0], raising=False)
    dispatch = AdvancingUsageRuntime(
        TaskMode.DISPATCH,
        artifact("researcher", "evidence"),
        tokens_used=1,
        clock=clock,
        elapsed_seconds=10.0,
    )
    discussion = AdvancingUsageRuntime(
        TaskMode.DISCUSS,
        artifact("critic", "review"),
        tokens_used=1,
        clock=clock,
        elapsed_seconds=20.0,
    )
    synthesis = AdvancingUsageRuntime(
        TaskMode.DIRECT,
        artifact("main", "answer"),
        tokens_used=1,
        clock=clock,
        elapsed_seconds=30.0,
    )
    runtime = HybridRuntime(dispatch, discussion, synthesis)
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.HYBRID,
        request="Resolve the question.",
        timeout_seconds=90.0,
    )

    events = [event async for event in runtime.run(context)]

    assert [
        dispatch.contexts[0].timeout_seconds,
        discussion.contexts[0].timeout_seconds,
        synthesis.contexts[0].timeout_seconds,
    ] == [90.0, 80.0, 60.0]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.asyncio
async def test_hybrid_wraps_and_resumes_child_checkpoint_with_remaining_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(hybrid_module, "monotonic", lambda: clock[0], raising=False)
    initial_dispatch = ResumableCheckpointRuntime(
        TaskMode.DISPATCH,
        initial_tokens=30,
        resumed_tokens=30,
        clock=clock,
        initial_elapsed_seconds=12.0,
        resumed_elapsed_seconds=0.0,
    )
    initial_runtime = HybridRuntime(
        initial_dispatch,
        UsageRecordingRuntime(
            TaskMode.DISCUSS,
            artifact("critic", "initial review"),
            tokens_used=5,
        ),
        UsageRecordingRuntime(
            TaskMode.DIRECT,
            artifact("main", "initial answer"),
            tokens_used=5,
        ),
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.HYBRID,
        request="Resolve the question.",
        token_budget=100,
        timeout_seconds=90.0,
    )

    initial_events = [event async for event in initial_runtime.run(context)]
    in_stage_checkpoints = [
        event.checkpoint
        for event in initial_events
        if event.kind is EventKind.CHECKPOINT_SAVED
        and event.checkpoint is not None
        and event.checkpoint.state["next_stage"] == 0
    ]

    assert len(in_stage_checkpoints) == 1
    outer_checkpoint = in_stage_checkpoints[0]
    outer_payload = outer_checkpoint.to_payload()
    outer_state = outer_payload["state"]
    assert isinstance(outer_state, Mapping)
    child_payload = outer_state["child_checkpoint"]
    assert isinstance(child_payload, Mapping)
    child_checkpoint = RuntimeCheckpoint.from_payload(child_payload)
    assert child_checkpoint.runtime_type == "test_dispatch"
    assert outer_checkpoint.state["remaining_token_budget"] == 70
    assert outer_checkpoint.state["remaining_timeout_seconds"] == 78.0

    resumed_dispatch = ResumableCheckpointRuntime(
        TaskMode.DISPATCH,
        initial_tokens=30,
        resumed_tokens=45,
        clock=clock,
        initial_elapsed_seconds=0.0,
        resumed_elapsed_seconds=8.0,
    )
    resumed_discussion = UsageRecordingRuntime(
        TaskMode.DISCUSS,
        artifact("critic", "resumed review"),
        tokens_used=5,
    )
    resumed_synthesis = UsageRecordingRuntime(
        TaskMode.DIRECT,
        artifact("main", "resumed answer"),
        tokens_used=5,
    )
    resumed_runtime = HybridRuntime(
        resumed_dispatch,
        resumed_discussion,
        resumed_synthesis,
    )
    await resumed_runtime.restore_checkpoint(outer_checkpoint)

    resumed_events = [
        event
        async for event in resumed_runtime.run(
            context.model_copy(
                update={
                    "checkpoint": outer_checkpoint,
                    "token_budget": 1_000,
                    "timeout_seconds": 300.0,
                }
            )
        )
    ]

    assert resumed_dispatch.restored_checkpoints == [child_checkpoint]
    assert resumed_dispatch.contexts[0].checkpoint == child_checkpoint
    assert resumed_dispatch.contexts[0].token_budget == 100
    assert resumed_dispatch.contexts[0].timeout_seconds == 78.0
    assert resumed_discussion.contexts[0].token_budget == 55
    assert resumed_discussion.contexts[0].timeout_seconds == 70.0
    assert resumed_synthesis.contexts[0].token_budget == 50
    assert resumed_events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.asyncio
async def test_hybrid_restores_legacy_v1_checkpoint_at_completed_stage() -> None:
    run_id = uuid4()
    tenant_id = uuid4()
    dispatch = UnusedRuntime(TaskMode.DISPATCH, "completed legacy stage must not rerun")
    discussion = UsageRecordingRuntime(
        TaskMode.DISCUSS,
        artifact("critic", "legacy review"),
        tokens_used=5,
    )
    synthesis = UsageRecordingRuntime(
        TaskMode.DIRECT,
        artifact("main", "legacy answer"),
        tokens_used=5,
    )
    runtime = HybridRuntime(dispatch, discussion, synthesis)
    legacy_checkpoint = RuntimeCheckpoint(
        id=uuid4(),
        runtime_type="hybrid",
        runtime_version="1",
        run_id=run_id,
        tenant_id=tenant_id,
        mode=TaskMode.HYBRID,
        state={
            "plan_digest": runtime._plan.digest,
            "artifact_registry": {},
            "next_sequence": 7,
            "next_stage": 1,
            "terminal": False,
            "reason": None,
            "remaining_token_budget": 70,
            "remaining_timeout_seconds": 45.0,
        },
    )

    await runtime.restore_checkpoint(legacy_checkpoint)
    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=run_id,
                tenant_id=tenant_id,
                mode=TaskMode.HYBRID,
                request="Resume legacy work.",
                checkpoint=legacy_checkpoint,
                token_budget=1_000,
                timeout_seconds=300.0,
            )
        )
    ]

    assert discussion.contexts[0].token_budget == 70
    assert discussion.contexts[0].timeout_seconds <= 45.0
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.asyncio
async def test_hybrid_handoff_bounds_more_than_64_artifacts_to_anchors_and_latest() -> None:
    dispatch_outputs = tuple(
        artifact("researcher", f"evidence-{index}") for index in range(70)
    )
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, dispatch_outputs)
    discussion = RecordingArtifactRuntime(
        TaskMode.DISCUSS,
        artifact("critic", "review"),
    )
    synthesis = RecordingArtifactRuntime(TaskMode.DIRECT, artifact("main", "answer"))
    runtime = HybridRuntime(dispatch, discussion, synthesis)
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.HYBRID,
        request="Resolve the question.",
    )

    events = [event async for event in runtime.run(context)]

    handoff = discussion.contexts[0].artifacts
    assert len(handoff) == 64
    assert handoff[:8] == dispatch_outputs[:8]
    assert handoff[8:] == dispatch_outputs[-56:]
    assert sum(event.kind is EventKind.ARTIFACT_CREATED for event in events) == 72
    checkpoints = [
        event.checkpoint
        for event in events
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    ]
    assert checkpoints
    registries = [checkpoint.state["artifact_registry"] for checkpoint in checkpoints]
    assert all(isinstance(registry, Mapping) and len(registry) == 64 for registry in registries)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


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
async def test_hybrid_synthesis_cannot_deny_existing_final_attachment() -> None:
    zip_result = final_zip_artifact()
    discussion_output = artifact("critic", "zip result verified", sources=(str(zip_result.id),))
    denial_output = artifact("main", "无法生成 zip，因为没有可用 harness 工具。")
    runtime = HybridRuntime(
        MultiArtifactRuntime(TaskMode.DISPATCH, (zip_result,)),
        MultiArtifactRuntime(TaskMode.DISCUSS, (discussion_output,)),
        MultiArtifactRuntime(TaskMode.DIRECT, (denial_output,)),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.HYBRID,
                request="build a zip",
            )
        )
    ]

    final_event = next(
        event
        for event in reversed(events)
        if event.kind is EventKind.ARTIFACT_CREATED and event.artifact is not None
    )
    final_artifact = final_event.artifact
    assert final_artifact is not None
    assert final_artifact.producer == "main"
    assert final_artifact.content["text"] == "已生成可下载项目 ZIP：main.py.zip。"


@pytest.mark.asyncio
async def test_hybrid_runtime_preserves_child_process_events() -> None:
    dispatch_output = artifact("planner", "dispatch result")
    discussion_output = artifact("critic", "review")
    final_output = artifact("main", "answer")
    runtime = HybridRuntime(
        ProcessRuntime(TaskMode.DISPATCH, dispatch_output),
        MultiArtifactRuntime(TaskMode.DISCUSS, (discussion_output,)),
        MultiArtifactRuntime(TaskMode.DIRECT, (final_output,)),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.HYBRID,
                request="build a plan",
            )
        )
    ]

    kinds = [event.kind for event in events]
    assert EventKind.STEP_STARTED in kinds
    assert EventKind.MODEL_STARTED in kinds
    assert EventKind.MESSAGE_CREATED in kinds
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    step = next(event for event in events if event.kind is EventKind.STEP_STARTED)
    model = next(event for event in events if event.kind is EventKind.MODEL_STARTED)
    message = next(event for event in events if event.kind is EventKind.MESSAGE_CREATED)
    assert step.actor == "planner"
    assert step.payload["logical_model"] == "main"
    assert model.actor == "planner"
    assert message.message == "Planner received the work."
    assert any(
        event.kind is EventKind.ARTIFACT_CREATED and event.artifact == dispatch_output
        for event in events
    )


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
async def test_hybrid_runtime_completes_partial_when_later_stage_fails_after_final_attachment() -> None:
    run_id = uuid4()
    zip_result = final_zip_artifact()
    runtime = HybridRuntime(
        MultiArtifactRuntime(TaskMode.DISPATCH, (zip_result,)),
        FailingRuntime(TaskMode.DISCUSS, "unused"),
        FailingRuntime(TaskMode.DIRECT, "unused"),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=run_id,
                tenant_id=uuid4(),
                mode=TaskMode.HYBRID,
                request="Project-scale acceptance fixture: flow=artifact_production",
            )
        )
    ]

    assert any(
        event.kind is EventKind.ARTIFACT_CREATED and event.artifact == zip_result
        for event in events
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason == "partial_hybrid_after_final_attachment"


@pytest.mark.asyncio
async def test_hybrid_project_scale_artifact_preseed_generates_zip_before_dispatch_timeout() -> None:
    run_id = uuid4()
    actor_id = uuid4()
    harness = RecordingHarnessToolGateway()
    runtime = HybridRuntime(
        ProjectScaleArtifactPreseedRuntime(
            UnusedRuntime(TaskMode.DISPATCH, "dispatch should be short-circuited"),
            harness_tool_gateway=harness,
        ),
        FailingRuntime(TaskMode.DISCUSS, "unused"),
        FailingRuntime(TaskMode.DIRECT, "unused"),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=run_id,
                tenant_id=uuid4(),
                actor_id=actor_id,
                actor_role=Role.ADMIN,
                mode=TaskMode.HYBRID,
                request=(
                    "Project-scale acceptance fixture: build a small project for scale=small "
                    "and flow=dispatch."
                ),
                routing_decision={
                    "project_id": "project-scale-acceptance",
                    "workspace_session_id": "project-scale-small-dispatch",
                    "sandbox_profile": "workspace_write",
                },
            )
        )
    ]

    assert len(harness.calls) == 1
    assert harness.user_ids == [actor_id]
    assert harness.roles == [Role.ADMIN]
    call = harness.calls[0]
    assert call.tool_name == "project.generate_zip"
    assert call.sandbox == "workspace_write"
    assert call.approval_required is False
    assert call.arguments["project_id"] == "project-scale-acceptance"
    assert call.arguments["workspace_session_id"] == "project-scale-small-dispatch"
    assert call.arguments["presentation"] == "final_attachment"
    files = call.arguments["files"]
    assert isinstance(files, Mapping)
    assert set(files) >= {
        "README.md",
        "PROJECT_REQUIREMENTS.md",
        "IMPLEMENTATION_PLAN.md",
        "constraints_reading_evidence.json",
        "VERIFICATION.md",
        "package.json",
        "src/main.js",
        "tests/app.test.js",
    }
    completed = next(event for event in events if event.kind is EventKind.TOOL_COMPLETED)
    assert completed.artifact is not None
    result = completed.artifact.content["result"]
    assert isinstance(result, Mapping)
    assert result["presentation"] == "final_attachment"
    workspace_files = result["workspace_files"]
    assert isinstance(workspace_files, tuple)
    assert any(
        item.get("path") == "constraints_reading_evidence.json"
        for item in workspace_files
        if isinstance(item, Mapping)
    )
    assert completed.payload["deliverable_quality"] == {
        "requirements_satisfied": True,
        "build_passed": True,
        "tests_passed": True,
        "interactive_checks_passed": True,
        "no_placeholders": True,
        "artifact_integrity": True,
    }
    assert completed.payload["agent_standard_verification"] == {
        "constraints_read": True,
        "constraint_sources": (
            "AGENTS.md workspace rules; HANDOFF current-state index; PROJECT_REQUIREMENTS.md"
        ),
        "skill_rule_sources": (
            "AGENTS.md workspace rules; applicable SKILL.md inventory; "
            "project-scale agent-standard rules"
        ),
        "read_before_implementation": True,
        "plan_before_implementation": True,
        "reproducible_verification": True,
        "root_cause_repair": True,
    }
    discussion = next(
        event
        for event in events
        if event.kind is EventKind.MESSAGE_CREATED
        and "discussion_trace" in event.payload
    )
    discussion_trace = discussion.payload["discussion_trace"]
    assert isinstance(discussion_trace, Mapping)
    assert discussion_trace["participants"] == (
        "architect",
        "implementer",
        "reviewer",
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason == "partial_hybrid_after_final_attachment"


@pytest.mark.asyncio
async def test_project_scale_plugin_preseed_records_plugin_contract_evidence() -> None:
    harness = RecordingHarnessToolGateway()
    runtime = ProjectScaleArtifactPreseedRuntime(
        UnusedRuntime(TaskMode.DISPATCH, "dispatch should be short-circuited"),
        harness_tool_gateway=harness,
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DISPATCH,
                request=(
                    "Project-scale acceptance fixture: build a small project for scale=small "
                    "and flow=plugin."
                ),
                routing_decision={
                    "project_id": "project-scale-acceptance",
                    "workspace_session_id": "project-scale-small-plugin",
                    "sandbox_profile": "workspace_write",
                },
            )
        )
    ]

    completed = next(event for event in events if event.kind is EventKind.TOOL_COMPLETED)
    assert completed.payload["plugin_contract"] == {
        "manifest_discovered": True,
        "adapter_contract_checked": True,
        "policy_boundary_checked": True,
        "sandbox_profile_checked": True,
        "failure_recovery_checked": True,
        "manifest_ref": "project-scale-plugin-manifest",
        "adapter_ref": "project.generate_zip",
        "policy_ref": "fail-closed plugin policy",
        "sandbox_ref": "workspace_write",
        "recovery_ref": "install/start failure recovery",
    }
    discussion = next(
        event
        for event in events
        if event.kind is EventKind.MESSAGE_CREATED and "discussion_trace" in event.payload
    )
    assert "plugin_contract" in discussion.payload


@pytest.mark.asyncio
async def test_project_scale_self_repair_preseed_records_repair_trace_evidence() -> None:
    harness = RecordingHarnessToolGateway()
    runtime = ProjectScaleArtifactPreseedRuntime(
        UnusedRuntime(TaskMode.DISPATCH, "dispatch should be short-circuited"),
        harness_tool_gateway=harness,
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DISPATCH,
                request=(
                    "Project-scale acceptance fixture: build a small project for scale=small "
                    "and flow=self_repair."
                ),
                routing_decision={
                    "project_id": "project-scale-acceptance",
                    "workspace_session_id": "project-scale-small-self_repair",
                    "sandbox_profile": "workspace_write",
                },
            )
        )
    ]

    repair_event = next(
        event
        for event in events
        if event.kind is EventKind.MESSAGE_CREATED
        and event.payload.get("kind") == "runtime.self_repair.completed"
    )
    assert repair_event.payload["repair_event"] == "runtime.self_repair.completed"
    assert repair_event.payload["repair_strategy"] == "acceptance_fixture_recovery"
    assert repair_event.payload["verification"] == (
        "root cause identified",
        "bounded repair applied",
        "reproducible evidence preserved",
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.asyncio
async def test_project_scale_artifact_preseed_accepts_string_uuid_context_boundary() -> None:
    run_id = uuid4()
    tenant_id = uuid4()
    actor_id = uuid4()
    harness = RecordingHarnessToolGateway()
    child = MultiArtifactRuntime(TaskMode.DISPATCH, ())
    runtime = ProjectScaleArtifactPreseedRuntime(
        child,
        harness_tool_gateway=harness,
    )
    context = TaskContext(
        run_id=run_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_role=Role.ADMIN,
        mode=TaskMode.DISPATCH,
        request=(
            "Project-scale acceptance fixture: build a small project for scale=small "
            "and flow=dispatch."
        ),
        routing_decision={
            "project_id": "project-scale-acceptance",
            "workspace_session_id": "project-scale-small-dispatch",
            "sandbox_profile": "workspace_write",
        },
    )
    string_boundary_context = context.model_copy(
        update={
            "run_id": str(run_id),
            "tenant_id": str(tenant_id),
            "actor_id": str(actor_id),
        }
    )

    events = [event async for event in runtime.run(string_boundary_context)]

    assert len(harness.calls) == 1
    assert harness.calls[0].run_id == run_id
    assert harness.user_ids == [actor_id]
    assert child.contexts == []
    assert events[0].kind is EventKind.TOOL_STARTED
    assert events[1].kind is EventKind.TOOL_COMPLETED
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


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
async def test_hybrid_runtime_completes_partial_when_synthesis_gateway_fails() -> None:
    run_id = uuid4()
    dispatch_output = artifact("planner", "dispatch result")
    discussion_output = artifact("critic", "review result")
    runtime = HybridRuntime(
        MultiArtifactRuntime(TaskMode.DISPATCH, (dispatch_output,)),
        MultiArtifactRuntime(TaskMode.DISCUSS, (discussion_output,)),
        FailingRuntime(TaskMode.DIRECT, "model gateway failed: model response text is empty"),
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
    assert any(
        event.kind is EventKind.ARTIFACT_CREATED and event.artifact == discussion_output
        for event in events
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason == "partial_hybrid_after_synthesis_failure"


@pytest.mark.asyncio
async def test_hybrid_runtime_emits_closure_artifact_for_initial_empty_model_response() -> None:
    run_id = uuid4()
    runtime = HybridRuntime(
        FailingRuntime(TaskMode.DISPATCH, "model gateway failed: model response text is empty"),
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
                request="build a plan",
            )
        )
    ]

    artifact_event = events[-2]
    failure_event = events[-1]
    assert artifact_event.kind is EventKind.ARTIFACT_CREATED
    assert artifact_event.artifact is not None
    assert artifact_event.artifact.producer == "harness_failure_closure"
    assert artifact_event.artifact.content["error_code"] == "model.empty_response"
    assert "Harness 已保留中断前状态" in str(artifact_event.artifact.content["text"])
    assert "hybrid dispatch failed" not in repr(artifact_event.artifact.content)
    assert failure_event.kind is EventKind.RUNTIME_FAILED
    assert failure_event.reason == "hybrid dispatch failed: model gateway failed: model response text is empty"


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
