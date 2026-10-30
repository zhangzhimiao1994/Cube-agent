"""Actual configured factories and policy; model/capacity/SQL transport are offline."""

from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.models.types import ToolCall
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime import defaults
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime, _checkpoint_ack_selected
from agent_hub.runtime.autogen.engineering_delivery import engineering_delivery_required
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.project_scale_artifact import (
    ProjectScaleArtifactPreseedRuntime,
    is_project_scale_preseed_request,
    project_scale_artifact_zip_arguments,
)
from tests.unit.runs.test_repository_approval_upserts import ApprovalProjectionSession
from tests.unit.runtime.test_action_admission_helper import (
    NoExecutionBackend,
    OfflineCapacity,
    OfflineTransport,
    configured_document,
    factory,
    response,
    task,
)
from tests.unit.runtime.test_configured_runtime import FakeConfigService, FakeSecretService


@pytest.mark.parametrize('mode', [TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize('permission_mode', ['default', 'auto_review'])
async def test_fresh_real_factory_reaches_model_without_dummy_tool_approval(
    mode: TaskMode, permission_mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = ApprovalProjectionSession()
    ctx = task(db, mode, permission_mode)
    db.run.routing_decision.update(engineering_action_admission_version=1,
        engineering_delivery_contract_version=1, engineering_action_version=1)
    ctx = ctx.model_copy(update={'routing_decision': db.run.routing_decision})
    transport, backend = OfflineTransport([]), NoExecutionBackend()
    monkeypatch.setattr(defaults, 'CapacityPool', OfflineCapacity)
    monkeypatch.setenv('AGENT_HUB_CREWAI_STORAGE_DIR', str(tmp_path / 'crewai'))
    runtime, _ = factory(mode, db, transport, backend)
    events = []
    try:
        async for event in runtime.run(ctx):
            events.append(event)
    except RuntimeError:
        # Provider intentionally stops after proving real model entry; no delivery claim.
        assert transport.requests
    assert transport.requests, 'fresh run must not stop at empty-argument startup approval'
    assert backend.effects == 0 and db.approvals == {}
    assert not any(event.checkpoint is not None
                   and event.checkpoint.runtime_type == 'engineering_admission' for event in events)


@pytest.mark.parametrize('mode', [TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize('policy', ['engineering_delivery_contract_version', 'engineering_action_version'])
@pytest.mark.parametrize('marker', [None, True, '1', 0, 2, 1.0])
async def test_factory_attestation_rejects_missing_or_malformed_engineering_policy(
    mode: TaskMode, policy: str, marker: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = ApprovalProjectionSession()
    ctx = task(db, mode, 'default')
    db.run.routing_decision.update(engineering_action_admission_version=1,
        engineering_delivery_contract_version=1, engineering_action_version=1)
    if marker is None:
        db.run.routing_decision.pop(policy)
    else:
        db.run.routing_decision[policy] = marker
    ctx = ctx.model_copy(update={'routing_decision': db.run.routing_decision})
    transport, backend = OfflineTransport([]), NoExecutionBackend()
    monkeypatch.setattr(defaults, 'CapacityPool', OfflineCapacity)
    monkeypatch.setenv('AGENT_HUB_CREWAI_STORAGE_DIR', str(tmp_path / 'crewai'))
    runtime, _ = factory(mode, db, transport, backend)
    planned = await runtime._runtime_for(ctx)
    assert defaults._configured_concrete_actions(planned, ctx,
        capability_gateway=backend, harness_tool_gateway=runtime._harness_tool_gateway,
        artifact_repository=runtime._artifact_repository, pending=None) is False
    assert not transport.requests and backend.effects == 0


@pytest.mark.parametrize('ack', [None, 1])
@pytest.mark.parametrize('mode', [TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_actual_engineering_autogen_without_ack_publication_keeps_legacy_gate(
    ack: int | None, mode: TaskMode, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = ApprovalProjectionSession()
    ctx = task(db, mode, 'default')
    db.run.routing_decision.update(engineering_action_admission_version=1,
        engineering_delivery_contract_version=1, engineering_action_version=1,
        project_delivery='workspace', artifact_strategy='workspace_bundle', project_scale='small')
    if ack is not None:
        db.run.routing_decision['engineering_checkpoint_ack_version'] = ack
    ctx = ctx.model_copy(update={'routing_decision': db.run.routing_decision})
    transport, backend = OfflineTransport([]), NoExecutionBackend()
    monkeypatch.setattr(defaults, 'CapacityPool', OfflineCapacity)
    monkeypatch.setenv('AGENT_HUB_CREWAI_STORAGE_DIR', str(tmp_path / 'crewai'))
    runtime, _ = factory(mode, db, transport, backend)
    planned = await runtime._runtime_for(ctx)
    child = planned._child
    if mode is TaskMode.HYBRID:
        assert isinstance(child, HybridRuntime)
        child = child._discussion
        assert isinstance(child, AutoGenDiscussionRuntime)
        participants = child._plan.participants
        child._plan = replace(child._plan, participants=(
            replace(participants[0], allowed_tools=('project.inspect', 'workspace.write_text')),
            *participants[1:],
        ))
    assert isinstance(child, AutoGenDiscussionRuntime)
    allowed = {participant.id: participant.allowed_tools for participant in child._plan.participants}
    child_context = ctx.model_copy(update={'mode': TaskMode.DISCUSS})
    assert engineering_delivery_required(child_context, allowed)
    assert not _checkpoint_ack_selected(child_context)
    assert defaults._configured_concrete_actions(planned, ctx,
        capability_gateway=backend, harness_tool_gateway=runtime._harness_tool_gateway,
        artifact_repository=runtime._artifact_repository, pending=None) is False
    assert not transport.requests and backend.effects == 0


class _ConcreteSession(ApprovalProjectionSession):
    async def scalar(self, statement: Any) -> object:
        if 'agent_hub_conversations' in str(statement):
            return None  # This fixture has no existing conversation approval grant.
        return await super().scalar(statement)


def _concrete_factory(
    db: _ConcreteSession, mode: TaskMode, transport: OfflineTransport,
    backend: NoExecutionBackend, monkeypatch: pytest.MonkeyPatch,
    *, effect: PolicyEffect = PolicyEffect.REQUIRE_APPROVAL,
) -> tuple[Any, DefaultRuntimeCapabilityPolicyGateway, list[tuple[CapabilityRequest, Any]]]:
    class Rules:
        def capability_policy_rules(self, tenant_id: Any) -> tuple[CapabilityRule, ...]:
            assert tenant_id == db.run.tenant_id
            return (
                CapabilityRule(tenant_id, Role.OPERATOR, None, 'file', 'write',
                               'workspace/current', effect),
                CapabilityRule(tenant_id, Role.OPERATOR, None, 'file', 'create',
                               'generated/project.generate_zip', effect),
            )

    repo = RunRepository(cast(Any, db))
    policy = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repo, plugin_policy_rules=Rules(),
    )
    decisions: list[tuple[CapabilityRequest, Any]] = []
    invoke = policy.invoke

    async def record(request: CapabilityRequest, *, role: Role) -> Any:
        result = await invoke(request, role=role)
        decisions.append((request, result))
        return result

    monkeypatch.setattr(policy, 'invoke', record)
    document = configured_document()
    runtime = defaults.configured_runtime_registry(
        config_service=cast(Any, FakeConfigService(document)),
        secret_service=cast(Any, FakeSecretService()), redis_client=object(),
        transport=transport, capability_gateway=backend,
        harness_tool_gateway=HarnessToolGateway(backend, policy_gateway=policy,
                                               require_actor_identity=True),
        artifact_repository=InMemoryArtifactRepository(),
    ).get(mode)
    return runtime, policy, decisions


def _assert_pending_action(db: _ConcreteSession, request: CapabilityRequest, result: Any) -> None:
    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert result.reason == 'capability requires approval'
    assert result.approval_id is not None and len(db.approvals) == 1
    row = db.approvals[result.approval_id]
    assert row.tenant_id == request.tenant_id == db.run.tenant_id
    assert row.run_id == request.run_id == db.run.id and request.user_id == db.run.actor_id
    assert row.status == 'pending' and row.action == 'capability_tool'
    assert row.payload['approval_fingerprint'] == fingerprint_capability_request(request)
    assert row.payload['approval_scope'] == capability_approval_scope(request)
    assert db.run.routing_decision['approval_id'] == result.approval_id
    assert db.run.routing_decision['approval_scope'] == capability_approval_scope(request)
    assert db.run.status == 'waiting_approval'
    changed = request.model_copy(update={'arguments': {**request.arguments, 'content': 'changed'}})
    assert fingerprint_capability_request(changed) != row.payload['approval_fingerprint']


@pytest.mark.parametrize('marker', [None, True, '1', 2])
async def test_old_or_malformed_factory_policy_requires_concrete_human_approval(
    marker: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _ConcreteSession()
    ctx = task(db, TaskMode.DISCUSS, 'default')
    if marker is not None:
        db.run.routing_decision['engineering_action_admission_version'] = marker
    db.run.routing_decision.update(project_delivery='workspace', artifact_strategy='workspace_bundle',
        project_scale='small', engineering_delivery_contract_version=1, engineering_final_report_version=1)
    args = {'path': 'pending-note.txt', 'content': 'This note requires concrete consent.\n'}
    db.run.request = 'Save pending-note.txt with exactly this content: ' + args['content']
    ctx = ctx.model_copy(update={'request': db.run.request, 'routing_decision': db.run.routing_decision})
    backend = NoExecutionBackend()

    class ProposalTransport(OfflineTransport):
        async def complete(self, deployment: Any, request: Any, api_key: str) -> Any:
            assert len(self.requests) < 2, 'approval must stop further model planning'
            assert not db.approvals and backend.effects == 0
            self.requests.append(request)
            self.completed.append(request)
            if not request.tools:
                return response('project_engineer')
            assert 'workspace_write_text' in {tool.name for tool in request.tools}
            return response(None, tools=(ToolCall(
                id='pending-write', name='workspace_write_text', arguments=args,
            ),))

    transport = ProposalTransport([])
    monkeypatch.setattr(defaults, 'CapacityPool', OfflineCapacity)
    runtime, policy, decisions = _concrete_factory(db, ctx.mode, transport, backend, monkeypatch)
    # Wrong actor/tenant must fail before either static discovery or concrete consent.
    candidate = CapabilityRequest(tenant_id=ctx.tenant_id, user_id=ctx.actor_id,
        run_id=ctx.run_id, agent_id='project_engineer', capability='file', operation='write',
        resource='workspace/current', arguments=args, idempotency_key='identity-negative')
    for changed in ({'user_id': uuid4()}, {'tenant_id': uuid4()}):
        bad = candidate.model_copy(update=changed)
        assert (await policy.authorize_static(bad, role=Role.OPERATOR)).status is CapabilityStatus.DENIED
        assert (await policy.invoke(bad, role=Role.OPERATOR)).status is CapabilityStatus.DENIED
    assert not db.approvals and not transport.requests and backend.effects == 0
    decisions.clear()
    events = []
    async with aclosing(runtime.run(ctx)) as stream:
        async for event in stream:
            events.append(event)
            if (event.checkpoint is not None and db.approvals
                    and checkpoint_waits_for_approval(event.checkpoint, next(iter(db.approvals)))):
                break
    assert len(transport.requests) == len(transport.completed) == 2
    assert len(decisions) == 1 and backend.effects == 0
    request, result = decisions[0]
    assert (request.agent_id, request.capability, request.operation, request.resource) == (
        'project_engineer', 'file', 'write', 'workspace/current')
    assert request.arguments == args
    _assert_pending_action(db, request, result)
    assert any(event.checkpoint is not None
               and checkpoint_waits_for_approval(event.checkpoint, result.approval_id) for event in events)
    route = db.run.routing_decision
    assert route['conversation_id'] == 'fixed-legacy-admission'
    assert (route['project_id'], route['workspace_session_id']) == ('project', 'main')
    assert route.get('engineering_action_admission_version') == marker
    assert type(route.get('engineering_action_admission_version')) is type(marker)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert not any(event.checkpoint is not None and event.checkpoint.runtime_type == 'engineering_admission'
                   for event in events)


@pytest.mark.parametrize('mode', [TaskMode.DISPATCH, TaskMode.HYBRID])
async def test_configured_preseed_requires_concrete_zip_approval_before_backend(
    mode: TaskMode, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = _ConcreteSession()
    ctx = task(db, mode, 'default')
    db.run.request = 'Project-scale acceptance fixture: build the small team task workspace ZIP.'
    db.run.routing_decision.update(engineering_action_admission_version=1,
        engineering_delivery_contract_version=1, engineering_action_version=1,
        project_delivery='workspace', artifact_strategy='workspace_bundle', project_scale='small')
    ctx = ctx.model_copy(update={'request': db.run.request, 'routing_decision': db.run.routing_decision})
    assert is_project_scale_preseed_request(ctx.request)
    arguments = project_scale_artifact_zip_arguments(ctx)
    assert arguments is not None and arguments['project_id'] == 'project'
    assert arguments['workspace_session_id'] == 'main'
    transport, backend = OfflineTransport([]), NoExecutionBackend()
    backend.available.add('project.generate_zip')
    monkeypatch.setattr(defaults, 'CapacityPool', OfflineCapacity)
    monkeypatch.setenv('AGENT_HUB_CREWAI_STORAGE_DIR', str(tmp_path / 'crewai'))
    runtime, _, decisions = _concrete_factory(db, mode, transport, backend, monkeypatch)
    planned = await runtime._runtime_for(ctx)
    child = planned._child
    if mode is TaskMode.HYBRID:
        assert isinstance(child, HybridRuntime)
        assert isinstance(child._stages()[0][0], ProjectScaleArtifactPreseedRuntime)
    else:
        assert isinstance(child, ProjectScaleArtifactPreseedRuntime)
    concrete = defaults._configured_concrete_actions(planned, ctx,
        capability_gateway=backend, harness_tool_gateway=runtime._harness_tool_gateway,
        artifact_repository=runtime._artifact_repository, pending=None)
    assert concrete is False
    calls: list[dict[str, Any]] = []
    harness = runtime._harness_tool_gateway
    invoke = harness.invoke

    async def trace(*args: Any, **kwargs: Any) -> Any:
        request = args[1]
        assert not transport.requests and backend.effects == 0
        result = await invoke(*args, **kwargs)
        calls.append({'tenant': args[0], 'request': request, 'user': kwargs['user_id'],
                      'role': kwargs['role'], 'result': result})
        return result

    monkeypatch.setattr(harness, 'invoke', trace)
    events = []
    async with aclosing(runtime.run(ctx)) as stream:
        async for event in stream:
            events.append(event)
            if calls:
                break  # Observe the concrete wait; do not advance a waiting DB run into its child.
    assert not transport.requests and backend.effects == 0
    assert len(calls) == len(decisions) == 1
    call = calls[0]
    proposed, result = call['request'], call['result']
    assert (call['tenant'], call['user'], call['role']) == (ctx.tenant_id, ctx.actor_id, ctx.actor_role)
    assert proposed.tool_name == 'project.generate_zip' and proposed.actor == 'implementer'
    assert dict(proposed.arguments) == dict(arguments) and proposed.sandbox == 'workspace_write'
    assert result.status == 'failed' and result.failure_reason == 'capability requires approval'
    request, decision = decisions[0]
    assert (request.agent_id, request.capability, request.operation, request.resource) == (
        'implementer', 'file', 'create', 'generated/project.generate_zip')
    assert request.arguments == dict(arguments)
    _assert_pending_action(db, request, decision)
    assert result.payload == {'approval_id': decision.approval_id}
    assert any(event.kind is EventKind.TOOL_FAILED and event.tool_name == 'project.generate_zip'
               and event.reason == 'capability requires approval' for event in events)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert not any(event.checkpoint is not None and event.checkpoint.runtime_type == 'engineering_admission'
                   for event in events)


async def test_static_explicit_deny_blocks_model_and_tool_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _ConcreteSession()
    ctx = task(db, TaskMode.DISCUSS, 'default')
    assert 'engineering_action_admission_version' not in ctx.routing_decision
    transport, backend = OfflineTransport([]), NoExecutionBackend()
    monkeypatch.setattr(defaults, 'CapacityPool', OfflineCapacity)
    runtime, _, decisions = _concrete_factory(
        db, ctx.mode, transport, backend, monkeypatch, effect=PolicyEffect.DENY,
    )
    calls = []
    harness = runtime._harness_tool_gateway
    invoke = harness.invoke

    async def trace(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return await invoke(*args, **kwargs)

    monkeypatch.setattr(harness, 'invoke', trace)
    with pytest.raises(RuntimeError, match='engineering permission gate failed: denied'):
        async with aclosing(runtime.run(ctx)) as stream:
            _ = [event async for event in stream]
    assert not transport.requests and not transport.completed
    assert not calls and not decisions and not db.approvals and backend.effects == 0
    assert db.run.status == 'running'


async def test_actual_configured_discuss_asks_for_concrete_write_then_cold_resumes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session(ApprovalProjectionSession):
        async def scalar(self, statement: Any) -> object:
            if 'agent_hub_conversations' in str(statement):
                return None
            return await super().scalar(statement)

    db = Session()
    ctx = task(db, TaskMode.DISCUSS, 'default')
    db.run.routing_decision['engineering_action_admission_version'] = 1
    db.run.routing_decision.update(project_delivery='workspace', artifact_strategy='workspace_bundle',
                                   project_scale='small', engineering_delivery_contract_version=1,
                                   engineering_action_version=1,
                                   engineering_checkpoint_ack_version=1,
                                   engineering_final_report_version=1)
    ctx = ctx.model_copy(update={'routing_decision': db.run.routing_decision})
    # Match new Discuss submission policy: ack alone does not enable publication on plain runs.
    assert _checkpoint_ack_selected(ctx)
    repo, artifacts = RunRepository(cast(Any, db)), InMemoryArtifactRepository()

    class Rules:
        def capability_policy_rules(self, tenant_id: Any) -> tuple[CapabilityRule, ...]:
            from agent_hub.auth.models import Role
            return (CapabilityRule(tenant_id, Role.OPERATOR, None, 'file', 'write',
                                   'workspace/current', PolicyEffect.REQUIRE_APPROVAL),)

    policy = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repo,
                                                    plugin_policy_rules=Rules())
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills',
        project_workspace_dir=tmp_path / 'workspaces', run_repository=repo)
    backend_actions = []
    execute = backend.execute

    async def record_execute(**kwargs: Any) -> Any:
        backend_actions.append((kwargs['name'], kwargs['idempotency_key']))
        return await execute(**kwargs)

    monkeypatch.setattr(backend, 'execute', record_execute)
    harness = HarnessToolGateway(backend, policy_gateway=policy, require_actor_identity=True)
    document = configured_document()
    monkeypatch.setattr(defaults, 'CapacityPool', OfflineCapacity)
    args = {'path': 'reviewed.txt', 'content': 'Approved concrete action.\n'}

    def build(transport: OfflineTransport) -> Any:
        return defaults.configured_runtime_registry(
            config_service=cast(Any, FakeConfigService(document)),
            secret_service=cast(Any, FakeSecretService()), redis_client=object(),
            transport=transport, capability_gateway=backend, harness_tool_gateway=harness,
            artifact_repository=artifacts,
        ).get(TaskMode.DISCUSS)

    class ActionTransport(OfflineTransport):
        async def complete(self, deployment: Any, request: Any, api_key: str) -> Any:
            assert len(self.requests) < 4
            self.requests.append(request)
            self.completed.append(request)
            if request.required_tool == 'project_inspect':
                return response(None, tools=(
                    ToolCall(id='real-inspection', name='project_inspect', arguments={}),
                ))
            if not request.tools:
                return response('project_engineer')
            assert request.required_tool is None
            assert [name for name, _ in backend_actions] == ['project.inspect']
            return response(None, tools=(
                ToolCall(id='real-reviewed-write', name='workspace_write_text', arguments=args),
            ))

    transport = ActionTransport([])
    first = build(transport)
    events = []
    checkpoint = None
    async with aclosing(first.run(ctx)) as stream:
        async for event in stream:
            events.append(event)
            if event.checkpoint is not None and event.checkpoint.state.get('tool_ledger'):
                receipt = event.checkpoint.state['tool_ledger'][-1]
                if (receipt.get('status') == 'waiting_approval'
                        and checkpoint_waits_for_approval(event.checkpoint, receipt['approval_id'])):
                    checkpoint = event.checkpoint
                    break
    assert checkpoint is not None, [(event.kind, event.reason) for event in events]
    assert checkpoint is not None and checkpoint.runtime_type == 'autogen'
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert checkpoint.state['tool_ledger']
    row = checkpoint.state['tool_ledger'][-1]
    approval_id = row['approval_id']
    assert checkpoint_waits_for_approval(checkpoint, approval_id)
    assert len(transport.completed) == len(transport.requests) == 4 and len(db.approvals) == 1
    assert sum(request.required_tool == 'project_inspect' for request in transport.requests) == 1
    assert 'workspace_write_text' in {tool.name for tool in transport.completed[-1].tools}
    assert row['actor'] == 'project_engineer' and row['name'] == 'workspace.write_text'
    target = tmp_path / 'workspaces' / str(ctx.tenant_id) / 'projects/project/sessions/main/reviewed.txt'
    assert not target.exists()
    assert len(backend_actions) == 1 and backend_actions[0][0] == 'project.inspect'
    inspection = backend_actions[0]
    assert checkpoint.state['tool_ledger'][-2]['name'] == 'project.inspect'
    assert checkpoint.state['tool_ledger'][-2]['status'] == 'succeeded'
    await repo.approve_capability_and_enqueue(tenant_id=ctx.tenant_id, run_id=ctx.run_id,
        actor_id=ctx.actor_id, approval_id=approval_id, version=db.run.version)
    db.run.status = 'running'
    resumed_transport = OfflineTransport([])
    second = build(resumed_transport)
    await second.restore_checkpoint(checkpoint)
    restored = ctx.model_copy(update={'checkpoint': checkpoint})
    completed = []
    async with aclosing(second.run(restored)) as stream:
        async for event in stream:
            completed.append(event)
            if (event.checkpoint is not None and event.checkpoint.state['tool_ledger']
                    and event.checkpoint.state['tool_ledger'][-1]['status'] == 'succeeded'
                    and event.checkpoint.state['tool_ledger'][-1]['name'] == 'workspace.write_text'):
                break
    # This proves concrete approval recovery, not project acceptance or delivery.
    assert len(transport.completed) == len(transport.requests) == 4
    assert not resumed_transport.requests and not resumed_transport.completed
    assert backend_actions == [inspection, ('workspace.write_text', row['idempotency_key'])]
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in completed)
    assert target.read_text() == args['content'] and len(db.approvals) == 1
    assert not any(event.checkpoint is not None and event.checkpoint.runtime_type == 'engineering_admission'
                   for event in completed)
