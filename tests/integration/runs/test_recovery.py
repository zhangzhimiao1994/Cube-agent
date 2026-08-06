from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import RunEventRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runs.tasks import CeleryRunQueue
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry


class FakeRuntime:
    mode = TaskMode.DISPATCH

    def __init__(self) -> None:
        self.calls = 0
        self.restored: list[RuntimeCheckpoint] = []
        self.artifact_id = uuid4()

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.calls += 1
        if context.checkpoint is not None:
            yield RunEvent(
                kind=EventKind.RUNTIME_COMPLETED,
                sequence=3,
                run_id=context.run_id,
            )
            return
        yield RunEvent(
            kind=EventKind.STEP_COMPLETED,
            sequence=1,
            run_id=context.run_id,
            step_id="research",
            actor="researcher",
            payload={"completed": True},
        )
        artifact = Artifact(
            id=self.artifact_id,
            type="text",
            producer="researcher",
            content={"text": "research complete"},
        )
        checkpoint = RuntimeCheckpoint(
            id=uuid4(),
            runtime_type="fake-dispatch",
            runtime_version="1",
            run_id=context.run_id,
            tenant_id=context.tenant_id,
            mode=TaskMode.DISPATCH,
            state={
                "completed_step_ids": ("research",),
                "artifact_refs": ({"id": str(artifact.id), "sha256": artifact.content_sha256}),
            },
        )
        yield RunEvent(
            kind=EventKind.ARTIFACT_CREATED,
            sequence=2,
            run_id=context.run_id,
            artifact=artifact,
        )
        yield RunEvent(
            kind=EventKind.CHECKPOINT_SAVED,
            sequence=3,
            run_id=context.run_id,
            checkpoint=checkpoint,
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("service persists checkpoint events directly")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        self.restored.append(checkpoint)

    async def cancel(self) -> None:
        raise AssertionError("not used")


class AccountingRuntime:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.APPROVAL_REQUESTED,
            sequence=1,
            run_id=context.run_id,
            actor="operator",
            approval_id="approval_1",
            action="tool_execute",
            reason="requires_user_approval",
        )
        yield RunEvent(
            kind=EventKind.APPROVAL_RESOLVED,
            sequence=2,
            run_id=context.run_id,
            actor="operator",
            approval_id="approval_1",
            decision="approved",
        )
        yield RunEvent(
            kind=EventKind.COST_RECORDED,
            sequence=3,
            run_id=context.run_id,
            actor="researcher",
            provider_id="deepseek",
            cost_usd=Decimal("0.25"),
            currency="USD",
        )
        yield RunEvent(
            kind=EventKind.RUNTIME_COMPLETED,
            sequence=4,
            run_id=context.run_id,
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError("not used")

    async def cancel(self) -> None:
        raise AssertionError("not used")


class ExplodingRuntime:
    mode = TaskMode.DISPATCH

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        del context
        return self._run()

    async def _run(self) -> AsyncIterator[RunEvent]:
        raise RuntimeError("provider details must not leak")
        yield  # pragma: no cover

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError("not used")

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RestoreExplodingRuntime(FakeRuntime):
    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint
        raise RuntimeError("restore failed")


@dataclass(slots=True)
class RecordingQueue:
    enqueued: list[UUID]
    idempotency_keys: list[str] | None = None
    fail_once: bool = False

    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("queue unavailable")
        self.enqueued.append(run_id)
        if self.idempotency_keys is not None:
            self.idempotency_keys.append(idempotency_key)


class Router(Protocol):
    async def route(
        self, task_text: object, *, confirmation_subject: object | None = None
    ) -> object: ...


@pytest.fixture
async def run_session_factory(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from agent_hub.db.session import build_database
    from tests.integration.conftest import _clean_database

    database = build_database(database_url)
    try:
        await _clean_database(database.session_factory)
        yield database.session_factory
    finally:
        await _clean_database(database.session_factory)
        await database.dispose()


async def test_worker_resumes_from_latest_safe_checkpoint_without_duplicate_artifact_work(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    queue = RecordingQueue([])
    runtime = FakeRuntime()
    repository = RunRepository(run_session_factory)
    service = RunService(
        repository,
        runtime_registry=RuntimeRegistry((runtime,)),
        router=None,
        task_queue=queue,
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=user_id,
        message="research the topic",
        mode=TaskMode.DISPATCH,
        idempotency_key="tenant-safe-key",
    )

    first = await service.execute(submitted.id, crash_after_event_kind=EventKind.CHECKPOINT_SAVED)
    assert first.status is RunStatus.RUNNING
    duplicate_worker = await service.execute(submitted.id)
    duplicate_events = await service.events(tenant_id, submitted.id)

    recovered = await service.recover(submitted.id)
    run = await service.get(tenant_id, submitted.id)
    events = await service.events(tenant_id, submitted.id)

    assert duplicate_worker.status is RunStatus.RUNNING
    assert sum(event["kind"] == "artifact.created" for event in duplicate_events) == 1
    assert recovered.status is RunStatus.COMPLETED
    assert run.status is RunStatus.COMPLETED
    assert run.completed_step_ids == ("research",)
    assert sum(event["kind"] == "artifact.created" for event in events) == 1
    assert runtime.calls == 2
    assert len(runtime.restored) == 1


async def test_recovery_fails_safe_when_side_effect_event_has_no_checkpoint(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    runtime = FakeRuntime()
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((runtime,)),
        router=None,
        task_queue=RecordingQueue([]),
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="research the topic",
        mode=TaskMode.DISPATCH,
        idempotency_key="tenant-safe-key-no-checkpoint",
    )

    first = await service.execute(submitted.id, crash_after_event_kind=EventKind.ARTIFACT_CREATED)
    recovered = await service.recover(submitted.id)
    events = await service.events(tenant_id, submitted.id)

    assert first.status is RunStatus.RUNNING
    assert recovered.status is RunStatus.FAILED
    assert runtime.calls == 1
    assert sum(event["kind"] == "artifact.created" for event in events) == 1


async def test_submission_writes_run_and_outbox_atomically_then_publisher_delivers_once(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    queue = RecordingQueue([], [])
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=None,
        task_queue=queue,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run fixed workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-1",
    )
    duplicate = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run fixed workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-1",
    )

    assert duplicate.id == submitted.id
    assert queue.enqueued == []
    assert await service.publish_pending() == 1
    assert await service.publish_pending() == 0
    assert queue.enqueued == [submitted.id]
    assert queue.idempotency_keys == [f"{tenant_id}:client-request-1"]


async def test_choose_mode_persists_choice_and_enqueues_waiting_run(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    queue = RecordingQueue([])
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=None,
        task_queue=queue,
    )

    waiting = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="ambiguous workflow",
        mode=TaskMode.AUTO,
        idempotency_key="client-request-2",
    )
    assert waiting.status is RunStatus.WAITING_USER_MODE
    assert waiting.decision_token is not None

    chosen = await service.choose_mode(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        run_id=waiting.id,
        mode=TaskMode.DISPATCH,
        decision_token=waiting.decision_token,
        version=waiting.version,
    )

    assert chosen.status is RunStatus.QUEUED
    assert chosen.mode is TaskMode.DISPATCH
    assert queue.enqueued == []
    assert await service.publish_pending() == 1
    assert await service.publish_pending() == 0
    assert queue.enqueued == [waiting.id]


async def test_outbox_is_not_marked_delivered_when_queue_enqueue_fails(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    queue = RecordingQueue([], fail_once=True)
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=None,
        task_queue=queue,
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run fixed workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-3",
    )

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await service.publish_pending()

    assert queue.enqueued == []
    assert await service.publish_pending() == 1
    assert await service.publish_pending() == 0
    assert queue.enqueued == [submitted.id]


async def test_resume_paused_run_enqueues_recovery_work(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    queue = RecordingQueue([])
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=None,
        task_queue=queue,
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run fixed workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-4",
    )
    await service.pause(tenant_id, submitted.id)

    resumed = await service.resume(tenant_id, submitted.id)

    assert resumed.status is RunStatus.QUEUED
    assert await service.publish_pending() == 2
    assert await service.publish_pending() == 0
    assert queue.enqueued == [submitted.id, submitted.id]


async def test_worker_persists_approval_and_usage_events(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((AccountingRuntime(),)),
        router=None,
        task_queue=RecordingQueue([]),
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run accounting workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-5",
    )

    completed = await service.execute(submitted.id)
    summary = await service.get(tenant_id, submitted.id)

    assert completed.status is RunStatus.COMPLETED
    assert summary.usage_cost_usd == Decimal("0.25")


async def test_runtime_exception_is_persisted_as_terminal_failure(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((ExplodingRuntime(),)),
        router=None,
        task_queue=RecordingQueue([]),
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="explode safely",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-8",
    )

    failed = await service.execute(submitted.id)
    duplicate_retry = await service.execute(submitted.id)
    events = await service.events(tenant_id, submitted.id)

    assert failed.status is RunStatus.FAILED
    assert duplicate_retry.status is RunStatus.FAILED
    assert [event["kind"] for event in events] == ["runtime.failed"]


async def test_restore_exception_is_persisted_as_terminal_failure(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    runtime = RestoreExplodingRuntime()
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((runtime,)),
        router=None,
        task_queue=RecordingQueue([]),
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="restore safely",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-9",
    )
    await service.execute(submitted.id, crash_after_event_kind=EventKind.CHECKPOINT_SAVED)

    failed = await service.recover(submitted.id)
    duplicate_retry = await service.execute(submitted.id)
    events = await service.events(tenant_id, submitted.id)

    assert failed.status is RunStatus.FAILED
    assert duplicate_retry.status is RunStatus.FAILED
    assert events[-1]["kind"] == "runtime.failed"


async def test_celery_run_queue_uses_outbox_key_as_task_id() -> None:
    class CeleryApp:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def send_task(self, name: str, **kwargs: object) -> None:
            self.sent.append({"name": name, **kwargs})

    app = CeleryApp()
    queue = CeleryRunQueue(app)
    run_id = uuid4()

    await queue.enqueue_run(run_id, idempotency_key="tenant:request")

    assert app.sent == [
        {
            "name": "agent_hub.runs.execute",
            "args": [str(run_id)],
            "task_id": "tenant:request",
        }
    ]


async def test_usage_events_are_idempotent_by_run_sequence(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    repository = RunRepository(run_session_factory)
    service = RunService(
        repository,
        runtime_registry=RuntimeRegistry((AccountingRuntime(),)),
        router=None,
        task_queue=RecordingQueue([]),
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run accounting workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-6",
    )
    duplicate_cost = RunEvent(
        kind=EventKind.COST_RECORDED,
        sequence=1,
        run_id=submitted.id,
        actor="researcher",
        provider_id="deepseek",
        cost_usd=Decimal("0.25"),
        currency="USD",
    )

    async with await repository.run_transaction() as session, session.begin():
        await repository.persist_event(
            session,
            tenant_id=tenant_id,
            run_id=submitted.id,
            event=duplicate_cost,
        )
        await repository.persist_event(
            session,
            tenant_id=tenant_id,
            run_id=submitted.id,
            event=duplicate_cost,
        )

    summary = await service.get(tenant_id, submitted.id)
    assert summary.usage_cost_usd == Decimal("0.25")


async def test_public_events_sanitize_sensitive_persisted_payload_keys(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    repository = RunRepository(run_session_factory)
    submitted = await repository.create_run(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        request="inspect event",
        mode=TaskMode.DISPATCH,
        status=RunStatus.RUNNING,
        idempotency_key="client-request-7",
        enqueue=False,
    )
    async with run_session_factory() as session, session.begin():
        session.add(
            RunEventRow(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=submitted.id,
                sequence=1,
                kind="custom.progress",
                payload={
                    "kind": "custom.progress",
                    "sequence": 1,
                    "run_id": str(submitted.id),
                    "payload": {
                        "safe": True,
                        "api_key": "sk-should-not-leak",
                        "nested": {"hidden_reasoning": "private", "visible": "ok"},
                    },
                },
            )
        )

    events = await repository.events(tenant_id, submitted.id)

    serialized = str(events).lower()
    assert "safe" in serialized
    assert "visible" in serialized
    assert "api_key" not in serialized
    assert "hidden_reasoning" not in serialized
    assert "sk-should-not-leak" not in serialized
