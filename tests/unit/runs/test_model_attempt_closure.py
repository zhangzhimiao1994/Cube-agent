from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.service import RunService, _adaptive_runtime_events, _has_delivery_artifact
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import (
    ExecutableFakeRepository,
    SelfRepairCheckpointRepository,
)


@pytest.mark.parametrize(("artifact_types", "expected_budgets"), [
    (("model_attempt",), (1_500_000, 1_500_000)),
    (("model_attempt",) * 3, (1_500_000,) * 4),
    (("text",), (1_500_000, 1_750_000)),
    (("model_attempt", "text", "model_attempt", "text", "model_attempt"),
     (1_500_000, 1_500_000, 1_750_000, 1_750_000, 2_000_000, 2_000_000)),
], ids=["single-receipt", "multipart-receipts", "delivery", "mixed-events"])
async def test_only_delivery_artifacts_extend_progress_budget(
    artifact_types: tuple[str, ...], expected_budgets: tuple[int, ...],
) -> None:
    observed_budgets: list[int] = []
    artifacts = tuple(
        Artifact(id=uuid4(), type=artifact_type, producer="main_agent",
                 content={"text": "delivered"} if artifact_type == "text"
                 else {"part_index": index, "history_complete": True})
        for index, artifact_type in enumerate(artifact_types, 1)
    )

    class Runtime:
        mode = TaskMode.DIRECT

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            observed_budgets.append(context.token_budget)
            sequence = 1
            for artifact in artifacts:
                yield RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=sequence,
                               run_id=context.run_id, actor="main_agent", artifact=artifact)
                sequence += 1
                observed_budgets.append(context.token_budget)
                if artifact.type == "model_attempt":
                    yield RunEvent(kind="model.failure_receipt", sequence=sequence,
                                   run_id=context.run_id,
                                   payload={"artifact_id": str(artifact.id), "actor": "main_agent"})
                    sequence += 1
                    assert context.token_budget == observed_budgets[-1]
            yield RunEvent(
                kind=EventKind.CHECKPOINT_SAVED, sequence=sequence, run_id=context.run_id,
                checkpoint=RuntimeCheckpoint(
                    id=uuid4(), runtime_type="test_direct", runtime_version="1",
                    run_id=context.run_id, tenant_id=context.tenant_id, mode=self.mode,
                    state={"completed": (), "artifact_registry": {
                        str(artifact.id): artifact.content_sha256 for artifact in artifacts
                    }},
                ),
            )
            assert context.token_budget == expected_budgets[-1]

        async def cancel(self) -> None:
            pass

        async def save_checkpoint(self) -> RuntimeCheckpoint:
            raise AssertionError("unused")

        async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
            raise AssertionError("unused")

    decision = {"project_scale": "medium", "critical_path_complexity_units": 4}
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DIRECT,
                          request="Build a project", token_budget=1_500_000)
    events = [event async for event in _adaptive_runtime_events(
        Runtime(), context, configured_tokens=1_000_000,
        routing_decision=decision, initial_progress_units=0,
    )]

    assert observed_budgets == list(expected_budgets)
    assert context.token_budget == expected_budgets[-1]
    assert tuple(event.artifact for event in events if event.artifact is not None) == artifacts
    assert sum(event.kind == "model.failure_receipt" for event in events) == artifact_types.count(
        "model_attempt"
    )


@pytest.mark.parametrize("completed_key", ["completed", "completed_steps"])
async def test_mixed_digest_checkpoint_restore_keeps_completed_work_and_ignores_receipts(
    completed_key: str,
) -> None:
    repository = SelfRepairCheckpointRepository(routing_decision={
        "source": "self_repair", "self_repair_accepted": True,
        "project_scale": "medium", "critical_path_complexity_units": 8,
    })
    artifacts = tuple(
        Artifact(id=uuid4(), type=kind, producer="main_agent", content={"text": "evidence"})
        for kind in ("text", "text", "model_attempt", "model_attempt", "model_attempt")
    )
    checkpoint = RuntimeCheckpoint(
        id=uuid4(), runtime_type="test_dispatch", runtime_version="1.0",
        run_id=repository.run_id, tenant_id=repository.checkpoint.tenant_id,
        mode=TaskMode.DISPATCH,
        state={completed_key: ("write-step", "verify-step"),
               "input_refs": ({"id": str(artifacts[0].id)},),
               "artifact_registry": {str(artifact.id): artifact.content_sha256
                                     for artifact in artifacts}},
    )
    repository.checkpoint = checkpoint
    repository.event_log.extend(
        RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=index,
                 run_id=repository.run_id, artifact=artifact)
        for index, artifact in enumerate(artifacts, 1)
    )
    budgets: list[int] = []

    class Runtime:
        mode = TaskMode.DISPATCH

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            budgets.append(context.token_budget)
            for index, artifact in enumerate(artifacts[2:], 3):
                yield RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=index,
                               run_id=context.run_id, actor="main_agent", artifact=artifact)
                budgets.append(context.token_budget)
            yield RunEvent(kind=EventKind.TOOL_COMPLETED, sequence=6, run_id=context.run_id,
                           actor="main_agent", tool_call_id="new-write",
                           tool_name="workspace.write_text", payload={"result_bytes": 12})
            budgets.append(context.token_budget)
            yield RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=7, run_id=context.run_id,
                           checkpoint=checkpoint)
            budgets.append(context.token_budget)
            yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=8, run_id=context.run_id)

        async def cancel(self) -> None:
            pass

        async def save_checkpoint(self) -> RuntimeCheckpoint:
            return checkpoint

        async def restore_checkpoint(self, restored: RuntimeCheckpoint) -> None:
            assert restored == checkpoint

    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((Runtime(),)), router=None,
        task_queue=object(),  # type: ignore[arg-type]
        runtime_token_budget=1_000_000,
    )
    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert budgets == [1_875_000] * 4 + [2_000_000, 2_000_000]


@pytest.mark.parametrize("with_delivery", [False, True])
async def test_model_attempt_does_not_suppress_empty_response_closure(
    with_delivery: bool,
) -> None:
    scope = Artifact(id=uuid4(), type="model_attempt", producer="main_agent",
                     content={"history_complete": True})

    class Runtime:
        mode = TaskMode.DISPATCH

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            yield RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=1,
                           run_id=context.run_id, artifact=scope)
            if with_delivery:
                yield RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=2,
                               run_id=context.run_id, artifact=Artifact(
                                   id=uuid4(), type="text", producer="writer",
                                   content={"text": "delivered"},
                               ))
            yield RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=3,
                           run_id=context.run_id,
                           reason="model gateway failed: model response text is empty")

        async def cancel(self) -> None:
            pass

        async def save_checkpoint(self) -> RuntimeCheckpoint:
            raise AssertionError("failed run has no checkpoint")

        async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
            raise AssertionError("unused")

    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((Runtime(),)),
        router=None, task_queue=object(),  # type: ignore[arg-type]
    )
    submitted = await service.execute(repository.run_id)
    assert submitted.status is RunStatus.FAILED
    scope_event = next(event for event in repository.event_log if event.artifact == scope)
    assert not _has_delivery_artifact((scope_event,))
    closures = [event.artifact for event in repository.event_log
                if event.artifact is not None and event.artifact.producer == "run_service"]
    assert len(closures) == (0 if with_delivery else 1)
    if closures:
        assert closures[0].content["failure_category"] == "empty_model_response"
    assert scope_event.artifact is not None
    assert scope_event.artifact.content_sha256 == scope.content_sha256
