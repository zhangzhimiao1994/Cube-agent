"""Real PG cancellation boundary with a controlled runtime, not a full Crew run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from agent_hub.db.models import RuntimeArtifactRow, RuntimeArtifactWriteRow
from agent_hub.db.session import Database, build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry


class CancellationQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        pass


class ReservedArtifactRuntime:
    """Commit cancellation before yielding; cleanup needs a second real RunRow lock."""

    mode = TaskMode.DISPATCH

    def __init__(self, database: Database) -> None:
        self.database = database
        self.repository = PostgresArtifactRepository(database.session_factory)
        self.runs = RunRepository(database.session_factory)
        self.artifact = Artifact(
            id=uuid4(), type="text", producer="worker",
            content={"text": "private cancellation boundary payload"},
        )
        self.reference = ArtifactReference(
            id=self.artifact.id, sha256=self.artifact.content_sha256,
        )
        self.write_id = uuid4()
        self.context: TaskContext | None = None
        self.reserved = False
        self.cancellation_committed = False
        self.cancel_calls = 0
        self.abort_completed = False
        self.abort_result: bool | None = None

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.context = context
        await self.repository.reserve_write(
            context.tenant_id, context.run_id, self.reference, write_id=self.write_id,
        )
        # A separate session proves the reservation committed before control changes.
        async with self.database.session_factory() as session:
            reservation = await session.get(
                RuntimeArtifactWriteRow, (context.tenant_id, context.run_id, self.write_id),
            )
            assert reservation is not None and reservation.status == "reserved"
        self.reserved = True
        # This public control operation uses its own transaction, like a user cancel.
        cancelled = await self.runs.update_control_status(
            context.tenant_id, context.run_id, RunStatus.CANCELLED,
        )
        assert cancelled.status is RunStatus.CANCELLED
        self.cancellation_committed = True
        yield RunEvent(kind=EventKind.MODEL_STARTED, sequence=1, run_id=context.run_id)
        raise AssertionError("A cancelled run must not request another runtime event")

    async def cancel(self) -> None:
        assert self.context is not None
        self.cancel_calls += 1
        self.abort_result = await self.repository.abort_write(
            self.context.tenant_id, self.context.run_id, self.reference, write_id=self.write_id,
        )
        self.abort_completed = True

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("This cancellation fixture does not create checkpoints")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError("This cancellation fixture must start a fresh run")


async def test_dispatch_cancel_releases_run_lock_before_private_artifact_abort(
    database_url: str,
) -> None:
    database = build_database(database_url)
    repository = RunRepository(database.session_factory)
    tenant_id, actor_id = uuid4(), uuid4()
    run_id: UUID | None = None
    try:
        assert database.engine.dialect.name == "postgresql", "Real PostgreSQL is required"
        runtime = ReservedArtifactRuntime(database)
        service = RunService(
            repository, runtime_registry=RuntimeRegistry((runtime,)),
            router=None, task_queue=CancellationQueue(), run_worker_lease_seconds=60,
        )
        submitted = await service.submit(
            tenant_id=tenant_id, actor_id=actor_id, mode=TaskMode.DISPATCH,
            message="Exercise private artifact cancellation without external actions.",
        )
        run_id = submitted.id
        try:
            result = await asyncio.wait_for(service.execute(run_id), timeout=5)
        except TimeoutError:
            # Reject unrelated setup timeouts: the old code stalls inside real abort_write.
            assert runtime.reserved and runtime.cancellation_committed
            assert runtime.cancel_calls == 1 and not runtime.abort_completed
            async with database.session_factory() as session:
                reservation = await session.get(
                    RuntimeArtifactWriteRow, (tenant_id, run_id, runtime.write_id),
                )
                assert reservation is not None and reservation.status == "reserved"
            raise

        assert result.status is RunStatus.CANCELLED
        assert runtime.reserved and runtime.cancellation_committed
        assert runtime.cancel_calls == 1 and runtime.abort_completed
        assert runtime.abort_result is True
        async with database.session_factory() as session:
            reservation = await session.get(
                RuntimeArtifactWriteRow, (tenant_id, run_id, runtime.write_id),
            )
            assert reservation is not None and reservation.status == "aborted"
            assert reservation.artifact_id == runtime.reference.id
            assert reservation.content_sha256 == runtime.reference.sha256
            assert await session.get(
                RuntimeArtifactRow, (tenant_id, run_id, runtime.reference.id),
            ) is None
        assert await repository.artifacts(tenant_id, run_id) == ()
        assert await repository.raw_artifacts(tenant_id, run_id) == ()
        events = await repository.raw_events(tenant_id, run_id)
        assert not any(event.kind in {
            EventKind.MODEL_STARTED, EventKind.ARTIFACT_CREATED, EventKind.RUNTIME_COMPLETED,
        } for event in events)
    finally:
        try:
            if run_id is not None:
                async with database.session_factory() as session, session.begin():
                    row = await repository.get_for_update(session, run_id)
                    assert row.tenant_id == tenant_id
                    row.status = RunStatus.CANCELLED.value
                await repository.delete_run(tenant_id, run_id)
        finally:
            await database.dispose()
