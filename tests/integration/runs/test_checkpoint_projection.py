from __future__ import annotations

import json
from uuid import UUID, uuid4

from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunNotFound, RunRepository
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, RuntimeCheckpoint


async def test_persisted_checkpoint_privacy_and_tenant_boundaries(database_url: str) -> None:
    database = build_database(database_url)
    repository = RunRepository(database.session_factory)
    tenant, foreign_tenant = uuid4(), uuid4()
    run_ids: list[UUID] = []
    private_marker = "private-rejected-candidate-" + uuid4().hex
    public_text = "Accepted result remains available."
    try:
        record = await repository.create_run(
            tenant_id=tenant, actor_id=uuid4(), request="Verify checkpoint privacy",
            mode=TaskMode.DISPATCH, status=RunStatus.COMPLETED,
            idempotency_key=None, enqueue=False,
        )
        run_ids.append(record.id)
        checkpoint = RuntimeCheckpoint(
            id=uuid4(), runtime_type="crew.dispatch", runtime_version="7",
            run_id=record.id, tenant_id=tenant, mode=TaskMode.DISPATCH,
            state={"opaque_extension": {"private_text": private_marker}},
        )
        events = (
            RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=1, run_id=record.id,
                     checkpoint=checkpoint),
            RunEvent(kind=EventKind.MESSAGE_CREATED, sequence=2, run_id=record.id,
                     actor="worker", message=public_text),
            RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=3, run_id=record.id,
                     artifact=Artifact(id=uuid4(), type="text", producer="worker",
                                       content={"text": public_text})),
        )
        async with database.session_factory() as session, session.begin():
            for event in events:
                await repository.persist_event(
                    session, tenant_id=tenant, run_id=record.id, event=event,
                )

        public = await repository.events(tenant, record.id)
        assert public[0]["checkpoint"] is None
        summary = public[0]["checkpoint_summary"]
        assert isinstance(summary, dict)
        assert summary["state_sha256"] == checkpoint.state_sha256
        assert summary["runtime_type"] == "crew.dispatch"
        assert private_marker not in json.dumps(public, default=str)
        assert public[1]["message"] == public_text
        assert public_text in json.dumps(public[2], default=str)

        # A new repository/session must restore the stored bytes, not a public projection.
        restored_repository = RunRepository(database.session_factory)
        raw = await restored_repository.raw_events(tenant, record.id)
        assert tuple(event.to_payload() for event in raw) == tuple(
            event.to_payload() for event in events
        )
        async with database.session_factory() as session:
            restored = await restored_repository.latest_checkpoint(
                session, tenant_id=tenant, run_id=record.id,
            )
            assert restored is not None
            assert restored.to_payload() == checkpoint.to_payload()
            assert await restored_repository.latest_checkpoint(
                session, tenant_id=foreign_tenant, run_id=record.id,
            ) is None

        for read in (restored_repository.events, restored_repository.raw_events):
            try:
                await read(foreign_tenant, record.id)
            except RunNotFound:
                pass
            else:
                raise AssertionError("Cross-tenant checkpoint access was accepted")
        assert await repository.events(tenant, record.id) == public
    finally:
        try:
            for run_id in run_ids:
                await repository.delete_run(tenant, run_id)
        finally:
            await database.dispose()
