from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.self_repair import (
    SelfRepairPolicy,
    classify_terminal_run,
    project_repair_checkpoint_is_safe,
    project_repair_scope_sha256,
)
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint


def evidence() -> tuple[dict[str, object], RuntimeCheckpoint]:
    from agent_hub.runs.workspace import workspace_selection

    route: dict[str, object] = {
        **workspace_selection(project_id='project', session_id='main',
                              sandbox_profile='workspace_write').routing_payload(),
        'engineering_auto_repair_version': 1,
        'engineering_delivery_contract_version': 1,
        'engineering_checkpoint_ack_version': 1,
        'runtime_plan_token_budget': 1000,
        'runtime_token_absolute_tokens': 1000,
        'runtime_timeout_absolute_seconds': 3600,
    }
    artifact_id = str(uuid4())
    checkpoint = RuntimeCheckpoint(
        id=uuid4(), run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISCUSS,
        runtime_type='autogen', runtime_version='2', state={
            'terminal': False, 'engineering_checkpoint_ack_version': 1,
            'remaining_timeout_seconds': 100,
            'remaining_absolute_timeout_seconds': 500,
            'usage': {'tokens': 100, 'cost_usd': '0.1'},
            'artifact_registry': {artifact_id: 'a' * 64},
            'model_ledger': [],
            'tool_ledger': [{'status': 'failed', 'artifact_id': artifact_id,
                             'artifact_sha256': 'a' * 64}],
        },
    )
    return route, checkpoint


def safe(route: dict[str, object], cp: RuntimeCheckpoint) -> bool:
    return project_repair_checkpoint_is_safe(
        cp, routing_decision=route, run_id=cp.run_id, tenant_id=cp.tenant_id, mode=cp.mode,
    )


def changed(cp: RuntimeCheckpoint, **fields: object) -> RuntimeCheckpoint:
    state = cp.to_payload()['state']
    state.update(fields)
    return RuntimeCheckpoint(
        id=cp.id, run_id=cp.run_id, tenant_id=cp.tenant_id, mode=cp.mode,
        runtime_type=cp.runtime_type, runtime_version=cp.runtime_version, state=state,
    )


def test_known_project_failure_can_continue_from_existing_budget() -> None:
    route, cp = evidence()
    assert safe(route, cp)
    assert SelfRepairPolicy().requires_approval is True


@pytest.mark.parametrize('defect', ['', 'unknown', 'unaccounted', 'wrong_scope', 'disabled'])
def test_initial_repair_retains_accounted_rejection_correction_chain(defect: str) -> None:
    from tests.unit.runtime.autogen.test_model_tool_correction import pair
    route, cp = evidence()
    route['engineering_tool_correction_version'] = 1
    rows = pair()
    rows[0]['rejection'].update(run_id=str(cp.run_id), tenant_id=str(cp.tenant_id))
    if defect == 'unknown':
        rows[1]['status'] = 'running'
    elif defect == 'wrong_scope':
        rows[0]['rejection']['run_id'] = str(uuid4())
    elif defect == 'disabled':
        route.pop('engineering_tool_correction_version')
    registry = {**cp.state['artifact_registry'], rows[1]['artifact_id']: rows[1]['artifact_sha256']}
    cp = changed(cp, model_ledger=rows, artifact_registry=registry,
                 usage={'tokens': 0 if defect == 'unaccounted' else 100, 'cost_usd': '0.1'})
    assert safe(route, cp) is (not defect)


def test_real_routing_uses_workspace_session_id_not_browser_session_id() -> None:
    route, cp = evidence()
    assert 'session_id' not in route
    assert safe(route, cp)
    route['session_id'] = route.pop('workspace_session_id')
    assert not safe(route, cp)


@pytest.mark.parametrize('seconds,tokens', [(200, 200), (7200, 20_000_000)])
def test_existing_hard_limits_are_dynamic_not_acceptance_sample_limits(seconds: int, tokens: int) -> None:
    route, cp = evidence()
    route.update(runtime_plan_token_budget=tokens, runtime_token_absolute_tokens=tokens,
                 runtime_timeout_absolute_seconds=seconds)
    cp = changed(cp, remaining_absolute_timeout_seconds=seconds,
                 remaining_timeout_seconds=seconds / 2)
    assert safe(route, cp)
    assert not safe(route, changed(cp, remaining_absolute_timeout_seconds=seconds + 1))


