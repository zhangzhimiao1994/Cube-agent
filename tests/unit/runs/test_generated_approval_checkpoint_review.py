"""Generated approvals never inherit the original pre-model recovery exemption."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, _pre_model_original_capture
from agent_hub.runtime.contracts import RuntimeCheckpoint
from tests.unit.runs.test_python_project_approval_projection import dao as dao  # noqa: PLC0414


@pytest.mark.parametrize('key,value', [
    ('generated_project_contract_version', 1), ('generated_project_contract_version', True),
    ('generated_project_preparation', {}), ('generated_project_preparation', None),
])
@pytest.mark.parametrize('schema', [1, 2])
async def test_mixed_capture_never_skips_checkpoint_at_begin(
    dao: Any, key: str, value: object, schema: int,
) -> None:
    route = dao.run.routing_decision
    route[key] = value
    if schema == 1:
        route['project_input_preparation']['schema_version'] = 1
        route['project_input_preparation'].pop('python_recipe')
    record = await dao.repository.begin_capability_approval(dao.run.tenant_id, dao.run.id,
        actor_id=dao.run.actor_id, approval_id='approval_generated', approval_fingerprint='fingerprint')
    assert record.routing_decision['approval_checkpoint_required'] is True
    assert not _pre_model_original_capture(route)


@pytest.mark.parametrize('entrypoint', ['enqueue', 'resolve'])
@pytest.mark.parametrize('mixed', [False, True])
@pytest.mark.parametrize('flag', [None, False, True])
async def test_generated_approval_without_durable_receipt_cannot_resume(
    dao: Any, entrypoint: str, mixed: bool, flag: bool | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = dao.run.routing_decision
    route['generated_project_contract_version'] = 1
    if mixed:
        route['project_input_preparation']['schema_version'] = 1
        route['project_input_preparation'].pop('python_recipe')
    else:
        for name in ('project_input_contract_version', 'project_input', 'project_input_preparation'):
            route.pop(name)
    route.update(approval_kind='capability_tool', approval_id='approval_generated',
                 approval_fingerprint='fingerprint')
    if flag is not None:
        route['approval_checkpoint_required'] = flag
    dao.run.status = RunStatus.WAITING_APPROVAL.value
    before = deepcopy((dao.run.status, dao.run.version, route))
    ready = await dao.repository.capability_approval_ready(
        dao.run.tenant_id, dao.run.id, actor_id=dao.run.actor_id)
    upserts: list[object] = []

    async def upsert(*args: object, **kwargs: object) -> None:
        upserts.append(kwargs)

    monkeypatch.setattr(dao.repository, '_upsert_capability_approval', upsert)
    with pytest.raises(RunConflict, match='checkpoint'):
        if entrypoint == 'enqueue':
            await dao.repository.approve_capability_and_enqueue(tenant_id=dao.run.tenant_id, run_id=dao.run.id,
                actor_id=dao.run.actor_id, approval_id='approval_generated', version=dao.run.version)
        else:
            await dao.repository.resolve_capability_approval(dao.run.tenant_id, dao.run.id, RunStatus.RUNNING,
                approval_id='approval_generated', approval_fingerprint='fingerprint')
    assert not ready
    assert (dao.run.status, dao.run.version, dao.run.routing_decision) == before
    assert not dao.outbox and not dao.approvals and not upserts


async def test_generated_begin_without_worker_still_requires_durable_checkpoint(dao: Any) -> None:
    route = dao.run.routing_decision
    route['generated_project_contract_version'] = 1
    for name in ('project_input_contract_version', 'project_input', 'project_input_preparation'):
        route.pop(name)
    dao.run.worker_lease_token = None
    dao.run.worker_id = None
    record = await dao.repository.begin_capability_approval(dao.run.tenant_id, dao.run.id,
        actor_id=dao.run.actor_id, approval_id='approval_generated', approval_fingerprint='fingerprint')
    assert record.routing_decision['approval_checkpoint_required'] is True
    assert 'approval_checkpoint_worker_token' not in record.routing_decision


@pytest.mark.parametrize('entrypoint', ['enqueue', 'resolve'])
async def test_generated_matching_durable_receipt_resumes_normally(
    dao: Any, entrypoint: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = dao.run.routing_decision
    route['generated_project_contract_version'] = 1
    for name in ('project_input_contract_version', 'project_input', 'project_input_preparation'):
        route.pop(name)
    record = await dao.repository.begin_capability_approval(dao.run.tenant_id, dao.run.id,
        actor_id=dao.run.actor_id, approval_id='approval_generated', approval_fingerprint='fingerprint')
    assert record.routing_decision['approval_checkpoint_required'] is True
    checkpoint = RuntimeCheckpoint(id=uuid4(), tenant_id=dao.run.tenant_id, run_id=dao.run.id,
        runtime_type='crew', runtime_version='11', mode=TaskMode.DISPATCH,
        state={'tools': {'verify': {'status': 'waiting_approval', 'approval_id': 'approval_generated'}}})
    dao.connection.execute(text('INSERT INTO agent_hub_run_checkpoints (run_id,sequence) VALUES (:run,1)'),
                           {'run': dao.run.id.hex})

    async def latest(*args: object, **kwargs: object) -> RuntimeCheckpoint:
        return checkpoint

    monkeypatch.setattr(dao.repository, 'latest_checkpoint', latest)
    assert await dao.repository.capability_approval_ready(dao.run.tenant_id, dao.run.id, actor_id=dao.run.actor_id)
    if entrypoint == 'enqueue':
        after = await dao.repository.approve_capability_and_enqueue(tenant_id=dao.run.tenant_id, run_id=dao.run.id,
            actor_id=dao.run.actor_id, approval_id='approval_generated', version=dao.run.version)
        assert after.status is RunStatus.QUEUED and len(dao.outbox) == 1
    else:
        after = await dao.repository.resolve_capability_approval(dao.run.tenant_id, dao.run.id, RunStatus.RUNNING,
            approval_id='approval_generated', approval_fingerprint='fingerprint')
        assert after.status is RunStatus.RUNNING and not dao.outbox
    assert dao.approvals[0].status == 'approved'
