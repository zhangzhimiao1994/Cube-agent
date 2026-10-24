"""Concrete Direct actions pause after model output, never at startup."""
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.project_scale_artifact import HarnessToolInvoker
from tests.unit.capabilities.test_scoped_read import FakeRunRepository, stored_run
from tests.unit.runtime.test_direct_project_verification import Model, batch, context


class Backend(RuntimeCapabilityGateway):
    def __init__(self, root: Path, ctx: TaskContext, repo: InMemoryArtifactRepository) -> None:
        super().__init__(skill_store_dir=root/'skills', project_workspace_dir=root/'workspaces',
            generated_artifact_dir=root/'generated', artifact_repository=repo,
            run_repository=FakeRunRepository(stored_run(tenant=ctx.tenant_id, run=ctx.run_id,
                project_id='direct-verification', session='same-task')))
        self.root = root/'workspaces'/str(ctx.tenant_id)/'projects'/'direct-verification'/'sessions'/'same-task'
        self.calls: list[str] = []

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        return False if name == 'project.verify' else super().is_available(tenant_id, name)

    async def execute(
        self, *, name: str, tenant_id: UUID, run_id: UUID, actor: str,
        arguments: Mapping[str, JsonValue], idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        self.calls.append(name)
        return await super().execute(name=name, tenant_id=tenant_id, run_id=run_id,
            actor=actor, arguments=arguments, idempotency_key=idempotency_key)


class Policy:
    def __init__(self) -> None:
        self.allowed: set[str] = set()
        self.requests: list[CapabilityRequest] = []

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        self.requests.append(request)
        if request.operation in self.allowed:
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        return CapabilityResult(CapabilityStatus.WAITING_APPROVAL, request.run_id,
            approval_id='approval_'+request.operation, reason='capability requires approval')


async def test_pause_at_actual_write_then_bundle_without_repeating_model_or_writes(tmp_path: Path) -> None:
    ctx = context()
    policy, repo = Policy(), InMemoryArtifactRepository()
    backend = Backend(tmp_path, ctx, repo)
    gateway = HarnessToolGateway(backend, policy_gateway=policy, require_actor_identity=True)
    model = Model(batch({'README.md': 'Actual content'}))
    def runtime() -> ActionApprovedDirectRuntime:
        return ActionApprovedDirectRuntime(model, logical_model='main', capability_gateway=backend,
                                           harness_tool_gateway=gateway, artifact_repository=repo)
    original = runtime()
    events = [e async for e in original.run(ctx)]
    assert len(model.requests) == 1
    cp = events[-1].checkpoint
    assert cp is not None and cp.state['tool_name'] == 'workspace.write_text'
    assert not backend.root.exists()
    assert policy.requests[-1].arguments['path'] == 'README.md'
    policy.allowed.add('write')
    resumed = runtime()
    await resumed.restore_checkpoint(cp)
    events = [e async for e in resumed.run(ctx.model_copy(update={'checkpoint': cp}))]
    cp = events[-1].checkpoint
    assert cp is not None and cp.state['tool_name'] == 'workspace.bundle'
    assert (backend.root/'README.md').read_text() == 'Actual content'
    writes = backend.calls.count('workspace.write_text')
    policy.allowed.add('create')
    final = runtime()
    await final.restore_checkpoint(cp)
    events = [e async for e in final.run(ctx.model_copy(update={'checkpoint': cp}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 1
    assert backend.calls.count('workspace.write_text') == writes
    assert backend.calls.count('workspace.bundle') == 1


async def test_large_file_request_uses_data_reference_and_resumes_exact_bytes(tmp_path: Path) -> None:
    ctx = context(token_budget=1_000_000)
    policy, repo = Policy(), InMemoryArtifactRepository()
    backend = Backend(tmp_path, ctx, repo)
    gateway = HarnessToolGateway(backend, policy_gateway=policy, require_actor_identity=True)
    text = 'Long file.\n'*20_000
    model = Model(batch({'README.md': text}))
    first = ActionApprovedDirectRuntime(model, logical_model='main', capability_gateway=backend,
                                       harness_tool_gateway=gateway, artifact_repository=repo)
    events = [e async for e in first.run(ctx)]
    cp = events[-1].checkpoint
    assert cp is not None and cp.state['tool_name'] == 'workspace.write_text'
    assert 'content_reference' in policy.requests[-1].arguments
    policy.allowed.update({'write', 'create'})
    resumed = ActionApprovedDirectRuntime(model, logical_model='main', capability_gateway=backend,
                                         harness_tool_gateway=gateway, artifact_repository=repo)
    await resumed.restore_checkpoint(cp)
    events = [e async for e in resumed.run(ctx.model_copy(update={'checkpoint': cp}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert (backend.root/'README.md').read_bytes() == text.encode()
    assert len(model.requests) == 1


async def test_changed_context_rejection_releases_runtime_and_stream_ownership(tmp_path: Path) -> None:
    from agent_hub.runtime.direct import RuntimeExecutionError
    ctx, policy, repo = context(), Policy(), InMemoryArtifactRepository()
    backend = Backend(tmp_path, ctx, repo)
    gateway = HarnessToolGateway(backend, policy_gateway=policy, require_actor_identity=True)
    model = Model(batch({'README.md': 'Actual content'}))
    first = ActionApprovedDirectRuntime(model, logical_model='main', capability_gateway=backend,
                                       harness_tool_gateway=gateway, artifact_repository=repo)
    events = [e async for e in first.run(ctx)]
    cp = events[-1].checkpoint
    assert cp is not None
    resumed = ActionApprovedDirectRuntime(model, logical_model='main', capability_gateway=backend,
                                         harness_tool_gateway=gateway, artifact_repository=repo)
    await resumed.restore_checkpoint(cp)
    with pytest.raises(RuntimeExecutionError, match='context changed'):
        _ = [e async for e in resumed.run(ctx.model_copy(update={'checkpoint': cp, 'request': 'Changed'}))]
    assert resumed._active_token is None
    assert resumed._active_stream is None


async def test_one_runtime_can_start_another_run_without_replaying_previous_actions(tmp_path: Path) -> None:
    ctx, policy, repo = context(), Policy(), InMemoryArtifactRepository()
    policy.allowed.update({'write', 'create'})
    backend = Backend(tmp_path, ctx, repo)
    gateway = HarnessToolGateway(backend, policy_gateway=policy, require_actor_identity=True)
    model = Model(batch({'README.md': 'First'}), batch({'README.md': 'Second'}))
    runtime = ActionApprovedDirectRuntime(model, logical_model='main', capability_gateway=backend,
                                         harness_tool_gateway=gateway, artifact_repository=repo)
    _ = [e async for e in runtime.run(ctx)]
    _ = [e async for e in runtime.run(ctx)]
    assert (backend.root/'README.md').read_text() == 'Second'
    assert len(model.requests) == 2


@pytest.mark.parametrize('native_first', [False, True])
async def test_generated_inspection_keeps_published_identity_across_approval_resume(
    tmp_path: Path, native_first: bool,
) -> None:
    from agent_hub.models.types import ToolCall
    from tests.unit.runtime.test_direct_generated_entry import Harness, Model, batch, response
    from tests.unit.runtime.test_generated_project_history import evidence

    ctx, verified, bundle = await evidence(tmp_path)
    ctx = ctx.model_copy(update={'token_budget': 50_000})
    repo = InMemoryArtifactRepository()

    class PausingHarness(Harness):
        allowed = False

        async def action_preparation_snapshot(self, context: TaskContext) -> dict[str, JsonValue]:
            from agent_hub.capabilities.action_preparation import CONTRACT_ROUTE_KEYS

            # Transport double only: this scene tests published inspection identity,
            # not real Root validation (covered by the separate integration tests).
            return {'schema_version': 1, 'scope': {'kind': 'private_root_transport_fake'},
                    'contracts': {key: context.routing_decision[key]
                                  for key in CONTRACT_ROUTE_KEYS if key in context.routing_decision},
                    'workspace_sha256': 'd' * 64}

        async def invoke(
            self, tenant: UUID, request: HarnessToolCallRequest, *,
            user_id: UUID | None = None, role: Role | None = None, **kwargs: object,
        ) -> HarnessToolCallResult:
            if request.tool_name in {'project.inspect', 'read_context'}:
                return await super().invoke(tenant, request, user_id=user_id, role=role, **kwargs)
            if request.tool_name == 'workspace.write_text' and not self.allowed:
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status='failed', failure_reason='capability requires approval',
                    payload={'approval_id': 'approval_generated_write'})
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status='succeeded', payload=bundle if request.tool_name == 'workspace.bundle'
                    else verified if request.tool_name == 'project.verify' else {})

    harness = PausingHarness(ctx, verified, bundle)
    first = (response(None, tools=(ToolCall('call-inspect', 'project.inspect', {}),)),) if native_first else ()
    model = Model(*first, response(batch()))

    def runtime() -> ActionApprovedDirectRuntime:
        return ActionApprovedDirectRuntime(model, logical_model='fixture', capability_gateway=harness,
            harness_tool_gateway=cast(HarnessToolInvoker, harness), artifact_repository=repo)

    initial = runtime()
    before = [event async for event in initial.run(ctx)]
    cp = before[-1].checkpoint
    assert cp is not None
    published = [event.artifact for event in before if event.artifact is not None]
    assert len(published) == 1
    harness.allowed = True
    resumed = runtime()
    await resumed.restore_checkpoint(cp)
    after = [event async for event in resumed.run(ctx.model_copy(update={
        'checkpoint': cp, 'artifacts': (*ctx.artifacts, *published)}))]
    final = next(event.artifact for event in after if event.artifact is not None
        and 'generated_event_layout' in event.artifact.content)
    layout = final.content['generated_event_layout']
    assert isinstance(layout, Mapping)
    assert layout['inspection_artifact_id'] == str(published[0].id)
    assert layout['inspection_artifact_sha256'] == published[0].content_sha256
    assert len(model.requests) == (2 if native_first else 1)