@pytest.mark.parametrize('key,value', [
    ('engineering_auto_repair_version', None),
    ('engineering_auto_repair_version', True),
    ('sandbox_profile', 'full_access'),
    ('runtime_plan_token_budget', 0),
    ('runtime_plan_token_budget', 1001),
    ('runtime_timeout_absolute_seconds', 0),
    ('project_id', ''),
    ('self_repair_accepted', True),
])
def test_legacy_or_invalid_authority_cannot_be_promoted(key: str, value: object) -> None:
    route, cp = evidence()
    route[key] = value
    assert not safe(route, cp)


@pytest.mark.parametrize('patch', [
    {'terminal': True},
    {'remaining_timeout_seconds': 0},
    {'remaining_absolute_timeout_seconds': 0},
    {'remaining_timeout_seconds': True},
    {'remaining_timeout_seconds': 501},
    {'usage': {'tokens': 1000, 'cost_usd': '0.1'}},
    {'usage': {'tokens': -1, 'cost_usd': '0.1'}},
    {'usage': {'tokens': 100, 'cost_usd': 'NaN'}},
    {'usage': {'tokens': 100, 'cost_usd': '-1'}},
    {'artifact_registry': {}},
    {'approval_batch': {}},
    {'engineering_checkpoint_ack_version': True},
    {'tool_ledger': []},
    {'model_ledger': [{'status': 'running'}]},
    {'model_ledger': [{'status': 'rejected'}]},
    {'tool_ledger': [{'status': 'waiting_approval'}]},
    {'tool_ledger': [{'status': 'running', 'replay_safe': True}]},
    {'tool_ledger': [{'status': 'UNKNOWN'}]},
])
def test_stopped_checkpoint_and_accounting_are_mandatory(patch: dict[str, object]) -> None:
    route, cp = evidence()
    assert not safe(route, changed(cp, **patch))


def test_checkpoint_scope_and_digest_must_match() -> None:
    route, cp = evidence()
    assert not project_repair_checkpoint_is_safe(
        cp, routing_decision=route, run_id=uuid4(), tenant_id=cp.tenant_id, mode=cp.mode,
    )
    assert not safe(route, cp.model_copy(update={'state_sha256': 'b' * 64}))


def test_project_policy_has_an_independent_kill_switch() -> None:
    assert SelfRepairPolicy(automatic_project_repairs=False).automatic_project_repairs is False


def decision(cp: RuntimeCheckpoint, reason: str = 'project build failed'):
    result = classify_terminal_run(
        status=RunStatus.FAILED, mode=cp.mode, routing_decision={},
        policy=SelfRepairPolicy(), events=[RunEvent(
                run_id=cp.run_id, sequence=1, kind=EventKind.STEP_FAILED, reason=reason,
                step_id='build', actor='implementer',
        )],
    )
    assert result is not None
    return result


