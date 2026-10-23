"""Opt-in Direct action assembly preserves legacy runtimes and model policy."""
from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.config.service import ConfigService
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.project_verification_receipts import VerificationAuthority
from agent_hub.runs.repository import RunConflict
from agent_hub.runtime import defaults
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    ExecutionRuntime,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from agent_hub.security.secrets import SecretService
from tests.unit.runtime.test_configured_runtime import (
    TENANT_ID,
    FakeConfigService,
    FakeSecretService,
    FakeTransport,
    _immediate_capacity,
)
from tests.unit.runtime.test_run_model_scope import _document


@dataclass
class Stack:
    repository: InMemoryArtifactRepository
    action: defaults.RuntimeCapabilityGatewayProtocol
    action_tools: defaults.HarnessToolInvoker
    legacy: defaults.RuntimeCapabilityGatewayProtocol
    legacy_tools: defaults.HarnessToolInvoker
    transport: FakeTransport


def stack() -> Stack:
    return Stack(InMemoryArtifactRepository(),
        cast(defaults.RuntimeCapabilityGatewayProtocol, object()),
        cast(defaults.HarnessToolInvoker, object()),
        cast(defaults.RuntimeCapabilityGatewayProtocol, object()),
        cast(defaults.HarnessToolInvoker, object()), FakeTransport())


def configured(dependencies: Stack, **overrides: Any) -> defaults.ConfigBackedDirectRuntime:
    options: dict[str, Any] = {
        'config_service': FakeConfigService(_document()),
        'secret_service': FakeSecretService(), 'capacity_factory': _immediate_capacity,
        'transport': dependencies.transport,
        'capability_gateway': dependencies.legacy,
        'harness_tool_gateway': dependencies.legacy_tools,
        'artifact_repository': dependencies.repository,
        'action_capability_gateway': dependencies.action,
        'action_harness_tool_gateway': dependencies.action_tools,
        **overrides,
    }
    return defaults.ConfigBackedDirectRuntime(**options)


def task(**route: JsonValue) -> TaskContext:
    return TaskContext(run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DIRECT,
        request='Build the workspace project.', routing_decision={
            'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle',
            'project_id': 'project-a', 'workspace_session_id': 'session-a',
            'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd',
            'requested_permissions': ('workspace.read', 'workspace.write', 'command.run'),
            'project_scale': 'small', 'direct_model': 'deepseek', **route,
        })


def checkpoint(ctx: TaskContext, *, runtime_type: str = 'direct_action', version: str = '2') -> RuntimeCheckpoint:
    return RuntimeCheckpoint(id=uuid4(), runtime_type=runtime_type, runtime_version=version,
        run_id=ctx.run_id, tenant_id=ctx.tenant_id, mode=TaskMode.DIRECT,
        state={'phase': 'concrete_action', 'completed': False, 'approval_id': 'approval_write',
            'tool_name': 'workspace.write_text', 'context_sha256': 'a' * 64,
            'elapsed_seconds': 1, 'timeout_seconds': 60,
            'reference': {'version': 1, 'content_sha256': 'b' * 64, 'total_bytes': 1,
                          'segments': ({'id': str(uuid4()), 'sha256': 'c' * 64},)}},
    )


async def test_new_workspace_uses_action_stack_and_same_private_repository() -> None:
    dependencies = stack()
    child = await configured(dependencies)._runtime_for(task())
    assert type(child) is ActionApprovedDirectRuntime
    assert child._repository is dependencies.repository
    assert child._action_backend is dependencies.action
    assert child._action_gateway is dependencies.action_tools
    assert dependencies.transport.calls == []


@pytest.mark.parametrize('route', [
    {'permission_mode': 'full_access'},
    {'permission_mode': 'read_only'},
    {'execution_backend': 'docker'},
    {'sandbox_profile': 'read_only'},
    {'sandbox_profile': 'restricted'},
    {'requested_permissions': ('workspace.read', 'command.run')},
    {'requested_permissions': ('workspace.read', 'workspace.write')},
], ids=['full-access', 'read-only-mode', 'docker', 'read-only-sandbox',
        'restricted-sandbox', 'missing-write', 'missing-command'])
