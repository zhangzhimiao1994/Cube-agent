from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRecord, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.failure_reason import RECOVERY_BLOCKED_FAILURE_REASON
from agent_hub.runtime.registry import RuntimeRegistry
from tests.integration.runs.test_recovery import FakeRuntime, RecordingQueue
from tests.integration.runs.test_recovery import (
    run_session_factory as run_session_factory,  # noqa: PLC0414 - pytest fixture export.
)


class ResumedRuntime(FakeRuntime):
    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.calls += 1
        assert context.checkpoint is not None
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=7, run_id=context.run_id)


def seed_events(checkpoint: RuntimeCheckpoint, unsafe_kind: str | None) -> tuple[RunEvent, ...]:
    run_id = checkpoint.run_id
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=3, run_id=run_id, reason="prior failure")
    if unsafe_kind == "model.started":
        event = RunEvent(kind=EventKind.MODEL_STARTED, sequence=3, run_id=run_id)
    elif unsafe_kind in {"tool.started", "tool.completed"}:
        event = RunEvent(
            kind=unsafe_kind, sequence=3, run_id=run_id, actor="implementer",
            tool_call_id="prior-write", tool_name="workspace.write_text", payload={"status": "succeeded"},
        )
    elif unsafe_kind == "artifact.created":
        event = RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=3, run_id=run_id,
                         artifact=Artifact(id=uuid4(), type="text", producer="implementer", content={"text": "prior"}))
    return (
        RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=1, run_id=run_id, checkpoint=checkpoint),
        RunEvent(kind=EventKind.STEP_FAILED, sequence=2, run_id=run_id,
                 step_id="implementer_step", actor="implementer", reason="prior failure"),
        event,
        RunEvent(kind="repair.classified", sequence=4, run_id=run_id, payload={"schema_version": 1}),
        RunEvent(kind="terminal.notified", sequence=5, run_id=run_id, payload={"status": "failed"}),
        RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=6, run_id=run_id, reason=RECOVERY_BLOCKED_FAILURE_REASON),
    )


async def seed_blocked_run(
    repository: RunRepository, *, unsafe_kind: str | None = None,
) -> tuple[RunRecord, UUID]:
    tenant, actor, run_id = uuid4(), uuid4(), uuid4()
    created = await repository.create_run(
        tenant_id=tenant, actor_id=actor, request="resume approved work",
        mode=TaskMode.DISPATCH, status=RunStatus.FAILED, idempotency_key=None,
        routing_decision={"source": "self_repair", "self_repair_accepted": True,
                          "self_repair_source_run_id": str(run_id),
                          "self_repair_recovery_baseline_sequence": 5,
                          "self_repair_attempt": 1}, enqueue=False,
    )
    async with await repository.run_transaction() as session, session.begin():
        row = await repository.get_for_update(session, created.id)
        row.routing_decision = {**(row.routing_decision or {}), "self_repair_source_run_id": str(created.id)}
        checkpoint = RuntimeCheckpoint(
            id=uuid4(), runtime_type="fake-dispatch", runtime_version="1", run_id=created.id,
            tenant_id=tenant, mode=TaskMode.DISPATCH, state={"completed_step_ids": ("prepare",)},
        )
        for event in seed_events(checkpoint, unsafe_kind):
            await repository.persist_event(
                session, tenant_id=tenant, run_id=created.id, event=event,
            )
    return await repository.get(tenant, created.id), actor


async def test_retry_recovery_preserves_approved_prefix_and_executes_once(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    record, actor = await seed_blocked_run(repository)
    before = await repository.events(record.tenant_id, record.id)
    runtime, queue = ResumedRuntime(), RecordingQueue([], [])
    service = RunService(repository, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=queue)

    retried = await service.retry_recovery(
        tenant_id=record.tenant_id, actor_id=actor, run_id=record.id, version=record.version,
    )
    assert retried.status is RunStatus.QUEUED and retried.version == record.version + 1
    with pytest.raises(RunConflict):
        await service.retry_recovery(
            tenant_id=record.tenant_id, actor_id=actor, run_id=record.id, version=record.version,
        )
    assert await service.publish_pending() == 1
    assert await service.publish_pending() == 0
    completed = await service.execute(record.id)
    assert completed.status is RunStatus.COMPLETED
    assert (await service.execute(record.id)).status is RunStatus.COMPLETED
    assert runtime.calls == len(runtime.restored) == 1
    assert queue.enqueued == [record.id]
    after = await repository.events(record.tenant_id, record.id)
    assert after[:len(before)] == before
    current = await repository.get(record.tenant_id, record.id)
    assert current.routing_decision is not None
    assert current.routing_decision["self_repair_attempt"] == 1
    assert current.routing_decision["self_repair_recovery_baseline_sequence"] == 6


@pytest.mark.parametrize("unsafe_kind", ["model.started", "tool.started", "tool.completed", "artifact.created"])
async def test_retry_recovery_never_ignores_uncheckpointed_execution(
    run_session_factory: async_sessionmaker[AsyncSession], unsafe_kind: str,
) -> None:
    repository = RunRepository(run_session_factory)
    record, actor = await seed_blocked_run(repository, unsafe_kind=unsafe_kind)
    with pytest.raises(RunConflict):
        await repository.retry_self_repair_recovery_and_enqueue(
            tenant_id=record.tenant_id, actor_id=actor, run_id=record.id, version=record.version,
        )
    assert await repository.get(record.tenant_id, record.id) == record
    assert await repository.pending_outbox() == ()


async def test_retry_recovery_checks_owner_and_version_inside_transaction(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    record, actor = await seed_blocked_run(repository)
    with pytest.raises(RunNotFound):
        await repository.retry_self_repair_recovery_and_enqueue(
            tenant_id=record.tenant_id, actor_id=uuid4(), run_id=record.id, version=record.version,
        )
    with pytest.raises(RunConflict):
        await repository.retry_self_repair_recovery_and_enqueue(
            tenant_id=record.tenant_id, actor_id=actor, run_id=record.id, version=record.version - 1,
        )
    assert await repository.get(record.tenant_id, record.id) == record
    assert await repository.pending_outbox() == ()