@pytest.mark.asyncio
async def test_service_promotes_only_current_new_project_with_stopped_worker() -> None:
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime

    from agent_hub.runs.repository import RunRecord
    from agent_hub.runs.service import RunService

    route, cp = evidence()
    record = RunRecord(
        id=cp.run_id, tenant_id=cp.tenant_id, actor_id=uuid4(), request='build',
        mode=cp.mode, status=RunStatus.FAILED, version=4, created_at=datetime.now(UTC),
        routing_decision=route,
    )

    @asynccontextmanager
    async def transaction():
        class Session:
            def begin(self):
                return transaction()
        yield Session()

    repo = AsyncMock()
    repo.get.return_value = record
    repo.latest_checkpoint.return_value = cp
    repo.run_transaction.side_effect = lambda: transaction()
    service = object.__new__(RunService)
    service._repository = repo
    service._self_repair_policy = SelfRepairPolicy()
    original = decision(cp)
    promoted = await service._safe_project_repair_decision(
        tenant_id=cp.tenant_id, run_id=cp.run_id, decision=original,
    )
    assert promoted.requires_approval is False
    assert promoted.automatic_execution is True
    assert promoted.automatic_checkpoint_sha256 == cp.state_sha256
    proposal = promoted.to_proposal(run_id=cp.run_id)
    assert proposal['automatic_checkpoint_sha256'] == cp.state_sha256
    assert promoted.with_source_sequence(3).automatic_checkpoint_sha256 == cp.state_sha256

    for record_patch in (
        {'worker_id': 'active'}, {'worker_heartbeat_at': datetime.now(UTC)},
        {'worker_lease_token': uuid4()}, {'worker_lease_expires_at': datetime.now(UTC)},
        {'status': RunStatus.RUNNING}, {'blocked_by_run_id': uuid4()},
        {'routing_decision': {}},
    ):
        repo.get.return_value = replace(record, **record_patch)
        result = await service._safe_project_repair_decision(
            tenant_id=cp.tenant_id, run_id=cp.run_id, decision=original,
        )
        if record_patch == {'routing_decision': {}}:
            assert result == original
        else:
            assert result.kind == 'repair.skipped'
            assert result.to_proposal(run_id=cp.run_id) is None
    repo.get.return_value = record
    service._self_repair_policy = SelfRepairPolicy(automatic_project_repairs=False)
    result = await service._safe_project_repair_decision(
        tenant_id=cp.tenant_id, run_id=cp.run_id, decision=original,
    )
    assert result.kind == 'repair.skipped'
    assert result.skip_reason == 'project_auto_repair_disabled'


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', [
    'model_outcome_uncertain', 'plugin.credential_unavailable',
    'model.provider_quota_or_billing_failed', 'plugin.sandbox_unsupported',
    'model capacity unavailable',
])
async def test_infrastructure_or_uncertain_failure_is_not_project_auto_repair(reason: str) -> None:
    from agent_hub.runs.service import RunService

    _, cp = evidence()
    original = decision(cp, reason)
    service = object.__new__(RunService)
    service._repository = AsyncMock()
    from types import SimpleNamespace
    service._repository.get.return_value = SimpleNamespace(
        routing_decision={'engineering_auto_repair_version': 1},
    )
    service._self_repair_policy = SelfRepairPolicy()
    result = await service._safe_project_repair_decision(
        tenant_id=cp.tenant_id, run_id=cp.run_id, decision=original,
    )
    assert result.kind == 'repair.skipped'
    assert result.to_proposal(run_id=cp.run_id) is None
    service._repository.latest_checkpoint.assert_not_called()