async def test_unsupported_fresh_scope_stays_on_legacy_stack(route: dict[str, JsonValue]) -> None:
    dependencies = stack()
    child = await configured(dependencies)._runtime_for(task(**route))
    assert type(child) is DirectRuntime
    assert child._capability_gateway is dependencies.legacy
    assert child._harness_tool_gateway is dependencies.legacy_tools
    assert dependencies.transport.calls == []


@pytest.mark.parametrize('mode', ['default', 'auto_review'])
async def test_supported_fresh_permission_modes_opt_into_action(mode: str) -> None:
    assert type(await configured(stack())._runtime_for(task(permission_mode=mode))) is ActionApprovedDirectRuntime


@pytest.mark.parametrize('route', [
    {'permission_mode': 'full_access'},
    {'permission_mode': 'read_only'},
    {'execution_backend': 'docker'},
    {'requested_permissions': ()},
])
async def test_unsupported_v2_scope_never_downgrades_to_legacy(route: dict[str, JsonValue]) -> None:
    dependencies = stack()
    ctx = task(**route)
    ctx = ctx.model_copy(update={'checkpoint': checkpoint(ctx)})
    assert type(await configured(dependencies)._runtime_for(ctx)) is ActionApprovedDirectRuntime
    assert dependencies.transport.calls == []


@pytest.mark.parametrize('route', [
    {'permission_mode': 'full_access'},
    {'permission_mode': 'read_only'},
    {'execution_backend': 'docker'},
])
def test_durable_authority_keeps_rejecting_unsupported_scopes(route: dict[str, JsonValue]) -> None:
    ctx = task(**route).model_copy(update={
        'actor_id': uuid4(), 'actor_role': Role.OPERATOR, 'execution_token': uuid4(),
    })
    with pytest.raises(RunConflict, match='project_verification_receipt_scope_changed'):
        VerificationAuthority.from_context(ctx, worker_id='worker-review', actor='main_agent')


@pytest.mark.parametrize('route', [
    {}, {'project_delivery': 'file', 'artifact_strategy': 'docx'},
    {'project_delivery': 'workspace'}, {'artifact_strategy': 'workspace_bundle'},
])
async def test_chat_documents_and_partial_workspace_markers_stay_legacy(route: dict[str, Any]) -> None:
    dependencies = stack()
    ctx = task().model_copy(update={'routing_decision': {'direct_model': 'deepseek', **route}})
    child = await configured(dependencies)._runtime_for(ctx)
    assert type(child) is DirectRuntime
    assert child._capability_gateway is dependencies.legacy
    assert child._harness_tool_gateway is dependencies.legacy_tools


@pytest.mark.parametrize('runtime_type,version', [
    ('direct', '1'), ('direct', '2'), ('engineering_admission', '1'),
    ('direct_action', '1'), ('direct_action', '3'), ('unknown_old', '1'),
])
@pytest.mark.parametrize('pending', [False, True])
async def test_legacy_checkpoint_never_switches_to_new_action_runtime(
    runtime_type: str, version: str, pending: bool,
) -> None:
    dependencies = stack()
    ctx = task()
    saved = checkpoint(ctx, runtime_type=runtime_type, version=version)
    factory = configured(dependencies)
    if pending:
        await factory.restore_checkpoint(saved)
    else:
        ctx = ctx.model_copy(update={'checkpoint': saved})
    assert type(await factory._runtime_for(ctx)) is DirectRuntime


@pytest.mark.parametrize('pending', [False, True])
async def test_v2_checkpoint_keeps_action_selection_when_scope_is_tightened(pending: bool) -> None:
    dependencies = stack()
    ctx = task()
    saved = checkpoint(ctx)
    narrowed = ctx.model_copy(update={'routing_decision': {'direct_model': 'deepseek',
        'sandbox_profile': 'read_only', 'requested_permissions': ()}})
    factory = configured(dependencies)
    if pending:
        await factory.restore_checkpoint(saved)
    else:
        narrowed = narrowed.model_copy(update={'checkpoint': saved})
    assert type(await factory._runtime_for(narrowed)) is ActionApprovedDirectRuntime


