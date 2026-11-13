"""Actual Python failure -> live worker evidence -> SQLite diagnostic ACK.

The original C6 RED logs remain in .tmp/c6-approval-recovery-source-ohm-nov13.
No validator is patched and no model/native/Root process is invoked.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from autogen_core import CancellationToken
from sqlalchemy import delete

from agent_hub.auth.models import Role
from agent_hub.db.models import RunRow
from agent_hub.runs.repository import RunConflict
from agent_hub.runtime.autogen.engineering_delivery import engineering_work_progress
from agent_hub.runtime.autogen.project_repair_phase import (
    authority_digest,
    prepare,
    validate_diagnostic_transition,
)
from agent_hub.runtime.contracts import Artifact, RuntimeCheckpoint
from tests.unit.runs.test_diagnostic_checkpoint_transition import (
    Checkpoints,
)
from tests.unit.runs.test_diagnostic_checkpoint_transition import (
    db as _sqlite_db_fixture,
)
from tests.unit.runtime.autogen.test_original_python_failure_continuation_c6 import (
    TOOLS,
    actual_chain,
)

# Reuse the existing fixture unchanged, without shadowing an unused import.
db = _sqlite_db_fixture


def mutable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=dict))


async def sqlite_case(tmp_path: Path, db: Checkpoints, *, phase: bool = False) -> Any:
    context, durable, failure, _executor, tool = await actual_chain(tmp_path)
    route = mutable(context.routing_decision)
    history = route.pop('python_project_history')
    route.update(engineering_diagnostic_allowance_version=1,
        engineering_progress_budget_version=1, engineering_checkpoint_ack_version=1,
        engineering_soft_admission_version=1, engineering_auto_repair_version=1,
        runtime_timeout_source='project_scale_soft_budget', runtime_timeout_seconds=3600.0,
        runtime_timeout_soft_seconds=900.0, runtime_timeout_absolute_seconds=3600.0,
        runtime_plan_token_budget=10_000_000, runtime_token_absolute_tokens=10_000_000)
    assert context.actor_id is not None
    db.run, db.tenant, db.actor = context.run_id, context.tenant_id, context.actor_id

    def checkpoint(rows: Any, artifacts: Any, soft: float, absolute: float,
                   prefix: int = 0, repair_phase: Any = None) -> RuntimeCheckpoint:
        additions = {} if repair_phase is None else {'project_repair_phase': repair_phase}
        return db.checkpoint(soft=soft, watermark=0, remaining_absolute_timeout_seconds=absolute,
            artifacts=artifacts, tool_ledger=rows, terminal=False,
            engineering_tool_prefix=prefix, **additions)

    source_rows = tuple(dict(row) for row in durable.tool_entries)
    source_artifacts = durable.artifacts
    repair_phase = None
    if phase:
        source = checkpoint(source_rows, source_artifacts, 309.0, 3403.0, len(source_rows))
        proposal = {'kind': 'self_repair', 'requires_approval': False, 'automatic_execution': True,
            'source_run_id': str(context.run_id), 'failure_kind': 'tool_failure',
            'automatic_checkpoint_sha256': source.state_sha256,
            'automatic_scope_sha256': authority_digest(route), 'fingerprint': 'a'*64, 'attempt': 1, 'max_attempts': 1}
        route.update(source='self_repair', self_repair_accepted=True, self_repair_attempt=1,
            self_repair_max_attempts=1, self_repair_source_run_id=str(context.run_id), repair_proposal=proposal)
        authority = context.model_copy(update={'routing_decision': route, 'artifacts': ()}).validated_internal_clone()
        repair_phase = prepare(authority, source, (), source_rows)
    await tool('workspace.write_text').run_json(
        {'path': 'src/billing/__init__.py', 'content': 'def total(): return 12\n'},
        CancellationToken(), call_id='local-current-repair-write')
    completed = [dict(row) for row in durable.tool_entries]
    running = [dict(row) for row in completed]
    running[-1]['status'] = 'running'
    for key in ('artifact_id', 'artifact_sha256'):
        running[-1].pop(key)
    previous = checkpoint(running, source_artifacts, 309.0, 3403.0,
        len(source_rows) if phase else 0, repair_phase)
    candidate = checkpoint(completed, durable.artifacts, 900.0, 3402.0,
        len(completed) if phase else 0, repair_phase)
    worker = context.model_copy(update={'routing_decision': {**route, 'python_project_history': history}}).validated_internal_clone()
    authority = context.model_copy(update={'routing_decision': route, 'artifacts': ()}).validated_internal_clone()
    assert authority_digest(authority.routing_decision) != authority_digest(worker.routing_decision)
    before = engineering_work_progress(worker, tool_entries=running,
        artifacts=durable.artifacts, allowed_tools=TOOLS)
    after = engineering_work_progress(worker, tool_entries=completed,
        artifacts=durable.artifacts, allowed_tools=TOOLS)
    assert after - before
    # The authority controls phase digest and budgets; only evidence uses worker.
    validate_diagnostic_transition(authority, previous, candidate, durable.artifacts,
        progress_context=worker)
    with pytest.raises(ValueError):
        validate_diagnostic_transition(authority, previous, candidate, durable.artifacts)
    async with db.factory.begin() as session:
        await session.execute(delete(RunRow))
        session.add(RunRow(id=context.run_id, tenant_id=context.tenant_id, actor_id=context.actor_id,
            actor_role='operator', request=context.request, mode='discuss', status='running',
            routing_decision=route, version=1, worker_id='c6-local-current-worker',
            worker_lease_token=worker.execution_token,
            worker_lease_expires_at=datetime.now(UTC)+timedelta(minutes=5)))
    db.route = route
    # C6: preparer baseline artifacts are in the trusted worker, not private DB.
    for artifact in durable.artifacts:
        await db.private(artifact, permanent=True)
    await db.seed(previous, 82)
    return SimpleNamespace(worker=worker, authority=authority, previous=previous,
        candidate=candidate, artifacts=durable.artifacts, failure=failure, phase=repair_phase)


async def persist(db: Any, case: Any, *, worker: Any = ...) -> None:
    selected = case.worker if worker is ... else worker
    async with db.factory.begin() as session:
        await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
            event=db.event(case.candidate, 83), routing_decision=case.worker.routing_decision,
            diagnostic_context=selected)


@pytest.mark.parametrize('phase', [False, True])
async def test_running_python_write_ack_uses_worker_evidence_and_db_authority(
    tmp_path: Path, db: Any, phase: bool,
) -> None:
    case = await sqlite_case(tmp_path, db, phase=phase)
    if phase:
        assert case.phase['source_scope_sha256'] == authority_digest(case.authority.routing_decision)
        assert case.phase['source_scope_sha256'] != authority_digest(case.worker.routing_decision)
    await persist(db, case)
    async with db.factory() as session:
        saved = await db.repository.latest_checkpoint(session, tenant_id=db.tenant, run_id=db.run)
        row = await session.get(RunRow, db.run)
    assert saved is not None and saved.id == case.candidate.id
    assert saved.state['remaining_timeout_seconds'] == 900.0
    assert saved.state['tool_ledger'][-1]['status'] == 'succeeded'
    assert saved.state_sha256 == case.candidate.state_sha256
    assert row is not None and 'python_project_history' not in row.routing_decision
    assert await db.counts() == (2, 2)


@pytest.mark.parametrize('defect', [
    'missing_context', 'actor', 'role', 'request', 'scope', 'stale_lease', 'token',
    'preparation_delta', 'missing_baseline', 'forged_history', 'forged_metadata',
    'artifact_hash', 'budget', 'UNKNOWN',
])
async def test_worker_evidence_drift_and_unknown_cannot_grant_window(
    tmp_path: Path, db: Any, defect: str,
) -> None:
    case = await sqlite_case(tmp_path, db)
    worker = case.worker
    if defect == 'missing_context':
        worker = None
    elif defect in {'actor', 'role', 'request', 'token'}:
        key, value = {'actor': ('actor_id', uuid4()), 'role': ('actor_role', Role.VIEWER),
            'request': ('request', worker.request+' changed'), 'token': ('execution_token', uuid4())}[defect]
        worker = worker.model_copy(update={key: value}).validated_internal_clone()
    elif defect in {'scope', 'forged_history'}:
        route = mutable(worker.routing_decision)
        if defect == 'scope':
            route['workspace_session_id'] = 'foreign-session'
        else:
            route['python_project_history']['artifact_sha256'] = '0'*64
        worker = worker.model_copy(update={'routing_decision': route}).validated_internal_clone()
    elif defect in {'stale_lease', 'preparation_delta'}:
        async with db.factory.begin() as session:
            row = await session.get(RunRow, db.run)
            assert row is not None
            if defect == 'stale_lease':
                row.worker_lease_expires_at = datetime.now(UTC)-timedelta(seconds=1)
            else:
                route = mutable(row.routing_decision)
                route['project_input_preparation']['execution_id'] = 'foreign-original'
                row.routing_decision = route
    elif defect == 'missing_baseline':
        worker = worker.model_copy(update={'artifacts': tuple(a for a in worker.artifacts
            if a.type != 'original_project_observation')}).validated_internal_clone()
    elif defect == 'forged_metadata':
        artifacts = list(worker.artifacts)
        old = next(a for a in artifacts if a.type == 'original_python_project_context')
        content = mutable(old.content)
        content['expected_contract_sha256'] = '0'*64
        replacement = Artifact(id=old.id, type=old.type, producer=old.producer,
            source_ids=old.source_ids, content=content)
        worker = worker.model_copy(update={'artifacts': tuple(replacement if a.id == old.id else a
            for a in artifacts)}).validated_internal_clone()
    else:
        state = mutable(case.candidate.state)
        if defect == 'artifact_hash':
            state['tool_ledger'][-1]['artifact_sha256'] = '0'*64
        elif defect == 'budget':
            state['remaining_timeout_seconds'] = 901.0
        else:
            state['tool_ledger'][-1]['status'] = 'running'
            state['tool_ledger'][-1].pop('artifact_id')
            state['tool_ledger'][-1].pop('artifact_sha256')
        case.candidate = RuntimeCheckpoint(id=case.candidate.id, runtime_type='autogen', runtime_version='2',
            tenant_id=db.tenant, run_id=db.run, mode=case.candidate.mode, state=state)
    with pytest.raises(RunConflict):
        await persist(db, case, worker=worker)
    assert await db.counts() == (1, 1)

@pytest.mark.parametrize('key,value', [
    ('requested_permissions', ['workspace.read', 'workspace.write', 'command.run', 'host.write']),
    ('sandbox_profile', 'unrestricted'),
    ('runtime_token_absolute_tokens', 20_000_000),
    ('unknown_future_authority_field', 'must-bind-by-default'),
])
async def test_phase_progress_view_cannot_change_nonhistory_authority(
    tmp_path: Path, db: Any, key: str, value: Any,
) -> None:
    case = await sqlite_case(tmp_path, db, phase=True)
    original_digest = authority_digest(case.authority.routing_decision)
    assert case.phase['source_scope_sha256'] == original_digest
    assert original_digest != authority_digest(case.worker.routing_decision)
    route = mutable(case.worker.routing_decision)
    route[key] = value
    forged_view = case.worker.model_copy(update={'routing_decision': route}).validated_internal_clone()
    assert forged_view.artifacts == case.worker.artifacts
    with pytest.raises(ValueError):
        validate_diagnostic_transition(case.authority, case.previous, case.candidate,
            case.artifacts, progress_context=forged_view)
    assert authority_digest(case.authority.routing_decision) == original_digest
    assert case.phase['source_scope_sha256'] == original_digest
