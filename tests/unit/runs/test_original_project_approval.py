from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, text

from agent_hub.domain.runs import RunStatus
from agent_hub.runs.repository import RunConflict, RunRepository
from tests.unit.runs.test_recovery_guard import (
    _CapabilityApprovalSession,
    _CapabilityApprovalSessionFactory,
    _RecoveryBlockingRepository,
)


@pytest.mark.parametrize('phase,version,expected', [
    ('captured', 1, False), ('observed', 1, True), ('executing', 1, True),
    ('captured', True, True), ('unknown', 1, True),
])
async def test_only_pre_model_captured_input_can_wait_without_runtime_checkpoint(
    phase: str, version: object, expected: bool,
) -> None:
    repository = _RecoveryBlockingRepository(status=RunStatus.RUNNING, routing_decision={
        'project_input_contract_version': version,
        'project_input': {'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['src']},
        'project_input_preparation': {'schema_version': 1, 'phase': phase,
            'reference': {'schema_version': 1, 'contract_sha256': 'a' * 64,
                          'input_source_sha256': 'b' * 64, 'file_count': 3}},
    }, blocked_after_sequence=0)
    row = repository.row
    row.worker_id, row.worker_lease_token = 'worker-current', repository.lease_token
    session = _CapabilityApprovalSession(row, approved=False)
    repository._session_factory = cast(Any, _CapabilityApprovalSessionFactory(session))
    record = await repository.begin_capability_approval(row.tenant_id, row.id,
        approval_id='approval-1', approval_fingerprint='fingerprint-1')
    assert record.status is RunStatus.WAITING_APPROVAL
    assert record.routing_decision is not None
    assert record.routing_decision['approval_checkpoint_required'] is expected


@pytest.mark.parametrize('phase,extra,ready', [
    ('captured', None, True), ('captured', 'context.loaded', True),
    ('captured', 'context.injected', False), ('captured', 'model.started', False),
    ('captured', 'tool.started', False), ('captured', 'tool.completed', False),
    ('captured', 'artifact.created', False), ('captured', 'runtime.failed', False),
    ('observed', None, False), ('executing', None, False), ('missing', None, False),
    ('invalid_reference', None, False), ('invalid_version', None, False),
    ('missing_required', None, False), ('required_true', None, False),
])
@pytest.mark.parametrize('entrypoint', ['enqueue', 'resolve'])
async def test_original_admission_readiness_and_approval_use_real_recovery_query(
    phase: str, extra: str | None, ready: bool, entrypoint: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    route: dict[str, object] = {
        'project_input_contract_version': 1,
        'project_input': {'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['src']},
        'project_input_preparation': {'schema_version': 1, 'phase': phase,
            'reference': {'schema_version': 1, 'contract_sha256': 'a' * 64,
                'input_source_sha256': 'b' * 64, 'file_count': 3}},
        'approval_kind': 'capability_tool', 'approval_id': 'approval-native',
        'approval_fingerprint': 'fingerprint-native', 'approval_checkpoint_required': False,
    }
    if phase in {'invalid_reference', 'invalid_version', 'missing_required', 'required_true'}:
        preparation = cast(dict[str, Any], route['project_input_preparation'])
        preparation['phase'] = 'captured'
        if phase == 'invalid_reference':
            preparation['reference']['contract_sha256'] = 'invalid'
        elif phase == 'invalid_version':
            route['project_input_contract_version'] = True
        elif phase == 'missing_required':
            route.pop('approval_checkpoint_required')
        else:
            route['approval_checkpoint_required'] = True
    row = _RecoveryBlockingRepository(status=RunStatus.WAITING_APPROVAL,
        routing_decision=route, blocked_after_sequence=0).row
    engine = create_engine('sqlite:///:memory:')
    added: list[object] = []
    with engine.connect() as connection:
        connection.execute(text('CREATE TABLE agent_hub_run_checkpoints ('
            'run_id CHAR(32), sequence INTEGER, id CHAR(32), tenant_id CHAR(32), '
            'runtime_type TEXT, runtime_version TEXT, mode TEXT, state TEXT, '
            'state_sha256 TEXT, payload TEXT, created_at TEXT)'))
        connection.execute(text('CREATE TABLE agent_hub_run_events (run_id CHAR(32), sequence INTEGER, kind TEXT)'))
        tail = [(1, 'context.loaded'), (2, 'harness.started')]
        if extra is not None:
            tail.append((3, extra))
        connection.execute(text('INSERT INTO agent_hub_run_events VALUES (:id,:sequence,:kind)'),
            [{'id': row.id.hex, 'sequence': sequence, 'kind': kind} for sequence, kind in tail])

        class Session:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

            def begin(self) -> Any:
                return self

            async def scalar(self, statement: Any) -> Any:
                tables = {item.name for item in statement.get_final_froms()}
                return row if tables == {'agent_hub_runs'} else connection.scalar(statement)

            async def flush(self) -> None:
                pass

            def add(self, item: object) -> None:
                added.append(item)

        repository = RunRepository(cast(Any, lambda: Session()))
        assert row.actor_id is not None
        assert await repository.capability_approval_ready(row.tenant_id, row.id, actor_id=row.actor_id) is ready
        upserts: list[object] = []

        async def upsert(*args: object, **kwargs: object) -> None:
            assert kwargs['status'] == 'approved'
            upserts.append(kwargs)

        monkeypatch.setattr(repository, '_upsert_capability_approval', upsert)

        async def approve() -> Any:
            if entrypoint == 'enqueue':
                return await repository.approve_capability_and_enqueue(tenant_id=row.tenant_id,
                    run_id=row.id, actor_id=row.actor_id, approval_id='approval-native', version=row.version)
            return await repository.resolve_capability_approval(row.tenant_id, row.id, RunStatus.RUNNING,
                approval_id='approval-native', approval_fingerprint='fingerprint-native')

        if ready:
            result = await approve()
            assert result.status is (RunStatus.QUEUED if entrypoint == 'enqueue' else RunStatus.RUNNING)
            assert len(added) == (1 if entrypoint == 'enqueue' else 0) and len(upserts) == 1
        elif entrypoint == 'enqueue' or phase in {'captured', 'required_true'}:
            before = deepcopy((row.status, row.version, row.routing_decision))
            with pytest.raises(RunConflict):
                await approve()
            assert (row.status, row.version, row.routing_decision) == before
            assert not added and not upserts
        else:
            result = await approve()
            assert result.status is RunStatus.RUNNING and not added and len(upserts) == 1
    engine.dispose()
