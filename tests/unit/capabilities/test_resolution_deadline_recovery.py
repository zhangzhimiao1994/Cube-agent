"""A new worker must retain the exact first resolution deadline and grant."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import text

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.generated_project_service import GeneratedProjectService
from agent_hub.capabilities.python_resolution_preparation import PythonResolutionPreparation
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.db.models import RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.contracts import Artifact, _freeze_object
from agent_hub.skills.sandbox import python_resolution_worker as wire
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionClaim,
    ResolutionRequest,
    ResolutionStatus,
)
from tests.integration.runtime.test_resolution_approval_sql import approval_checkpoint
from tests.unit.capabilities.test_resolution_harness_approval_nov01 import (
    Case,
    approve_pending,
    configured,
)
from tests.unit.capabilities.test_resolution_harness_approval_nov01 import (
    dao as dao,  # noqa: PLC0414
)
from tests.unit.capabilities.test_resolution_harness_approval_nov01 import (
    offline_network as offline_network,  # noqa: PLC0414
)


async def invoke_worker(case: Case, worker: str) -> HarnessToolCallResult:
    with bind_execution_authority(case.context, worker_id=worker, actor='engineer'):
        return await case.harness.invoke(case.context.tenant_id, case.request,
            user_id=case.context.actor_id, role=Role.OPERATOR)


def capture_claims(monkeypatch: pytest.MonkeyPatch) -> list[ResolutionClaim]:
    original_transport = wire.request_resolution
    claims: list[ResolutionClaim] = []

    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        claims.append(request.claim)
        return await original_transport(request)

    monkeypatch.setattr(wire, 'request_resolution', transport)
    return claims


async def approve_mode_pending(case: Case, waiting: HarnessToolCallResult) -> None:
    if case.context.mode is TaskMode.DIRECT:
        await approve_pending(case, waiting)
        return
    approval_id = waiting.payload['approval_id']
    assert isinstance(approval_id, str)
    checkpoint = approval_checkpoint(case.context.tenant_id, case.context.run_id, case.context.mode, approval_id)
    case.dao.connection.execute(text('INSERT INTO agent_hub_run_checkpoints '
        '(run_id,sequence,id,tenant_id,runtime_type,runtime_version,mode,state,state_sha256,payload) '
        'VALUES (:run,:sequence,:id,:tenant,:kind,:version,:mode,:state,:sha,:payload)'),
        {'run': case.context.run_id.hex, 'sequence': 100 + case.dao.run.version,
         'id': checkpoint.id.hex, 'tenant': case.context.tenant_id.hex,
         'kind': checkpoint.runtime_type, 'version': checkpoint.runtime_version, 'mode': checkpoint.mode.value,
         'state': json.dumps(checkpoint.to_payload()['state']), 'sha': checkpoint.state_sha256,
         'payload': json.dumps(checkpoint.to_payload())})
    await case.dao.repository.approve_capability_and_enqueue(tenant_id=case.context.tenant_id,
        run_id=case.context.run_id, actor_id=case.context.actor_id,
        approval_id=approval_id, version=case.dao.run.version)


def resume_worker(case: Case, *, remaining: float, worker: str, cold: bool) -> None:
    row = case.dao.run
    assert isinstance(row, RunRow)
    token = uuid4()
    row.status, row.worker_id = 'running', worker
    row.worker_lease_token = token
    row.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=3)
    case.context = case.context.model_copy(update={'execution_token': token, 'timeout_seconds': remaining})
    if cold:
        previous = case.service
        journal = previous._resolution_journal
        assert journal is not None
        fresh = GeneratedProjectService(root=previous.root, repository=previous.repository,
            dependency_inspector=previous._dependency_inspector, metadata_client=previous._metadata_client)
        fresh.configure_resolution_artifacts(journal.artifacts)
        runtime = case.harness._backend
        assert isinstance(runtime, RuntimeCapabilityGateway) and runtime._project_verification is not None
        runtime._project_verification.generated = fresh
        case.service = fresh


@pytest.mark.parametrize('cold', [False, True], ids=['warm-data-fresh-worker', 'cold-worker'])
@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize('charged', [0.000137, 272.140273], ids=['submillisecond', 'approval-wait'])
async def test_specific_approval_retains_nonrounded_deadline_across_repeated_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: object, cold: bool, mode: TaskMode, charged: float,
) -> None:
    case = await configured(tmp_path, monkeypatch, dao, mode=mode)
    claims = capture_claims(monkeypatch)
    row = case.dao.run
    assert isinstance(row, RunRow)
    row.created_at = datetime.now(UTC) - timedelta(seconds=300)  # Earlier model/approval time remains charged.
    assert row.routing_decision is not None
    row.routing_decision = {**row.routing_decision, 'runtime_timeout_seconds': 3600}
    case.context = case.context.model_copy(update={'timeout_seconds': 3599.865685,
        'routing_decision': row.routing_decision})
    waiting = await invoke_worker(case, 'resolution-worker')
    assert waiting.failure_reason == 'capability requires approval'
    proposal = case.policy.requests[-1]
    deadline = proposal.arguments['original_deadline']
    assert isinstance(deadline, str)
    assert deadline == (row.created_at + timedelta(seconds=3599.865685)).isoformat()
    assert deadline != (row.created_at + timedelta(seconds=3600)).isoformat()
    journal = case.service._resolution_journal
    assert journal is not None
    saved = await journal.read_artifacts(case.context.tenant_id, case.context.run_id,
        artifact_type='python_resolution_checkpoint', producer='python_resolution')
    assert len(saved) == 1
    assert saved[0].content['pending_plan_sha256'] is None
    await approve_mode_pending(case, waiting)
    assert len(case.dao.approvals) == 1
    permissions = tuple(cast(list[str], row.routing_decision['requested_permissions']))
    for index, remaining in enumerate((3599.865685 - charged, 65.0)):
        worker = f'resolution-resumed-{index}'
        resume_worker(case, remaining=remaining, worker=worker, cold=cold)
        before = len(claims)
        started = time.monotonic()
        result = await invoke_worker(case, worker)
        # Real declaration validation follows resolution; an old unpinned lock
        # needs model repair. A checkpoint mismatch or repeated ASK is a defect.
        assert result.failure_reason == 'generated project draft requires correction', result
        original = case.native['original']
        assert isinstance(original, ResolutionClaim)
        assert original.original_deadline.isoformat() == deadline
        assert case.calls.count('resolve_python_inputs') == 1
        assert case.calls.count('publish_python_resolution') == 0
        assert len(case.dao.approvals) == 1
        assert row.routing_decision['requested_permissions'] == list(permissions)
        assert 'generated_project_preparation' not in row.routing_decision
        assert len(claims) > before
        for claim in claims[before:]:
            assert claim.original_deadline.isoformat() == deadline
            assert claim.worker_id == worker and claim.lease_token == case.context.execution_token
            assert claim.monotonic_deadline <= started + remaining + 5  # Conservative runtime overhead only.
        records = await journal.read_artifacts(case.context.tenant_id, case.context.run_id,
            artifact_type='python_resolution_checkpoint', producer='python_resolution')
        for artifact in records:
            scope = artifact.content['scope']
            assert isinstance(scope, Mapping)
            assert scope['original_deadline'] == deadline


@pytest.mark.parametrize('defect', ['naive', 'expired', 'extended'])
async def test_retained_deadline_cannot_extend_or_replace_live_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: object, defect: str,
) -> None:
    case = await configured(tmp_path, monkeypatch, dao)
    claims = capture_claims(monkeypatch)
    assert (await invoke_worker(case, 'resolution-worker')).failure_reason == 'capability requires approval'
    assert claims
    original = claims[0]
    invalid = {'naive': original.original_deadline.replace(tzinfo=None),
        'expired': datetime.now(UTC) - timedelta(seconds=1),
        'extended': original.original_deadline + timedelta(seconds=1)}[defect]

    async def current() -> RunRecord:
        record = await case.service._record(case.context.tenant_id, case.context.run_id)
        assert isinstance(record, RunRecord)
        return record

    client = PythonResolutionPreparation(current=current, approved=None, original_deadline=invalid)
    before = list(case.calls)
    resume_worker(case, remaining=65, worker='bound-validation-worker', cold=False)
    with (bind_execution_authority(case.context, worker_id='bound-validation-worker', actor='engineer'),
          pytest.raises(ValueError, match='resolution_deadline')):
        await client._claim(original.planning)
    assert case.calls == before


@pytest.mark.parametrize('field', ['actor', 'root_sha256', 'catalog_sha256', 'original_deadline'])
async def test_registered_conflicting_history_cannot_select_a_new_deadline_or_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: object, field: str,
) -> None:
    case = await configured(tmp_path, monkeypatch, dao)
    waiting = await invoke_worker(case, 'resolution-worker')
    await approve_pending(case, waiting)
    journal = case.service._resolution_journal
    assert journal is not None
    records = await journal.read_artifacts(case.context.tenant_id, case.context.run_id,
        artifact_type='python_resolution_checkpoint', producer='python_resolution')
    content = json.loads(json.dumps(dict(records[0].content), default=dict))
    if field == 'original_deadline':
        content['scope'][field] = (datetime.fromisoformat(content['scope'][field]) + timedelta(microseconds=1)).isoformat()
    else:
        content['scope'][field] = 'neighbor' if field == 'actor' else 'f' * 64
    await journal.artifacts.put(case.context.tenant_id, case.context.run_id, Artifact(id=uuid4(), version=2,
        type='python_resolution_checkpoint', producer='python_resolution',
        content=_freeze_object(content, name='conflicting registered test checkpoint')))
    before = list(case.calls)
    resume_worker(case, remaining=65, worker='fresh-history-worker', cold=True)
    result = await invoke_worker(case, 'fresh-history-worker')
    assert result.status == 'failed'
    # Catalog projection may reject the conflicting actor before prepare is
    # entered; either existing finite gate must leave resolution IO untouched.
    assert result.failure_reason in {'resolution_checkpoint_scope_changed',
        'generated_acceptance_unavailable_or_changed', 'capability authorization unavailable'}
    assert case.calls == before and len(case.dao.approvals) == 1
    assert 'generated_project_preparation' not in case.dao.run.routing_decision
