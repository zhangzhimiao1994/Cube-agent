"""Real PostgreSQL owner locking and permanent archives; Root IO has separate fixtures."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
from agent_hub.db.models import RunRow, RuntimeArtifactRow
from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRepository
from tests.unit.capabilities.test_python_expired_capacity_oct29 import Session as FixtureOwner
from tests.unit.capabilities.test_stopped_premodel_retirement_lifecycle_nov07 import snapshot_for
from tests.unit.skills.sandbox.test_python_project_client import invocation


@pytest.mark.parametrize("requeue_after_archive", [False, True])
async def test_real_permanent_archive_is_outside_owner_lock_and_requeued_owner_cannot_commit(
    database_url: str, requeue_after_archive: bool,
) -> None:
    database = build_database(database_url)
    repository = RunRepository(database.session_factory)
    tenant = uuid4()
    record = await repository.create_run(
        tenant_id=tenant, actor_id=uuid4(), request="fixture original request",
        mode=TaskMode.DISPATCH, status=RunStatus.FAILED, idempotency_key=None,
        routing_decision=FixtureOwner().row.routing_decision, enqueue=False,
    )
    item = invocation().model_copy(update={"tenant_id": tenant, "run_id": record.id,
                                          "source_kind": "original", "install_allowed": False})
    cutoff = datetime.now(UTC) - timedelta(days=1)
    async with database.session_factory() as session, session.begin():
        row = await repository.get_for_update(session, record.id)
        row.updated_at = cutoff - timedelta(days=1)
        assert row.routing_decision is not None
        before = (row.version, row.status, row.updated_at, row.request, dict(row.routing_decision))
    calls: list[str] = []

    class Reader:
        async def python_expired_environment_invocation(self, owner: UUID, run: UUID) -> Any:
            assert (owner, run) == (tenant, record.id)
            return item

    class Executor:
        async def inspect_stopped_premodel(self, maintenance: Any) -> Any:
            calls.append("inspect")
            assert (maintenance.tenant_id, maintenance.run_id) == (tenant, record.id)
            return snapshot_for(item)

        async def commit_stopped_premodel(self, maintenance: Any, snapshot: Any) -> bool:
            calls.append("commit")
            # A separate real transaction sees both durable rows. If archive
            # publication had retained/nested the owner lock this call is unreachable.
            async with database.session_factory() as session:
                rows = (await session.scalars(select(RuntimeArtifactRow).where(
                    RuntimeArtifactRow.tenant_id == tenant,
                    RuntimeArtifactRow.run_id == record.id,
                ))).all()
                assert len(rows) == 2 and all(row.permanent is True for row in rows)
                values = [PostgresArtifactRepository._hydrate(row) for row in rows]
                assert {v.type for v in values} == {
                    "python_stopped_premodel_cache_archive", "python_stopped_premodel_commit_intent",
                }
                assert snapshot.archive().to_payload() in [v.to_payload() for v in values]
            return True

        async def release_expired(self, *_args: Any) -> bool:
            pytest.fail("a stopped attempt must never fall through to normal release")

    class Archives(PostgresArtifactRepository):
        async def get_many(self, owner: UUID, run: UUID, references: Any) -> Any:
            values = await super().get_many(owner, run, references)
            if requeue_after_archive and values and values[0].type == "python_stopped_premodel_commit_intent":
                # Actual concurrent state change after permanent readback and
                # before the final locked revalidation, not a fake-session mutation.
                async with database.session_factory() as session, session.begin():
                    row = await repository.get_for_update(session, record.id)
                    row.status = RunStatus.QUEUED.value
                    row.version += 1
            return values

    hook = PythonProjectTerminalHook(repository, Reader(), Executor())  # type: ignore[arg-type]
    hook._failure_archive_repository = Archives(database.session_factory)
    try:
        async with asyncio.timeout(12):
            assert await hook._retire_stopped_pre_model(tenant, record.id, cutoff) is not requeue_after_archive
        assert calls == (["inspect"] if requeue_after_archive else ["inspect", "commit"])
        async with database.session_factory() as session:
            actual_row = await session.get(RunRow, record.id)
            assert actual_row is not None and actual_row.routing_decision is not None
            actual = (actual_row.version, actual_row.status, actual_row.updated_at, actual_row.request,
                      dict(actual_row.routing_decision))
            if requeue_after_archive:
                assert actual[:2] == (before[0] + 1, "queued")
                assert actual[2] >= before[2] and actual[3:] == before[3:]
            else:
                assert actual == before
        # A second hook sees the durable intent and cannot replay a commit.
        again = PythonProjectTerminalHook(repository, Reader(), Executor())  # type: ignore[arg-type]
        async with asyncio.timeout(5):
            assert await again._retire_stopped_pre_model(tenant, record.id, cutoff) is False
        assert calls == (["inspect"] if requeue_after_archive else ["inspect", "commit"])
    finally:
        async with database.session_factory() as session, session.begin():
            row = await repository.get_for_update(session, record.id)
            row.status = RunStatus.CANCELLED.value
        await repository.delete_run(tenant, record.id)
        await database.dispose()
