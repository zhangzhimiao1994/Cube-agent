"""Real SQLite diagnostic roundtrip and original RunService failure arguments."""

from collections.abc import Mapping
from typing import NoReturn, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.db.models import RunEventRow, RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunRecord, RunRepository
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.contracts import JsonValue, RunEvent, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionFailureDiagnostic
from tests.unit.capabilities.test_protected_original_resolution_authorization import (
    original as running_record,
)
from tests.unit.runs.test_crew_report_publication_red_oct29 import sql_repository
from tests.unit.runs.test_project_input_lifecycle import (
    ObservingRuntime,
    original_project_repository,
)


def leaf() -> ResolutionFailureDiagnostic:
    return ResolutionFailureDiagnostic(phase='registry_metadata', code='registry_unavailable',
                                       exit_code=2, timed_out=False)


async def test_real_sql_failure_roundtrip_ownership_and_terminal_immutability() -> None:
    record = running_record()
    context = TaskContext(run_id=record.id, tenant_id=record.tenant_id, request=record.request,
        mode=cast(TaskMode, record.mode), actor_id=record.actor_id, actor_role=record.actor_role, timeout_seconds=25.)
    async with sql_repository(context) as (repository, factory, _engine):
        async with factory.begin() as session:
            await session.execute(update(RunRow).where(RunRow.id == record.id).values(
                worker_id=record.worker_id, worker_lease_token=record.worker_lease_token))
        with pytest.raises(RunConflict, match='worker execution ownership changed'):
            await repository.fail_run(record.id, reason='project verification failed: resolution inspect only',
                worker_id='wrong-worker', worker_lease_token=uuid4(), resolution_failure=leaf())
        assert await repository.raw_events(record.tenant_id, record.id) == ()
        failed = await repository.fail_run(record.id,
            reason='project verification failed: resolution inspect only', worker_id=record.worker_id,
            worker_lease_token=record.worker_lease_token, resolution_failure=leaf())
        assert failed.status is RunStatus.FAILED and failed.worker_id == record.worker_id
        assert failed.worker_lease_token == record.worker_lease_token
        events = await repository.raw_events(record.tenant_id, record.id)
        assert len(events) == 1
        event = RunEvent.from_payload(events[0].to_payload())
        assert event.payload['resolution_failure'] == leaf().model_dump()
        assert event.payload['error_code'] == 'project_verification.resolution.registry_unavailable'
        assert event.payload['retryable'] is False
        async with factory() as session:
            row = await session.scalar(select(RunEventRow).where(RunEventRow.run_id == record.id))
            assert row is not None
            assert RunEvent.from_payload(dict(row.payload)).payload == event.payload
        await repository.fail_run(record.id, reason='must not replace terminal event',
            worker_id=record.worker_id, worker_lease_token=record.worker_lease_token,
            resolution_failure=leaf().model_copy(update={'code': 'internal_failure'}))
        assert await repository.raw_events(record.tenant_id, record.id) == events


async def test_actual_service_passes_only_closed_diagnostic_and_current_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = original_project_repository()
    original_fail = repository.fail_run
    seen: list[tuple[str, UUID]] = []
    model_calls: list[str] = []

    async def fail(
        run_id: UUID, *, reason: str, worker_id: str | None,
        worker_lease_token: UUID | None, resolution_failure: Mapping[str, JsonValue],
    ) -> RunRecord:
        assert run_id == repository.row.id
        assert worker_id == repository.row.worker_id and worker_id is not None
        assert worker_lease_token == repository.row.worker_lease_token and worker_lease_token is not None
        assert resolution_failure == leaf().model_dump()
        assert set(resolution_failure) == {'phase', 'code', 'exit_code', 'timed_out'}
        seen.append((worker_id, worker_lease_token))
        return await original_fail(run_id, reason=reason, worker_id=worker_id,
                                   worker_lease_token=worker_lease_token)

    async def prepare(context: TaskContext) -> NoReturn:
        raise ProjectVerificationError('project verification failed: resolution inspect only',
                                       resolution_failure=leaf())

    monkeypatch.setattr(repository, 'fail_run', fail)
    service = RunService(cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((ObservingRuntime(TaskMode.DISPATCH, model_calls),)),
        router=None, task_queue=cast(TaskQueue, object()), project_input_preparer=prepare)
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.FAILED and len(seen) == 1 and model_calls == []
    assert repository.row.worker_id is None and repository.row.worker_lease_token is None
