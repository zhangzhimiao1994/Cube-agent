"""Actual DAO metadata admission against a simulated transactional session."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.capabilities.approvals import (
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.generated_project_service import generated_authority
from agent_hub.runs.repository import RunConflict
from tests.unit.capabilities.test_python_metadata_preparation import setup
from tests.unit.runs.test_database_uuid_boundary import _repository_session


def fixture() -> tuple[Any, ...]:
    m, row, plan = setup()
    row.mode, row.version, row.created_at = 'direct', 1, datetime.now(UTC)
    dao, session = _repository_session()
    user = SimpleNamespace(tenant_id=row.tenant_id, id=row.actor_id, role=row.actor_role, disabled=False)
    session.user = user

    async def scalar(statement: Any) -> Any:
        return user if {t.name for t in statement.get_final_froms()} == {'agent_hub_users'} else row

    session.scalar.side_effect = scalar
    request = m.metadata_preparation_binding(row)
    approval = SimpleNamespace(tenant_id=row.tenant_id, run_id=row.id,
        approval_id='metadata', action='capability_tool', status='approved', payload={
            'approval_scope': capability_approval_scope(request),
            'approval_fingerprint': fingerprint_capability_request(request),
            'python_metadata_binding': request.arguments})
    session.scalars.return_value = SimpleNamespace(all=lambda: [approval])
    authority = generated_authority(row, row.tenant_id, row.id)
    expected = deepcopy(row.routing_decision[m.METADATA_KEY])
    replacement = expected | {'phase': 'executing', 'execution_id': 'metadata_prepare_' + 'a' * 32}
    kwargs = {'actor_id': row.actor_id, 'actor_role': authority.role,
        'lease_token': authority.lease_token, 'request_sha256': authority.request_sha256,
        'project_id': authority.project_id, 'session_id': authority.session_id,
        'expected': expected, 'replacement': replacement}
    return m, dao, session, row, plan, approval, kwargs


async def test_metadata_intent_requires_locked_exact_grant_and_fresh_draft() -> None:
    m, dao, session, row, _, _, kwargs = fixture()
    assert callable(getattr(dao, 'compare_and_set_python_metadata_preparation', None)), 'missing metadata CAS'
    entered = []

    async def guard() -> None:
        assert all('FOR UPDATE' in str(call.args[0]) for call in session.scalar.call_args_list)
        assert row.routing_decision[m.METADATA_KEY]['phase'] == 'planned'
        assert session.flush.await_count == 0
        entered.append(True)

    result = await dao.compare_and_set_python_metadata_preparation(row.tenant_id, row.id,
        **kwargs, before_reservation=guard)
    assert entered == [True] and session.flush.await_count == 1
    assert 'FOR UPDATE' in str(session.scalars.call_args.args[0])
    assert result.routing_decision[m.METADATA_KEY] == kwargs['replacement']
    assert row.version == 2 and 'generated_project_preparation' not in row.routing_decision


@pytest.mark.parametrize('defect', [
    'missing_guard', 'missing_grant', 'generic', 'conversation', 'mixed', 'foreign',
    'stale_scope', 'bool_version', 'draft_changed', 'lease_expired', 'state_changed',
    'actor_changed', 'captured', 'replay',
    'disabled_user', 'user_role_changed', 'nested_drift',
])
async def test_metadata_intent_rejects_without_writes(defect: str) -> None:
    m, dao, session, row, _, approval, kwargs = fixture()
    assert callable(getattr(dao, 'compare_and_set_python_metadata_preparation', None)), 'missing metadata CAS'
    if defect == 'missing_grant': session.scalars.return_value = SimpleNamespace(all=list)
    if defect == 'generic': approval.payload.pop('python_metadata_binding')
    if defect == 'conversation': approval.payload['decision_scope'] = 'conversation'
    if defect == 'mixed': approval.payload['python_input_binding'] = approval.payload['python_metadata_binding']
    if defect == 'foreign': approval.run_id = None
    if defect == 'stale_scope': approval.payload['approval_scope'] += '0'
    if defect == 'bool_version': approval.payload['python_metadata_binding'] = dict(approval.payload['python_metadata_binding']) | {'schema_version': True}
    if defect == 'state_changed': kwargs['expected'] = None
    if defect == 'actor_changed': kwargs['project_id'] = 'foreign'
    if defect == 'captured': row.routing_decision['generated_project_preparation'] = {}
    if defect == 'disabled_user': session.user.disabled = True
    if defect == 'user_role_changed': session.user.role = 'viewer'
    if defect == 'replay':
        row.routing_decision[m.METADATA_KEY] = deepcopy(kwargs['replacement'])
        kwargs['expected'] = deepcopy(kwargs['replacement'])
    before = deepcopy(row.routing_decision)

    async def guard() -> None:
        if defect == 'draft_changed': raise ValueError('draft changed')
        if defect == 'lease_expired': row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        if defect == 'nested_drift': row.routing_decision[m.METADATA_KEY]['plan_sha256'] = '0' * 64

    if defect != 'missing_guard': kwargs['before_reservation'] = guard
    with pytest.raises((RunConflict, ValueError, RuntimeError)):
        await dao.compare_and_set_python_metadata_preparation(row.tenant_id, row.id, **kwargs)
    assert session.flush.await_count == 0 and row.version == 1
    if defect != 'nested_drift': assert row.routing_decision == before


@pytest.mark.parametrize('method', ['is_capability_approval_approved', 'is_capability_approval_scope_approved'])
@pytest.mark.parametrize('defect', ['none', 'generic', 'conversation', 'mixed', 'stale', 'bool'])
async def test_metadata_grant_queries_revalidate_current_private_binding(method: str, defect: str) -> None:
    _, dao, _, row, _, approval, _ = fixture()
    if defect == 'generic': approval.payload.pop('python_metadata_binding')
    if defect == 'conversation': approval.payload['decision_scope'] = 'conversation'
    if defect == 'mixed': approval.payload['public_binding'] = {}
    if defect == 'stale': row.routing_decision['workspace_session_id'] = 'foreign'
    if defect == 'bool': approval.payload['python_metadata_binding'] = dict(approval.payload['python_metadata_binding']) | {'schema_version': True}
    target = approval.payload['approval_scope' if 'scope' in method else 'approval_fingerprint']
    result = await getattr(dao, method)(row.tenant_id, row.id, target)
    assert result is (defect == 'none')


async def test_once_resolution_remains_exactly_usable() -> None:
    _, dao, _, row, _, approval, _ = fixture()
    approval.payload['decision_scope'] = 'once'
    assert await dao.is_capability_approval_scope_approved(row.tenant_id, row.id,
        approval.payload['approval_scope'])


async def test_begin_persists_private_metadata_binding_before_clearing_lease() -> None:
    m, dao, session, row, _, approval, _ = fixture()
    captured = []

    async def persist(*args: Any, **kwargs: Any) -> None:
        assert row.worker_lease_token is not None
        captured.append(kwargs)

    dao._upsert_capability_approval = persist
    request = m.metadata_preparation_binding(row)
    result = await dao.begin_capability_approval(row.tenant_id, row.id,
        actor_id=row.actor_id, approval_id='metadata',
        approval_scope=approval.payload['approval_scope'],
        approval_fingerprint=fingerprint_capability_request(request), approval_request=request)
    assert len(captured) == 1
    assert captured[0]['python_metadata_binding'] == request.arguments
    assert captured[0].get('conversation_binding') is None
    assert result.routing_decision['approval_checkpoint_required'] is True
    assert row.worker_lease_token is None and session.flush.await_count == 1


@pytest.mark.parametrize('defect', ['no_request', 'stale_scope', 'lease', 'executing', 'bool_arguments'])
async def test_begin_metadata_cannot_fall_back_to_generic_approval(defect: str) -> None:
    from unittest.mock import AsyncMock

    m, dao, session, row, _, approval, _ = fixture()
    request = m.metadata_preparation_binding(row)
    scope = approval.payload['approval_scope']
    if defect == 'stale_scope': scope += '0'
    if defect == 'lease': row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if defect == 'executing': row.routing_decision[m.METADATA_KEY].update(
        phase='executing', execution_id='metadata_prepare_' + 'a' * 32)
    if defect == 'bool_arguments': request = request.model_copy(update={
        'arguments': dict(request.arguments) | {'schema_version': True}})
    dao._upsert_capability_approval = AsyncMock()
    with pytest.raises(RunConflict, match='metadata'):
        await dao.begin_capability_approval(row.tenant_id, row.id, actor_id=row.actor_id,
            approval_id='metadata', approval_scope=scope,
            approval_fingerprint=fingerprint_capability_request(request),
            approval_request=None if defect == 'no_request' else request)
    assert not dao._upsert_capability_approval.await_count and not session.flush.await_count
    assert row.status == 'running' and row.worker_lease_token is not None


async def test_auto_review_persists_exact_metadata_plan_not_generic_grant() -> None:
    from unittest.mock import AsyncMock

    m, dao, _, row, _, approval, _ = fixture()
    dao._upsert_capability_approval_review = AsyncMock()
    await dao.record_capability_approval_review(row.tenant_id, row.id, approval_id='meta-review',
        approval_scope=approval.payload['approval_scope'],
        approval_fingerprint=approval.payload['approval_fingerprint'], reviewer='rules',
        reason='concrete plan', status='approved')
    assert dao._upsert_capability_approval_review.call_args.kwargs['python_metadata_binding'] == m.metadata_preparation_binding(row).arguments


@pytest.mark.parametrize('defect', ['none', 'scope_stripped', 'missing_binding', 'changed_plan', 'checkpoint_drift'])
async def test_manual_grant_revalidates_before_enqueue(defect: str) -> None:
    from unittest.mock import AsyncMock

    _, dao, session, row, _, approval, _ = fixture()
    row.status = 'waiting_approval'
    row.routing_decision.update(approval_kind='capability_tool', approval_id='metadata',
        approval_fingerprint=approval.payload['approval_fingerprint'],
        approval_scope=approval.payload['approval_scope'])
    approval.status = 'pending'
    if defect == 'scope_stripped': row.routing_decision.pop('approval_scope')
    if defect == 'missing_binding': approval.payload.pop('python_metadata_binding')
    if defect == 'changed_plan': row.routing_decision['workspace_session_id'] = 'foreign'

    async def checkpoint(*args: Any, **kwargs: Any) -> None:
        if defect == 'checkpoint_drift': row.routing_decision['workspace_session_id'] = 'foreign'

    dao._require_capability_approval_checkpoint = checkpoint
    dao._upsert_capability_approval = AsyncMock()
    if defect == 'none':
        result = await dao.approve_capability_and_enqueue(tenant_id=row.tenant_id, run_id=row.id,
            actor_id=row.actor_id, approval_id='metadata', version=row.version)
        assert result.status.value == 'queued'
        assert dao._upsert_capability_approval.call_args.kwargs['python_metadata_binding'] == approval.payload['python_metadata_binding']
    else:
        with pytest.raises(RunConflict, match='metadata'):
            await dao.approve_capability_and_enqueue(tenant_id=row.tenant_id, run_id=row.id,
                actor_id=row.actor_id, approval_id='metadata', version=row.version)
        assert not dao._upsert_capability_approval.await_count and not session.flush.await_count
        assert row.status == 'waiting_approval'


@pytest.mark.parametrize('captured', [False, True])
async def test_metadata_approval_history_names_action_without_private_configuration(captured: bool) -> None:
    from agent_hub.runs.repository import _public_plugin_approval_events

    _, _, _, row, plan, approval, _ = fixture()
    approval.created_at = datetime.now(UTC)
    approval.payload['public_request'] = {'kind': 'approval.requested', 'sequence': 1,
        'run_id': str(row.id), 'approval_id': 'metadata',
        'payload': {'run_id': str(row.id), 'version': 1}}
    approval.payload['public_resolution'] = {'kind': 'approval.resolved', 'sequence': 2,
        'run_id': str(row.id), 'approval_id': 'metadata', 'decision': 'approved'}
    row.version = 2
    if captured: row.routing_decision['generated_project_preparation'] = {}
    events = _public_plugin_approval_events([approval], row)
    payload = events[0]['payload']
    assert payload['resource'] == 'workspace/current/project-metadata/python/' + plan.sha256
    assert payload['download_count'] == plan.missing_count and payload['download_bytes'] == plan.missing_bytes
    assert all(secret not in str(events) for secret in ('root_config_sha256', 'catalog_sha256', 'request_sha256'))


async def test_real_policy_auto_review_keeps_metadata_grant_usable_at_dao() -> None:
    from unittest.mock import AsyncMock

    from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
    from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
    from agent_hub.capabilities.gateway import (
        ApprovalReviewDecision,
        ApprovalReviewOutcome,
        CapabilityStatus,
    )

    m, dao, session, row, _, approval, _ = fixture()
    rows = []
    session.scalars.return_value = SimpleNamespace(all=lambda: rows)
    dao.get = AsyncMock(return_value=dao._record(row))

    async def persist(*args: Any, **kwargs: Any) -> None:
        payload = {name: kwargs[name] for name in (
            'approval_scope', 'approval_fingerprint', 'python_metadata_binding') if name in kwargs}
        rows.append(SimpleNamespace(tenant_id=row.tenant_id, run_id=row.id,
            status=kwargs['status'], action='capability_tool.auto_review', payload=payload))

    dao._upsert_capability_approval_review = persist

    class Reviewer:
        async def review(self, request: Any) -> Any:
            return ApprovalReviewDecision(outcome=ApprovalReviewOutcome.ALLOW,
                reviewer='configured-reviewer', reason='concrete approved catalog download')

    policy = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), dao,
        approval_reviewer=Reviewer())
    request = m.metadata_preparation_binding(row)
    result = await policy.invoke(request, role=row.actor_role)
    assert result.status is CapabilityStatus.ALLOWED
    assert len(rows) == 1 and rows[0].payload.get('approval_scope') == approval.payload['approval_scope']
    assert await dao.is_capability_approval_scope_approved(row.tenant_id, row.id,
        approval.payload['approval_scope'])
