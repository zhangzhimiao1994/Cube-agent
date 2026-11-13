"""Exact observed Python recipe review; no native process or provider is used."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Never, TypedDict, Unpack, cast
from uuid import UUID

import pytest
from sqlalchemy import Select, Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.routers.admin import PersistentToolApprovalMode
from agent_hub.auth.models import Role
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.capabilities.python_project_service import _Context
from agent_hub.capabilities.review_context import _ConcreteReviewCall
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.db.models import RunApprovalRow, RunOutboxRow
from agent_hub.runtime.contracts import RuntimeCheckpoint, TaskContext, _freeze_object
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities.test_python_project_service import PythonExecutor
from tests.unit.runs.test_original_python_offline_review import LocalRepository, setup


class _RecipeRepository(LocalRepository):
    # Annotation-only view of fields installed by pending_case; never instantiated.
    outbox: list[RunOutboxRow]
    checkpoint: RuntimeCheckpoint
    events_after_checkpoint: bool
    pool: SimpleNamespace | None
    unresolved_attempt: UUID | None


type _ObservedCase = tuple[ProjectVerificationService, _RecipeRepository, PythonExecutor,
                          TaskContext, DefaultRuntimeCapabilityPolicyGateway, CapabilityRequest]


class _InvocationArgs(TypedDict):
    tool_name: str
    backend: object
    backend_getter: Callable[[], object]


async def observed_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ObservedCase:
    preparer, verifier, repository, executor, context, policy = await setup(tmp_path, monkeypatch)
    verifier = cast(ProjectVerificationService, verifier)
    repository = cast(_RecipeRepository, repository)
    executor = cast(PythonExecutor, executor)
    context = cast(TaskContext, context)
    policy = cast(DefaultRuntimeCapabilityPolicyGateway, policy)
    with bind_execution_authority(context, worker_id='local-worker'):
        context = await preparer(context)
    request = executor.inputs(executor.calls[0]).approval_request(
        user_id=cast(UUID, context.actor_id), agent_id='project_engineer', idempotency_key='candidate-verification')
    # Same owned verifier that the production stack binds, not model-supplied data.
    policy._python_recipe_verifier = lambda: verifier
    from agent_hub.capabilities import python_project_service
    from agent_hub.runs import python_recipe_review

    project_source_fingerprint = cast(Callable[[Path, ProjectInvocation], Awaitable[str]],
        vars(python_project_service)["project_source_fingerprint"])
    project_source_manifest = cast(Callable[[Path, ProjectInvocation], Awaitable[dict[str, str]]],
        vars(python_project_service)["project_source_manifest"])

    monkeypatch.setattr(python_recipe_review, 'project_source_fingerprint', project_source_fingerprint)
    monkeypatch.setattr(python_recipe_review, 'project_source_manifest', project_source_manifest)
    return verifier, repository, executor, context, policy, request


async def test_observed_recipe_uses_command_consent_without_another_manual_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repository, executor, context, policy, request = await observed_case(tmp_path, monkeypatch)
    with bind_execution_authority(context, worker_id='local-worker'):
        decision = await policy.invoke(request, role=cast(Role, context.actor_role))
    assert decision.status is CapabilityStatus.ALLOWED
    assert repository.waited == 0
    assert len(executor.calls) == 1  # review itself never executes the candidate

@pytest.mark.parametrize('defect', ['tenant_ask', 'user_disabled', 'run_default', 'missing_command',
    'other_backend', 'recipe_changed', 'plugin_ask', 'plugin_deny', 'foreign_user', 'supply', 'external'])
async def test_current_policy_and_exact_recipe_are_not_widened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str) -> None:
    from uuid import uuid4

    from agent_hub.capabilities.policy import CapabilityRule
    from agent_hub.capabilities.types import PolicyEffect

    _, repo, _, ctx, policy, request = await observed_case(tmp_path, monkeypatch)
    if defect == 'tenant_ask': repo.tenant_mode = 'ask'
    elif defect == 'user_disabled': repo.user_disabled = True
    elif defect == 'run_default': repo.row.routing_decision['permission_mode'] = 'default'
    elif defect == 'missing_command': repo.row.routing_decision['requested_permissions'].remove('command.run')
    elif defect == 'other_backend': repo.row.routing_decision['execution_backend'] = 'docker'
    elif defect == 'recipe_changed': repo.row.routing_decision['project_input_preparation']['python_recipe']['recipe_sha256'] = '0' * 64
    elif defect.startswith('plugin_'):
        from types import SimpleNamespace
        effect = PolicyEffect.REQUIRE_APPROVAL if defect == 'plugin_ask' else PolicyEffect.DENY
        policy._plugin_policy_rules = SimpleNamespace(capability_policy_rules=lambda tenant: (
            CapabilityRule(tenant, cast(Role, ctx.actor_role), request.agent_id, 'command', 'run', request.resource, effect),))
    elif defect == 'foreign_user': request = request.model_copy(update={'user_id': uuid4()})
    elif defect == 'supply': request = request.model_copy(update={'resource': request.resource.replace('project-verification', 'project-inputs')})
    elif defect == 'external': request = request.model_copy(update={'tool_origin': 'mcp'})
    before = len(repo.receipts)
    with bind_execution_authority(ctx, worker_id='local-worker'):
        assert not await policy.python_recipe_review.review(request, cast(Role, ctx.actor_role), create=True)
    assert len(repo.receipts) == before


async def test_audited_recipe_rechecks_tenant_policy_before_backend_execute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verifier, repo, executor, ctx, policy, request = await observed_case(tmp_path, monkeypatch)
    verifier._python.recipe_review = policy.python_recipe_review
    with bind_execution_authority(ctx, worker_id='local-worker'):
        assert (await policy.invoke(request, role=cast(Role, ctx.actor_role))).status is CapabilityStatus.ALLOWED
        selected = cast(_Context, await verifier._python.select(ctx.tenant_id, ctx.run_id, 'project_engineer'))
        await verifier._python._approval(selected, 'project_engineer')
        repo.tenant_mode = 'ask'
        from agent_hub.capabilities.project_verification import ProjectVerificationError
        with pytest.raises(ProjectVerificationError, match='install_approval_required'):
            await verifier._python._approval(selected, 'project_engineer')
    assert len(executor.calls) == 1


async def pending_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ObservedCase:
    import hashlib
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from uuid import uuid4

    from agent_hub.capabilities.approvals import (
        capability_approval_scope,
        fingerprint_capability_request,
    )
    from agent_hub.db.models import RunApprovalRow, RunOutboxRow
    from agent_hub.domain.runs import RunStatus
    from agent_hub.runtime.contracts import RuntimeCheckpoint
    from tests.unit.runs.test_original_python_offline_review import (
        LocalSession,
        LocalSessionFactory,
    )

    verifier, repo, executor, ctx, policy, req = await observed_case(tmp_path, monkeypatch)
    aid = 'approval_verify_recipe'
    ahash = hashlib.sha256(b'{}').hexdigest()
    idem = hashlib.sha256(f'{ctx.run_id}:project_engineer:0:project.verify:{ahash}'.encode()).hexdigest()
    req = req.model_copy(update={'idempotency_key': idem})
    cp = RuntimeCheckpoint(id=uuid4(), tenant_id=ctx.tenant_id, run_id=ctx.run_id,
        runtime_type='autogen', runtime_version='2', mode=ctx.mode, state=_freeze_object({
            'terminal': False, 'model_ledger': [{'status': 'succeeded'}],
            'tool_ledger': [{'status': 'waiting_approval', 'actor': 'project_engineer', 'name': 'project.verify',
                'approval_id': aid, 'position': 0, 'arguments_hash': ahash, 'idempotency_key': idem, 'replay_safe': False}],
            'timeout_seconds': 123.0, 'absolute_remaining_seconds': 1345.0,
        }, name='pending checkpoint fixture'))
    scope = capability_approval_scope(req)
    fp = fingerprint_capability_request(req)
    repo.row.routing_decision = {**repo.row.routing_decision, 'approval_kind': 'capability_tool',
        'approval_id': aid, 'approval_scope': scope, 'approval_fingerprint': fp,
        'approval_checkpoint_required': True, 'approval_review_profile': {'config_sha256': 'c' * 64}}
    repo.row.status = RunStatus.WAITING_APPROVAL.value
    repo.row.worker_id = repo.row.worker_lease_token = None
    repo.row.worker_lease_expires_at = repo.row.worker_heartbeat_at = None
    repo.receipts.append(RunApprovalRow(tenant_id=ctx.tenant_id, run_id=ctx.run_id,
        approval_id=aid, action='capability_tool', status='pending', payload={
            'approval_id': aid, 'approval_scope': scope, 'approval_fingerprint': fp,
            'python_binding': req.arguments, 'status': 'pending'}))
    repo.outbox = []
    repo.checkpoint = cp
    repo.events_after_checkpoint = False
    repo.pool = SimpleNamespace(tenant_id=ctx.tenant_id, run_id=ctx.run_id, actor_id=ctx.actor_id,
        actor_role=cast(Role, ctx.actor_role).value, config_sha256='c' * 64, original_token_grant=10_000_000,
        token_charged=133404, closed=False, original_deadline=datetime.now(UTC) + timedelta(minutes=5))
    repo.unresolved_attempt = None

    class Session(LocalSession):
        async def scalar(self, statement: Select[tuple[object, ...]]) -> object:
            table = cast(Table, statement.get_final_froms()[0]).name
            if table == 'agent_hub_run_approvals':
                assert self.lock_order[:3] == ['run', 'user', 'settings']
                approval_id = statement.compile().params['approval_id_1']
                return next((r for r in repo.receipts if r.approval_id == approval_id), None)
            if table == 'agent_hub_run_admission_pools': return repo.pool
            if table == 'agent_hub_run_admission_attempts': return repo.unresolved_attempt
            if table == 'agent_hub_run_checkpoints': return 10
            if table == 'agent_hub_run_events': return 11 if repo.events_after_checkpoint else None
            return cast(object, await super().scalar(statement))
        async def scalars(self, statement: Select[tuple[object, ...]]) -> object:
            if cast(Table, statement.get_final_froms()[0]).name == 'agent_hub_runs':
                return SimpleNamespace(all=lambda: [repo.row])
            return cast(object, await super().scalars(statement))
        async def flush(self) -> None:
            ids = [(r.run_id, r.approval_id) for r in repo.receipts]
            if len(ids) != len(set(ids)):
                raise RuntimeError('unit_emulated_unique_run_approval_id')
            await super().flush()
        def add(self, row: RunOutboxRow | RunApprovalRow) -> None:
            cast(list[RunOutboxRow | RunApprovalRow], repo.outbox if isinstance(row, RunOutboxRow) else repo.receipts).append(row)
    class Factory(LocalSessionFactory):
        def __call__(self) -> Session: return Session(repo)
    # Keep the actual PersistentToolApprovalMode bound to this same factory.
    reader = cast(PersistentToolApprovalMode, policy._tool_approval_mode)
    factory = Factory(repo)
    repo._session_factory = reader.service._session_factory = cast(async_sessionmaker[AsyncSession], factory)
    async def latest(*args: object, **kwargs: object) -> RuntimeCheckpoint: return repo.checkpoint
    monkeypatch.setattr(repo, 'latest_checkpoint', latest)
    return verifier, repo, executor, ctx, policy, req


async def test_existing_pending_rechecks_and_enqueues_same_run_once_without_manual_consent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.domain.runs import RunStatus
    _, repo, executor, ctx, policy, _ = await pending_case(tmp_path, monkeypatch)
    saved_cp = repo.checkpoint.to_payload()
    saved_pool = vars(repo.pool).copy()
    created, version = repo.row.created_at, repo.row.version
    assert await policy.python_recipe_review.reevaluate_pending(10) == 1
    assert repo.row.status == RunStatus.QUEUED.value and repo.row.version == version + 1
    assert repo.row.created_at == created and repo.checkpoint.to_payload() == saved_cp
    assert vars(repo.pool) == saved_pool
    assert repo.row.worker_lease_token is None
    assert len(repo.outbox) == 1 and repo.outbox[0].run_id == ctx.run_id
    original = next(r for r in repo.receipts if r.action == 'capability_tool')
    audit = next(r for r in repo.receipts if r.action == 'capability_tool.auto_review')
    assert original.payload['automatic_review_id'] == audit.approval_id
    assert audit.payload['reviewer'] == 'python_recipe_task_policy'
    assert 'decision_scope' not in original.payload
    assert await policy.python_recipe_review.reevaluate_pending(10) == 0
    assert len(repo.outbox) == 1 and len(executor.calls) == 1


@pytest.mark.parametrize('defect', ['tenant_ask', 'unknown_model', 'running_model', 'event_after_checkpoint',
    'fingerprint', 'binding', 'foreign_approval', 'version_race', 'active_lease', 'manual_rejected'])
async def test_pending_recheck_keeps_failed_fences_and_unknown_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str) -> None:
    from uuid import uuid4

    from agent_hub.runtime.contracts import RuntimeCheckpoint
    _, repo, executor, _ctx, policy, _ = await pending_case(tmp_path, monkeypatch)
    before_version = repo.row.version
    if defect == 'tenant_ask': repo.tenant_mode = 'ask'
    elif defect in ('unknown_model', 'running_model'):
        cp = repo.checkpoint.to_payload()
        cast(list[dict[str, object]], cast(dict[str, object], cp['state'])['model_ledger'])[0]['status'] = 'unknown' if defect == 'unknown_model' else 'running'
        cp['state_sha256'] = ''
        repo.checkpoint = RuntimeCheckpoint.from_payload(cp)
    elif defect == 'event_after_checkpoint': repo.events_after_checkpoint = True
    elif defect == 'fingerprint': repo.row.routing_decision['approval_fingerprint'] = '0' * 64
    elif defect == 'binding': repo.receipts[-1].payload['python_binding'] = {}
    elif defect == 'foreign_approval': repo.row.routing_decision['approval_id'] = 'approval_other'
    elif defect == 'active_lease': repo.row.worker_lease_token = uuid4()
    elif defect == 'manual_rejected': repo.receipts[-1].status = 'rejected'
    elif defect == 'version_race':
        original_source = policy.python_recipe_review._source
        async def changed(req: CapabilityRequest) -> dict[str, object]:
            source = await original_source(req)
            repo.row.version += 1
            return source
        monkeypatch.setattr(policy.python_recipe_review, '_source', changed)
    assert await policy.python_recipe_review.reevaluate_pending(10) == 0
    assert not repo.outbox and not any(r.action == 'capability_tool.auto_review' for r in repo.receipts)
    assert len(executor.calls) == 1
    assert repo.row.version == before_version + int(defect == 'version_race')


@pytest.mark.parametrize('defect', ['missing', 'expired', 'closed', 'exhausted', 'foreign_owner',
    'config_changed', 'no_profile', 'unresolved_attempt'])
async def test_pending_never_reopens_or_refills_original_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str) -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4
    _, repo, executor, _, policy, _ = await pending_case(tmp_path, monkeypatch)
    if defect == 'missing': repo.pool = None
    elif defect == 'expired': cast(SimpleNamespace, repo.pool).original_deadline = datetime.now(UTC) - timedelta(seconds=1)
    elif defect == 'closed': cast(SimpleNamespace, repo.pool).closed = True
    elif defect == 'exhausted': cast(SimpleNamespace, repo.pool).token_charged = cast(SimpleNamespace, repo.pool).original_token_grant
    elif defect == 'foreign_owner': cast(SimpleNamespace, repo.pool).actor_id = uuid4()
    elif defect == 'config_changed': cast(SimpleNamespace, repo.pool).config_sha256 = 'd' * 64
    elif defect == 'no_profile': repo.row.routing_decision.pop('approval_review_profile')
    elif defect == 'unresolved_attempt': repo.unresolved_attempt = uuid4()
    before = None if repo.pool is None else vars(repo.pool).copy()
    assert await policy.python_recipe_review.reevaluate_pending(10) == 0
    assert (None if repo.pool is None else vars(repo.pool)) == before
    assert not repo.outbox and len(executor.calls) == 1

async def test_real_harness_runtime_verifier_executes_candidate_after_automatic_recipe_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
    from agent_hub.harness.tool_gateway import HarnessToolGateway
    from agent_hub.harness.types import HarnessToolCallRequest

    verifier, repo, executor, ctx, policy, _ = await observed_case(tmp_path, monkeypatch)
    verifier._python.recipe_review = policy.python_recipe_review
    executor.failure_index = None
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills', run_repository=repo,
        project_workspace_dir=tmp_path, generated_artifact_dir=tmp_path / 'artifacts')
    backend._project_verification = verifier
    backend._project_executor_configured = True
    harness = HarnessToolGateway(backend, policy_gateway=policy, raise_backend_errors=True)
    call = HarnessToolCallRequest(run_id=ctx.run_id, actor='project_engineer', tool_name='project.verify',
        arguments={}, approval_required=False, sandbox='workspace_write', idempotency_key='candidate-native')
    with bind_execution_authority(ctx, worker_id='local-worker'):
        result = await harness.invoke(ctx.tenant_id, call, user_id=cast(UUID, ctx.actor_id), role=cast(Role, ctx.actor_role))
    assert result.status == 'succeeded', result.failure_reason
    assert len(executor.calls) == 2 and executor.calls[-1].source_kind == 'candidate'
    assert repo.waited == 0
    assert any(r.payload.get('reviewer') == 'python_recipe_task_policy' for r in repo.receipts)

async def test_pending_reuses_exact_existing_approved_auto_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, repo, _, ctx, policy, req = await pending_case(tmp_path, monkeypatch)
    review = policy.python_recipe_review
    binding = review._binding(repo.row, req, cast(Role, ctx.actor_role))
    binding.update(await review._source(req))
    async with repo._session_factory() as session, session.begin():
        await session.scalar(repo._run_select(req.tenant_id, req.run_id).with_for_update())
        await review._locked(session, repo.row, req, cast(Role, ctx.actor_role))
        await review._audit(session, repo.row, req, binding)
    before = len(repo.receipts)
    assert await review.reevaluate_pending(10) == 1
    assert len(repo.receipts) == before
    assert len(repo.outbox) == 1


@pytest.mark.parametrize('conflict', ['payload', 'denied'])
async def test_pending_rejects_conflicting_existing_audit_without_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str) -> None:
    _, repo, _, ctx, policy, req = await pending_case(tmp_path, monkeypatch)
    review = policy.python_recipe_review
    binding = review._binding(repo.row, req, cast(Role, ctx.actor_role))
    binding.update(await review._source(req))
    async with repo._session_factory() as session, session.begin():
        await session.scalar(repo._run_select(req.tenant_id, req.run_id).with_for_update())
        await review._locked(session, repo.row, req, cast(Role, ctx.actor_role))
        await review._audit(session, repo.row, req, binding)
    audit = repo.receipts[-1]
    if conflict == 'payload': audit.payload = {**audit.payload, 'reviewer': 'other'}
    else: audit.status = 'denied'
    before = (audit.status, dict(audit.payload))
    assert await review.reevaluate_pending(10) == 0
    assert (audit.status, audit.payload) == before
    assert not repo.outbox

@pytest.mark.parametrize('ceiling', ['none', 'tenant_ask', 'plugin_deny'])
async def test_real_concrete_gate_recipe_dispatch_keeps_live_policy_fences(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ceiling: str) -> None:
    from types import SimpleNamespace

    from agent_hub.auth.models import AuthenticatedPrincipal
    from agent_hub.capabilities.policy import CapabilityRule
    from agent_hub.capabilities.review_context import (
        ConcreteModelReviewGate,
        TrustedReviewContextLoader,
    )
    from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
    from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
    from agent_hub.capabilities.types import PolicyEffect
    from agent_hub.harness.tool_gateway import HarnessToolGateway
    from agent_hub.harness.types import HarnessToolCallRequest
    from agent_hub.runs.authorization import _capture_authenticated_input

    verifier, repo, executor, ctx, policy, request = await observed_case(tmp_path, monkeypatch)
    verifier._python.recipe_review = policy.python_recipe_review
    executor.failure_index = None
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills', run_repository=repo,
        project_workspace_dir=tmp_path, generated_artifact_dir=tmp_path / 'artifacts')
    backend._project_verification = verifier
    backend._project_executor_configured = True
    evidence = _capture_authenticated_input(AuthenticatedPrincipal(cast(UUID, ctx.actor_id), ctx.tenant_id, cast(Role, ctx.actor_role)),
                                             ctx.request, {'mode': ctx.mode.value})
    async def original_input(*args: object) -> object: return evidence
    monkeypatch.setattr(repo, 'load_authenticated_input', original_input)
    async def config(_tenant: UUID) -> ApprovalReviewConfig: return ApprovalReviewConfig(engine='rules_then_model', reviewer_logical_model='reviewer', max_cost_usd_per_run='10.00')
    class NoModelOrPermit:
        async def review(self, *args: object) -> Never: pytest.fail('rules authorization must not invent a model verdict')
        async def lookup_effect(self, *args: object) -> Never: pytest.fail('rules authorization must not consume model permits')
        async def consume_permit(self, **kwargs: object) -> Never: pytest.fail('rules authorization must not consume model permits')
    gate = ConcreteModelReviewGate(TrustedReviewContextLoader(repo,
        preparation_reader=backend.action_preparation_snapshot, config_getter=config), NoModelOrPermit(), NoModelOrPermit())
    invocations = []
    original_invocation = gate.invocation
    def record_invocation(**kwargs: Unpack[_InvocationArgs]) -> _ConcreteReviewCall:
        owned = original_invocation(**kwargs)
        invocations.append(owned)
        return owned
    monkeypatch.setattr(gate, 'invocation', record_invocation)
    harness = HarnessToolGateway(backend, policy_gateway=policy, review_gate=gate, raise_backend_errors=True)
    async def no_legacy(*args: object, **kwargs: object) -> Never: pytest.fail('must execute invoke_concrete, not legacy invoke')
    monkeypatch.setattr(policy, 'invoke', no_legacy)
    if ceiling == 'tenant_ask': repo.tenant_mode = 'ask'
    elif ceiling == 'plugin_deny':
        policy._plugin_policy_rules = SimpleNamespace(capability_policy_rules=lambda tenant: (
            CapabilityRule(tenant, cast(Role, ctx.actor_role), request.agent_id, 'command', 'run', request.resource, PolicyEffect.DENY),))
    call = HarnessToolCallRequest(run_id=ctx.run_id, actor='project_engineer', tool_name='project.verify',
        arguments={}, approval_required=False, sandbox='workspace_write', idempotency_key='concrete-candidate')
    with bind_execution_authority(ctx, worker_id='local-worker'):
        result = await harness.invoke(ctx.tenant_id, call, user_id=cast(UUID, ctx.actor_id), role=cast(Role, ctx.actor_role))
    assert result.status == ('succeeded' if ceiling == 'none' else 'failed'), (result.failure_reason, [(c._check is not None, c._dispatched, c._dispatch_ready, c._identity_check is not None) for c in invocations])
    assert len(executor.calls) == (2 if ceiling == 'none' else 1)
    if ceiling == 'none':
        assert repo.waited == 0 and executor.calls[-1].source_kind == 'candidate'
        assert invocations[0]._dispatch_ready and invocations[0]._dispatched
        assert not invocations[0]._model_permit_consumed