async def test_pending_v2_cannot_be_hidden_by_legacy_context_checkpoint() -> None:
    dependencies = stack()
    ctx = task()
    factory = configured(dependencies)
    await factory.restore_checkpoint(checkpoint(ctx))
    current = ctx.model_copy(update={'checkpoint': checkpoint(ctx, runtime_type='direct', version='1')})
    assert type(await factory._runtime_for(current)) is ActionApprovedDirectRuntime
    with pytest.raises(RuntimeExecutionError, match='checkpoint context changed'):
        _ = [event async for event in factory.run(current)]
    assert dependencies.transport.calls == []


@pytest.mark.parametrize('missing', ['artifact_repository', 'action_capability_gateway',
                                    'action_harness_tool_gateway'])
@pytest.mark.parametrize('pending', [False, True])
async def test_v2_checkpoint_without_dependencies_fails_without_legacy_reexecution(
    missing: str, pending: bool,
) -> None:
    dependencies = stack()
    ctx = task()
    saved = checkpoint(ctx)
    factory = configured(dependencies, **{missing: None})
    if pending:
        await factory.restore_checkpoint(saved)
    else:
        ctx = ctx.model_copy(update={'checkpoint': saved})
    child = await factory._runtime_for(ctx)
    assert isinstance(child, defaults.UnavailableRuntime)
    events = [event async for event in factory.run(ctx)]
    assert len(events) == 1 and events[0].kind is EventKind.RUNTIME_FAILED
    assert dependencies.transport.calls == []


async def test_unconfigured_action_stack_retains_original_fresh_workspace_factory() -> None:
    dependencies = stack()
    child = await configured(dependencies, artifact_repository=None,
        action_capability_gateway=None, action_harness_tool_gateway=None)._runtime_for(task())
    assert type(child) is DirectRuntime


