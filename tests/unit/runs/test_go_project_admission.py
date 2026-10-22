"""Original Go admission DAO proofs; SQL doubles do not prove native execution or PG locks."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.capabilities.approvals import (
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.go_project_inputs import bind_go_project_inputs
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunRepository, _public_plugin_approval_events
from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority
from agent_hub.skills.sandbox.go_project_protocol import GoProjectInvocation
from tests.unit.capabilities.test_go_project_inputs import case as original_case
from tests.unit.capabilities.test_toolchain_preparation import case as case  # noqa: PLC0414
from tests.unit.capabilities.test_toolchain_preparation import state_for
from tests.unit.runs.test_toolchain_authorization_fence import ApprovalDB
from tests.unit.runs.test_toolchain_authorization_fence import Session as ApprovalSession


class Repository(RunRepository):
    async def _recovery_blocked_after_checkpoint(self, *args: Any, **kwargs: Any) -> bool:
        # The real checkpoint gate runs; unrelated recovery-event SQL is covered separately.
        assert kwargs.get('pre_model_original_capture') is True
        return False


class Session(ApprovalSession):
    async def scalars(self, statement: Any) -> Any:
        if statement._for_update_arg is not None:
            self.db.locks.append('approval')
            if self.db.during_approval_lock is not None:
                self.db.during_approval_lock(self)
        return await super().scalars(statement)

    async def execute(self, statement: Any) -> Any:
        self.db.locks.append('approval-write')
        if statement.is_insert:
            return await super().execute(statement)
        assert self.active and self.locked and statement.is_update
        params = statement.compile().params
        delta = next(value for value in params.values() if isinstance(value, dict))
        selected = [row for row in self.approvals if row.status == 'pending'
            and row.approval_id in params.values()
            and row.payload['approval_fingerprint'] in params.values()
            and ('go_recipe_binding' not in row.payload or any(
                isinstance(value, dict) and value == row.payload['go_recipe_binding']
                for value in params.values()))]
        for row in selected:
            row.status = params['status']
            row.payload = {**row.payload, **delta}
        return type('Result', (), {'scalar_one_or_none': lambda _: selected[0].id if selected else None})()

    async def flush(self) -> None:
        await super().flush()
        if self.db.fail_write:
            raise RunConflict('controlled commit failure')

    def add(self, value: object) -> None:
        pass


class GoDB(ApprovalDB):
    def __init__(self, case: Any, inputs: Any) -> None:
        super().__init__(case)
        self.inputs = inputs
        self.row.routing_decision['toolchain_supply'] = state_for(case, 'supplied')
        self.repository = Repository(cast(Any, lambda: Session(self)))
        self.fail_write = False
        self.during_approval_lock: Any = None

    def current_authority(self) -> ToolchainSupplyAuthority:
        return ToolchainSupplyAuthority.from_context(self.context(), worker_id=self.row.worker_id)


@pytest.fixture
def db(tmp_path: Path, case: Any) -> GoDB:
    value = original_case(tmp_path / 'original')
    inputs = bind_go_project_inputs(stored=value.stored, scope=value.scope,
        toolchain=case.environment, source_limit_bytes=1024**2, file_limit=100)
    case.plan = case.plan.model_copy(update={'scope': value.scope,
        'declaration_sha256': inputs.declaration.sha256})
    case.ref = value.stored.reference
    case.row.routing_decision['project_input_preparation']['reference'] = value.stored.reference.to_payload()
    return GoDB(case, inputs)


@pytest.fixture
def api() -> Any:
    name = 'agent_hub.runs.go_project_admission'
    assert importlib.util.find_spec(name) is not None, 'missing original-only Go admission helper'
    return importlib.import_module(name)


def invocation(db: GoDB, **patch: Any) -> GoProjectInvocation:
    inputs = db.inputs
    return GoProjectInvocation.model_validate_json(json.dumps({
        'execution_id': 'go_original_' + db.row.id.hex,
        'tenant_id': str(db.row.tenant_id), 'run_id': str(db.row.id),
        'project_id': 'demo', 'session_id': 'session', 'sandbox_profile': 'workspace_write',
        'original_reference': inputs.original_reference.to_payload(), 'source_kind': 'original',
        'scope': inputs.scope.to_payload(), 'recipe_sha256': inputs.sha256,
        'environment_input_sha256': inputs.environment_input_sha256,
        'toolchain': inputs.toolchain.model_dump(mode='json'), 'timeout_seconds': 300,
        'source_limit_bytes': inputs.source_limit_bytes, 'file_limit': inputs.file_limit, **patch,
    }), strict=True)


async def bind(db: GoDB) -> Any:
    return await db.repository.bind_go_project_recipe(db.row.tenant_id, db.row.id,
        authority=db.current_authority(), inputs=db.inputs, expected=None)


async def seed_grant(db: GoDB, api: Any) -> Any:
    await bind(db)
    request = api.go_recipe_binding(db.row)
    await db.repository.record_capability_approval_review(db.row.tenant_id, db.row.id,
        approval_id='go_recipe_review', approval_fingerprint=fingerprint_capability_request(request),
        approval_scope=capability_approval_scope(request), reviewer='controlled', reason='Go recipe',
        status='approved', go_recipe_authority=db.current_authority())
    return request


def snapshot(db: GoDB) -> tuple[object, ...]:
    row = db.row
    return (row.status, row.worker_id, row.worker_lease_token, row.worker_lease_expires_at,
            row.version, deepcopy(row.routing_decision),
            [(a.approval_id, a.status, deepcopy(a.payload)) for a in db.approvals])


def test_go_recipe_has_its_own_approval_scope(db: GoDB) -> None:
    request = db.inputs.approval_request(user_id=db.row.actor_id, agent_id='main_agent', idempotency_key='go')
    assert capability_approval_scope(request) == (
        f'capability-scope:v1:tenant={db.row.tenant_id}:user={db.row.actor_id}:run={db.row.id}:'
        f'capability=command:operation=run:resource=workspace/current/project-verification/go/{db.inputs.sha256}')


async def test_bind_preserves_capture_and_stores_only_compact_recipe(db: GoDB, api: Any) -> None:
    before = deepcopy(db.row.routing_decision)
    await bind(db)
    route = db.row.routing_decision
    assert {key: value for key, value in route.items() if key != 'go_project_recipe'} == before
    marker = route['go_project_recipe']
    assert set(marker) == {'schema_version', 'binding', 'toolchain', 'supply_plan_sha256',
                           'source_limit_bytes', 'file_limit'}
    assert 'source_files' not in str(marker) and 'main_test.go' not in str(marker)
    assert marker['binding'] == db.inputs.approval_request(user_id=db.row.actor_id,
        agent_id='main_agent', idempotency_key='ignored').arguments
    assert api.go_recipe_binding(db.row).resource.endswith('/go/' + db.inputs.sha256)
    version = db.row.version
    await bind(db)
    assert db.row.version == version


@pytest.mark.parametrize('defect', ['sdk', 'not_supplied', 'bool_schema', 'generated', 'python', 'capture'])
async def test_binding_requires_real_persisted_supply_and_original_capture(db: GoDB, defect: str) -> None:
    route = db.row.routing_decision
    if defect == 'sdk':
        db.inputs = replace(db.inputs,
            toolchain=db.inputs.toolchain.model_copy(update={'tree_sha256': '0' * 64}))
    elif defect == 'not_supplied':
        route['toolchain_supply'] = state_for(db.case)
    elif defect == 'bool_schema':
        route['project_input_preparation']['schema_version'] = True
    elif defect in {'generated', 'python'}:
        route['generated_project_preparation' if defect == 'generated' else 'python_input_supply'] = None
    else:
        route['project_input_preparation']['reference']['file_count'] += 1
    before = snapshot(db)
    with pytest.raises((RunConflict, ValueError)):
        await bind(db)
    assert snapshot(db) == before


@pytest.mark.parametrize('field', ['worker', 'token', 'role', 'user', 'expiry', 'disabled'])
async def test_bind_checks_exact_live_authority_under_locks(db: GoDB, field: str) -> None:
    original = db.current_authority()
    if field in {'worker', 'token', 'role', 'user', 'expiry'}:
        name, value = {'worker': ('worker_id', 'B'), 'token': ('worker_lease_token', uuid4()),
            'role': ('actor_role', 'admin'), 'user': ('actor_id', uuid4()),
            'expiry': ('worker_lease_expires_at', datetime.now(UTC))}[field]
        setattr(db.row, name, value)
    else:
        db.user.disabled = True
    before = snapshot(db)
    with pytest.raises(RunConflict):
        await db.repository.bind_go_project_recipe(db.row.tenant_id, db.row.id,
            authority=original, inputs=db.inputs, expected=None)
    assert snapshot(db) == before


async def test_supplied_sdk_is_not_execution_approval(db: GoDB, api: Any) -> None:
    await bind(db)
    before = snapshot(db)
    with pytest.raises(RunConflict, match='approval'):
        await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
            authority=db.current_authority(), invocation=invocation(db))
    assert snapshot(db) == before


async def test_original_reservation_commits_once_and_retry_returns_metadata(db: GoDB, api: Any) -> None:
    await seed_grant(db, api)
    before = deepcopy(db.row.routing_decision)
    version = db.row.version
    first = await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
        authority=db.current_authority(), invocation=invocation(db))
    assert first.disposition == 'created' and db.row.version == version + 1
    assert first.state == db.row.routing_decision['go_project_execution']
    assert first.state['source_sha256'] == db.inputs.scope.source_sha256
    assert {k: v for k, v in db.row.routing_decision.items() if k != 'go_project_execution'} == before
    again = await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
        authority=db.current_authority(), invocation=invocation(db))
    assert again.disposition == 'existing' and again.state == first.state
    assert db.row.version == version + 1 and 'authorized' not in again.state
    assert str(db.current_authority().lease_token) not in str(first)


@pytest.mark.parametrize('patch', [
    {'source_kind': 'candidate'}, {'execution_id': 'different'}, {'timeout_seconds': 301},
    {'source_limit_bytes': 1024**2 + 1}, {'file_limit': 101},
])
async def test_reservation_rejects_candidate_changed_limits_or_retry(db: GoDB, api: Any, patch: Any) -> None:
    await seed_grant(db, api)
    if 'timeout_seconds' in patch:
        await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
            authority=db.current_authority(), invocation=invocation(db))
    before = snapshot(db)
    with pytest.raises((RunConflict, ValueError)):
        await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
            authority=db.current_authority(), invocation=invocation(db, **patch))
    assert snapshot(db) == before


@pytest.mark.parametrize('getter', ['scope', 'fingerprint'])
async def test_generic_grant_getters_cannot_bypass_private_admission(db: GoDB, api: Any, getter: str) -> None:
    request = await seed_grant(db, api)
    if getter == 'scope':
        approved = await db.repository.is_capability_approval_scope_approved(
            db.row.tenant_id, db.row.id, capability_approval_scope(request))
    else:
        approved = await db.repository.is_capability_approval_approved(
            db.row.tenant_id, db.row.id, fingerprint_capability_request(request))
    assert approved is False
    assert (await db.repository.require_go_project_recipe_approval(db.row.tenant_id, db.row.id,
        authority=db.current_authority(), request=request)).resource == request.resource


@pytest.mark.parametrize('defect', ['marker_schema', 'binding_schema', 'sdk_count', 'scope', 'binding', 'supply'])
async def test_canonical_types_and_exact_binding_cannot_reuse_old_grant(db: GoDB, api: Any, defect: str) -> None:
    request = await seed_grant(db, api)
    route = db.row.routing_decision
    marker = route['go_project_recipe']
    if defect == 'marker_schema':
        marker['schema_version'] = True
    elif defect == 'binding_schema':
        marker['binding']['schema_version'] = True
    elif defect == 'sdk_count':
        marker['toolchain']['file_count'] = True
    elif defect == 'scope':
        db.approvals[0].payload['approval_scope'] += '/nested'
    elif defect == 'binding':
        db.approvals[0].payload['go_recipe_binding']['binding']['schema_version'] = True
    else:
        route['toolchain_supply']['receipt']['environment']['tree_sha256'] = '0' * 64
    before = snapshot(db)
    with pytest.raises((RunConflict, ValueError)):
        await db.repository.require_go_project_recipe_approval(db.row.tenant_id, db.row.id,
            authority=db.current_authority(), request=request)
    assert snapshot(db) == before


async def pending(db: GoDB, api: Any) -> Any:
    await bind(db)
    request = api.go_recipe_binding(db.row)
    await db.repository.begin_capability_approval(db.row.tenant_id, db.row.id,
        actor_id=db.row.actor_id, approval_id='approval_go_recipe',
        approval_fingerprint=fingerprint_capability_request(request), approval_scope=capability_approval_scope(request),
        go_recipe_authority=db.current_authority())
    return request


@pytest.mark.parametrize('manual', ['enqueue', 'resolve'])
@pytest.mark.parametrize('defect', ['none', 'binding', 'disabled', 'role', 'already_reserved', 'scope_removed'])
async def test_manual_approval_revalidates_without_old_worker_lease(
    db: GoDB, api: Any, manual: str, defect: str,
) -> None:
    await pending(db, api)
    assert db.row.worker_lease_token is None
    if defect == 'binding':
        db.approvals[0].payload['go_recipe_binding']['binding']['recipe_sha256'] = '0' * 64
    elif defect == 'disabled':
        db.user.disabled = True
    elif defect == 'role':
        db.user.role = 'admin'
    elif defect == 'already_reserved':
        db.row.routing_decision['go_project_execution'] = {'approved': True}
    elif defect == 'scope_removed':
        db.row.routing_decision.pop('approval_scope')
    before = snapshot(db)

    async def resolve() -> Any:
        if manual == 'enqueue':
            return await db.repository.approve_capability_and_enqueue(tenant_id=db.row.tenant_id,
                run_id=db.row.id, actor_id=db.row.actor_id, approval_id='approval_go_recipe', version=db.row.version)
        return await db.repository.resolve_capability_approval(db.row.tenant_id, db.row.id,
            RunStatus.RUNNING, approval_id='approval_go_recipe',
            approval_fingerprint=db.approvals[0].payload['approval_fingerprint'])

    if defect != 'none':
        with pytest.raises(RunConflict):
            await resolve()
        assert snapshot(db) == before
    else:
        await resolve()
        assert db.approvals[0].status == 'approved'
        db.row.status, db.row.worker_id, db.row.worker_lease_token = 'running', 'resumed', uuid4()
        db.row.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        result = await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
            authority=db.current_authority(), invocation=invocation(db))
        assert result.disposition == 'created'


async def test_go_manual_reject_keeps_capture_and_never_grants(db: GoDB, api: Any) -> None:
    await pending(db, api)
    capture = deepcopy(db.row.routing_decision['project_input_preparation'])
    await db.repository.reject_capability_approval(tenant_id=db.row.tenant_id, run_id=db.row.id,
        actor_id=db.row.actor_id, approval_id='approval_go_recipe', version=db.row.version)
    assert db.approvals[0].status == 'rejected'
    assert db.row.routing_decision['project_input_preparation'] == capture
    assert 'go_project_execution' not in db.row.routing_decision


async def test_public_go_approval_evidence_is_compact_and_precise(db: GoDB, api: Any) -> None:
    request = await pending(db, api)
    events = _public_plugin_approval_events(db.approvals, db.row)
    assert events[0]['payload'] == {'run_id': str(db.row.id), 'version': db.row.version,
        'capability': 'command', 'operation': 'run', 'resource': request.resource,
        'approval_scope': capability_approval_scope(request)}
    assert 'go_recipe_binding' not in str(events) and 'source_files' not in str(events)


@pytest.mark.parametrize('marker', [None, True, {}])
async def test_binding_does_not_replace_malformed_existing_marker(db: GoDB, marker: Any) -> None:
    db.row.routing_decision['go_project_recipe'] = marker
    before = snapshot(db)
    with pytest.raises(RunConflict):
        await bind(db)
    assert snapshot(db) == before


async def test_recipe_declaration_must_match_the_persisted_supply_plan(db: GoDB) -> None:
    db.inputs = replace(db.inputs, declaration=replace(db.inputs.declaration, go_version='1.19'))
    before = snapshot(db)
    with pytest.raises(RunConflict):
        await bind(db)
    assert snapshot(db) == before


@pytest.mark.parametrize('change', ['disabled', 'role'])
@pytest.mark.parametrize('action', ['reject', 'cancel'])
async def test_non_grant_resolution_survives_current_user_changes(db: GoDB, api: Any, change: str, action: str) -> None:
    request = await pending(db, api)
    db.user.disabled = change == 'disabled'
    if change == 'role':
        db.user.role = 'admin'
    capture = deepcopy(db.row.routing_decision['project_input_preparation'])
    if action == 'reject':
        await db.repository.reject_capability_approval(tenant_id=db.row.tenant_id, run_id=db.row.id,
            actor_id=db.row.actor_id, approval_id='approval_go_recipe', version=db.row.version)
    else:
        await db.repository.resolve_capability_approval(db.row.tenant_id, db.row.id, RunStatus.CANCELLED,
            approval_id='approval_go_recipe', approval_fingerprint=fingerprint_capability_request(request))
    assert db.row.status == 'cancelled' and db.approvals[0].status == 'rejected'
    assert db.row.routing_decision['project_input_preparation'] == capture
    assert 'go_project_execution' not in db.row.routing_decision


@pytest.mark.parametrize('action', ['reject', 'cancel'])
async def test_non_grant_resolution_still_requires_exact_pending_binding(db: GoDB, api: Any, action: str) -> None:
    request = await pending(db, api)
    db.user.disabled = True
    db.approvals[0].payload['go_recipe_binding']['binding']['schema_version'] = True
    before = snapshot(db)
    with pytest.raises(RunConflict):
        if action == 'reject':
            await db.repository.reject_capability_approval(tenant_id=db.row.tenant_id, run_id=db.row.id,
                actor_id=db.row.actor_id, approval_id='approval_go_recipe', version=db.row.version)
        else:
            await db.repository.resolve_capability_approval(db.row.tenant_id, db.row.id, RunStatus.CANCELLED,
                approval_id='approval_go_recipe', approval_fingerprint=fingerprint_capability_request(request))
    assert snapshot(db) == before


@pytest.mark.parametrize('change', ['takeover', 'token', 'worker', 'request'])
async def test_original_intent_cannot_be_reused_by_a_different_lease(db: GoDB, api: Any, change: str) -> None:
    await seed_grant(db, api)
    request = invocation(db)
    await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
        authority=db.current_authority(), invocation=request)
    if change == 'takeover':
        db.takeover()
    elif change == 'token':
        db.row.worker_lease_token = uuid4()
    elif change == 'worker':
        db.row.worker_id = 'worker-B'
    else:
        db.row.request = 'another original request'
    before = snapshot(db)
    with pytest.raises(RunConflict):
        await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
            authority=db.current_authority(), invocation=request)
    assert snapshot(db) == before


async def test_reservation_lease_digest_is_private_and_heartbeat_stable(db: GoDB, api: Any) -> None:
    await seed_grant(db, api)
    authority = db.current_authority()
    version = db.row.version
    first = await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
        authority=authority, invocation=invocation(db))
    binding = {'schema_version': 1, 'kind': 'go_project_execution_lease_v1',
        'tenant_id': str(db.row.tenant_id), 'run_id': str(db.row.id), 'user_id': str(db.row.actor_id),
        'actor_role': 'operator', 'worker_id': db.row.worker_id, 'lease_token': str(authority.lease_token),
        'mode': TaskMode(db.row.mode).value,
        'request_sha256': hashlib.sha256(api.encoded(db.row.request)).hexdigest(),
        'capture_sha256': hashlib.sha256(api.encoded(db.row.routing_decision['project_input_preparation'])).hexdigest()}
    assert first.state['lease_binding_sha256'] == hashlib.sha256(api.encoded(binding)).hexdigest()
    assert set(first.state) == {'schema_version', 'phase', 'source_kind', 'execution_id', 'recipe_sha256',
                               'environment_input_sha256', 'source_sha256', 'invocation_sha256', 'lease_binding_sha256'}
    db.row.worker_lease_expires_at += timedelta(minutes=5)
    db.row.worker_heartbeat_at = datetime.now(UTC)
    again = await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
        authority=authority, invocation=invocation(db))
    assert again.disposition == 'existing' and again.state == first.state
    assert db.row.version == version + 1
    assert str(authority.lease_token) not in repr(first) + repr(again) + str(dict(first.state))
    assert 'lease_token' not in first.state and 'worker_id' not in first.state


async def test_unbound_legacy_intent_cannot_be_adopted(db: GoDB, api: Any) -> None:
    await seed_grant(db, api)
    await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
        authority=db.current_authority(), invocation=invocation(db))
    db.row.routing_decision['go_project_execution'].pop('lease_binding_sha256', None)
    before = snapshot(db)
    with pytest.raises(RunConflict):
        await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
            authority=db.current_authority(), invocation=invocation(db))
    assert snapshot(db) == before


async def test_existing_intent_cannot_be_replaced_by_a_new_original_source(db: GoDB, api: Any) -> None:
    await seed_grant(db, api)
    original = invocation(db)
    await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
        authority=db.current_authority(), invocation=original)
    payload = original.model_dump(mode='json')
    payload['scope']['source_sha256'] = 'f' * 64
    payload['original_reference']['input_source_sha256'] = 'f' * 64
    changed = GoProjectInvocation.model_validate_json(json.dumps(payload), strict=True)
    before = snapshot(db)
    with pytest.raises(RunConflict):
        await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
            authority=db.current_authority(), invocation=changed)
    assert snapshot(db) == before


async def test_same_token_mode_change_cannot_adopt_existing_intent(db: GoDB, api: Any) -> None:
    await seed_grant(db, api)
    original = invocation(db)
    token = db.row.worker_lease_token
    first = await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
        authority=db.current_authority(), invocation=original)
    db.row.mode = TaskMode(db.row.mode)
    normalized = await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
        authority=db.current_authority(), invocation=original)
    assert normalized.disposition == 'existing' and normalized.state == first.state
    db.row.mode = TaskMode.DIRECT if db.row.mode is not TaskMode.DIRECT else TaskMode.DISPATCH
    before = snapshot(db)
    with pytest.raises(RunConflict):
        await db.repository.reserve_go_project_execution(db.row.tenant_id, db.row.id,
            authority=db.current_authority(), invocation=original)
    assert db.row.worker_lease_token == token and snapshot(db) == before