def repository_fixture(seconds: int = 3600, tokens: int = 1000):
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from agent_hub.db.models import RunRow
    from agent_hub.runs.repository import RunRepository

    route, cp = evidence()
    route.update(runtime_timeout_absolute_seconds=seconds, runtime_plan_token_budget=tokens,
                 runtime_token_absolute_tokens=tokens)
    cp = changed(cp, remaining_absolute_timeout_seconds=seconds,
                 remaining_timeout_seconds=seconds / 2)
    proposal = replace(decision(cp), requires_approval=False, automatic_execution=True,
                       automatic_checkpoint_sha256=cp.state_sha256,
                       automatic_scope_sha256=project_repair_scope_sha256(route),
                       ).to_proposal(run_id=cp.run_id)
    route.update(approval_kind='self_repair', decision_token='token', repair_proposal=proposal)
    row = RunRow(
        id=cp.run_id, tenant_id=cp.tenant_id, actor_id=uuid4(), actor_role=None,
        request='original build request', mode=cp.mode.value, status=RunStatus.FAILED.value,
        version=4, created_at=datetime.now(UTC), routing_decision=route,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def begin(self):
            return self

    session = Session()
    session.scalar = AsyncMock(return_value=row)
    session.flush = AsyncMock()
    session.add = Mock()
    repo = object.__new__(RunRepository)
    repo._session_factory = lambda: session
    repo.latest_checkpoint = AsyncMock(return_value=cp)
    repo.next_event_sequence = AsyncMock(return_value=5)
    repo._recovery_blocked_after_checkpoint = AsyncMock(return_value=False)
    return repo, row, cp, session


@pytest.mark.asyncio
@pytest.mark.parametrize('seconds,tokens', [(200, 200), (7200, 20_000_000)])
async def test_atomic_accept_preserves_permissions_budgets_checkpoint_and_user_request(
    seconds: int, tokens: int,
) -> None:
    repo, row, cp, session = repository_fixture(seconds, tokens)
    before = dict(row.routing_decision)
    before_checkpoint = cp.to_payload()
    result = await repo.accept_self_repair_and_enqueue(
        tenant_id=cp.tenant_id, run_id=cp.run_id, decision_token='token', version=4,
        automatic_checkpoint_sha256=cp.state_sha256,
    )
    assert result.status is RunStatus.QUEUED
    assert result.request == 'original build request'
    assert cp.to_payload() == before_checkpoint
    assert cp.state['usage'] == {'tokens': 100, 'cost_usd': '0.1'}
    for key in ('sandbox_profile', 'requested_permissions', 'permission_mode',
                'runtime_plan_token_budget',
                'runtime_token_absolute_tokens', 'runtime_timeout_absolute_seconds'):
        assert result.routing_decision.get(key) == before.get(key)
    assert result.routing_decision['self_repair_recovery_baseline_sequence'] == 4
    session.add.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    'worker_id', 'worker_lease_token', 'worker_heartbeat_at', 'worker_lease_expires_at',
    'running', 'waiting_approval', 'checkpoint_missing', 'checkpoint_changed',
    'unknown_ledger', 'new_effect_after_checkpoint', 'permissions_changed',
    'caller_digest_changed', 'proposal_requires_approval', 'proposal_digest_changed',
    'blocked_by_run_id', 'requested_permissions_changed', 'hard_limit_raised', 'hard_limit_lowered',
])
async def test_atomic_accept_refuses_changed_evidence_without_enqueue(change: str) -> None:
    from datetime import UTC, datetime

    from agent_hub.runs.repository import RunConflict

    repo, row, cp, session = repository_fixture()
    supplied_hash = cp.state_sha256
    if change.startswith('worker_'):
        setattr(row, change, {'worker_id': 'worker', 'worker_lease_token': uuid4(),
                             'worker_heartbeat_at': datetime.now(UTC),
                             'worker_lease_expires_at': datetime.now(UTC)}[change])
    elif change in ('running', 'waiting_approval'):
        row.status = change
    elif change == 'checkpoint_missing':
        repo.latest_checkpoint.return_value = None
    elif change == 'checkpoint_changed':
        repo.latest_checkpoint.return_value = changed(cp, remaining_timeout_seconds=99)
    elif change == 'unknown_ledger':
        repo.latest_checkpoint.return_value = changed(cp, tool_ledger=[{'status': 'running'}])
    elif change == 'new_effect_after_checkpoint':
        repo._recovery_blocked_after_checkpoint.return_value = True
    elif change == 'permissions_changed':
        row.routing_decision['sandbox_profile'] = 'full_access'
    elif change == 'caller_digest_changed':
        supplied_hash = 'b' * 64
    elif change == 'proposal_requires_approval':
        row.routing_decision['repair_proposal']['requires_approval'] = True
    elif change == 'proposal_digest_changed':
        row.routing_decision['repair_proposal']['automatic_checkpoint_sha256'] = 'b' * 64
    elif change == 'blocked_by_run_id':
        row.blocked_by_run_id = uuid4()
    elif change == 'requested_permissions_changed':
        row.routing_decision['requested_permissions'].append('network.read')
    elif change == 'hard_limit_raised':
        row.routing_decision['runtime_timeout_absolute_seconds'] += 1
    elif change == 'hard_limit_lowered':
        row.routing_decision['runtime_token_absolute_tokens'] -= 1
    original_version = row.version
    with pytest.raises(RunConflict):
        await repo.accept_self_repair_and_enqueue(
            tenant_id=cp.tenant_id, run_id=cp.run_id, decision_token='token', version=4,
            automatic_checkpoint_sha256=supplied_hash,
        )
    assert row.version == original_version
    session.add.assert_not_called()


def test_guarded_resume_cannot_drop_checkpoint_and_reset_budgets() -> None:
    from agent_hub.runs.service import _should_drop_incompatible_self_repair_checkpoint

    route = {'source': 'self_repair', 'self_repair_accepted': True}
    error = RuntimeError('runtime checkpoint is incompatible')
    assert _should_drop_incompatible_self_repair_checkpoint(error, route)
    route['engineering_auto_repair_version'] = 1
    assert not _should_drop_incompatible_self_repair_checkpoint(error, route)