async def test_real_legacy_completion_checkpoint_restores_with_action_dependencies() -> None:
    original_dependencies = stack()
    original = configured(original_dependencies)
    ctx = task().model_copy(update={'routing_decision': {'direct_model': 'deepseek'}})
    events = [event async for event in original.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    saved = next(event.checkpoint for event in events if event.checkpoint is not None)
    assert saved.runtime_type == 'direct' and saved.runtime_version == '1'
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    assert artifacts and original_dependencies.transport.calls

    cold_dependencies = stack()
    cold = configured(cold_dependencies)
    await cold.restore_checkpoint(saved)
    recovered = ctx.model_copy(update={'checkpoint': saved, 'artifacts': artifacts})
    resumed = [event async for event in cold.run(recovered)]
    assert [event.kind for event in resumed] == [EventKind.RUNTIME_COMPLETED]
    assert cold_dependencies.transport.calls == []


@pytest.mark.parametrize('first', [
    'agent_hub.runtime.action_direct',
    'agent_hub.capabilities.action_preparation',
])
def test_action_stack_imports_in_a_fresh_interpreter(first: str) -> None:
    source = Path(__file__).resolve().parents[3] / 'src'
    modules = (first, 'agent_hub.runs.service', 'agent_hub.capabilities.defaults',
               'agent_hub.runtime.defaults', 'agent_hub.app', 'agent_hub.runtime.worker')
    script = (f'import sys, importlib; sys.path.insert(0, {str(source)!r}); '
              f'[importlib.import_module(name) for name in {modules!r}]')
    result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                            text=True, timeout=45, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('route,model,attempts', [
    ({}, 'deepseek', 2),
    ({'harness_policy': {'fallback_policy': 'disabled'}}, 'deepseek', 1),
    ({'allowed_models': ('deepseek',)}, 'deepseek', 1),
    ({'direct_model': 'sonnet'}, 'sonnet', 1),
])
async def test_action_runtime_uses_existing_model_selection_and_attempt_policy(
    route: Mapping[str, JsonValue], model: str, attempts: int,
) -> None:
    child = await configured(stack())._runtime_for(task(**route))
    assert isinstance(child, ActionApprovedDirectRuntime)
    assert child._logical_model == model
    assert child._available_model_attempts == attempts


@pytest.mark.parametrize('pending', [False, True])
async def test_context_or_pending_v2_checkpoint_is_restored_on_real_action_runtime(
    monkeypatch: pytest.MonkeyPatch, pending: bool,
) -> None:
    dependencies = stack()
    ctx = task()
    saved = checkpoint(ctx)
    factory = configured(dependencies)
    seen: list[TaskContext] = []

    async def observe(self: ActionApprovedDirectRuntime, current: TaskContext) -> AsyncIterator[RunEvent]:
        assert self._action_restored is not None and self._action_restored.id == saved.id
        seen.append(current)
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=current.run_id)

    def no_outer_gate(*args: Any, **kwargs: Any) -> ExecutionRuntime:
        raise AssertionError('a real action runtime must use its concrete action policy gate')

    monkeypatch.setattr(ActionApprovedDirectRuntime, 'run', observe)
    monkeypatch.setattr(defaults, 'with_engineering_admission', no_outer_gate)
    if pending:
        await factory.restore_checkpoint(saved)
    else:
        ctx = ctx.model_copy(update={'checkpoint': saved})
    events = [event async for event in factory.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(seen) == 1 and seen[0].checkpoint is not None and seen[0].checkpoint.id == saved.id
    assert not factory._active and not factory._pending_checkpoints
    assert dependencies.transport.calls == []


async def test_legacy_runtime_still_crosses_engineering_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    dependencies = stack()
    factory = configured(dependencies)
    seen: list[tuple[ExecutionRuntime, object]] = []

    def admission(child: ExecutionRuntime, authorizer: object, current: TaskContext) -> ExecutionRuntime:
        seen.append((child, authorizer))
        return child

    async def completed(self: DirectRuntime, current: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=current.run_id)

    monkeypatch.setattr(defaults, 'with_engineering_admission', admission)
    monkeypatch.setattr(DirectRuntime, 'run', completed)
    ctx = task().model_copy(update={'routing_decision': {'direct_model': 'deepseek'}})
    _ = [event async for event in factory.run(ctx)]
    assert len(seen) == 1 and type(seen[0][0]) is DirectRuntime
    assert seen[0][1] is dependencies.legacy_tools


async def test_malformed_v2_checkpoint_is_rejected_without_legacy_fallback() -> None:
    dependencies = stack()
    ctx = task()
    malformed = RuntimeCheckpoint(id=uuid4(), runtime_type='direct_action', runtime_version='2',
        run_id=ctx.run_id, tenant_id=ctx.tenant_id, mode=ctx.mode, state={'completed': False})
    factory = configured(dependencies)
    await factory.restore_checkpoint(malformed)
    with pytest.raises(RuntimeExecutionError, match='checkpoint is incompatible'):
        _ = [event async for event in factory.run(ctx.model_copy(update={'checkpoint': malformed}))]
    assert dependencies.transport.calls == []


def test_registry_injects_action_stack_only_into_direct() -> None:
    dependencies = stack()
    registry = defaults.configured_runtime_registry(
        config_service=cast(ConfigService, FakeConfigService(_document())),
        secret_service=cast(SecretService, FakeSecretService()), redis_client=object(),
        transport=dependencies.transport, capability_gateway=dependencies.legacy,
        harness_tool_gateway=dependencies.legacy_tools, artifact_repository=dependencies.repository,
        action_capability_gateway=dependencies.action,
        action_harness_tool_gateway=dependencies.action_tools,
    )
    direct = registry.get(TaskMode.DIRECT)
    assert isinstance(direct, defaults.ConfigBackedDirectRuntime)
    assert direct._action_capability_gateway is dependencies.action
    assert direct._action_harness_tool_gateway is dependencies.action_tools
    assert direct._artifact_repository is dependencies.repository
    for mode in (TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID):
        child = registry.get(mode)
        assert isinstance(child, (defaults.ConfigBackedDispatchRuntime,
                                  defaults.ConfigBackedDiscussionRuntime,
                                  defaults.ConfigBackedHybridRuntime))
        assert not hasattr(child, '_action_capability_gateway')
        assert not hasattr(child, '_action_harness_tool_gateway')
        assert child._capability_gateway is dependencies.legacy
        assert child._harness_tool_gateway is dependencies.legacy_tools
