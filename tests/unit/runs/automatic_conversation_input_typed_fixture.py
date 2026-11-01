"""Typed IO fixtures for automatic selection and metadata-only app wiring."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Never, TypedDict
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import Role
from agent_hub.capabilities.defaults import RuntimeCapabilityStack
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.project_inputs import OriginalProjectPreparer
from agent_hub.runs.project_verification_receipts import ProjectVerificationReceiptRepository
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.workspace import PermissionMode
from agent_hub.runtime import worker
from agent_hub.runtime.artifacts import ArtifactRepository
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.runtime.review_assembly import assemble_review_capability_stack
from agent_hub.settings import Settings
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities.test_project_baselines import source_tree
from tests.unit.runs.conversation_authenticated_input_typed_fixture import (
    LockSession,
    json_map,
    mutable,
    present,
)
from tests.unit.runs.project_auto_repair_typed_fixture import RepairRepository
from tests.unit.runs.test_conversation_mode import ConversationModeRepository
from tests.unit.runs.test_original_project_preparation import Observer
from tests.unit.test_app_wiring import FakeRedis, StubConfigService, valid_settings


class SubmissionOptions(TypedDict, total=False):
    project_id: str | None
    workspace_session_id: str | None
    conversation_id: str | None
    sandbox_profile: str | None
    execution_backend: str
    permission_mode: PermissionMode | None
    requested_permissions: tuple[str, ...]
    skip_evolution_proposal: bool
    allow_scale_mode_upgrade: bool
    replace_workspace_files: bool
    project_input: ManagedProjectInput | None
    conversation_artifact_repository: ArtifactRepository | None
    submission_actor_role: Role | None


class SubmissionRepository(RunRepository):
    def __init__(self, previous_mode: TaskMode | None) -> None:
        super().__init__(async_sessionmaker())
        self.memory = ConversationModeRepository(previous_mode)

    @property
    def created(self) -> list[dict[str, object]]:
        return self.memory.created

    async def latest_resolved_mode_for_conversation(self, *, tenant_id: UUID, actor_id: UUID,
                                                   conversation_id: str) -> TaskMode | None:
        return await self.memory.latest_resolved_mode_for_conversation(
            tenant_id=tenant_id, actor_id=actor_id, conversation_id=conversation_id)

    async def create_run(self, **kwargs: object) -> RunRecord:
        # The production service is the only caller; validate its concrete DTOs.
        from agent_hub.domain.runs import RunStatus, TaskMode
        tenant, actor = kwargs['tenant_id'], kwargs['actor_id']
        request, mode, status = kwargs['request'], kwargs['mode'], kwargs['status']
        role, key = kwargs.get('actor_role'), kwargs['idempotency_key']
        route, enqueue = kwargs.get('routing_decision'), kwargs['enqueue']
        assert isinstance(tenant, UUID) and isinstance(actor, UUID) and isinstance(request, str)
        assert mode is None or isinstance(mode, TaskMode)
        assert isinstance(status, RunStatus) and (role is None or isinstance(role, Role))
        assert key is None or isinstance(key, str)
        assert isinstance(enqueue, bool)
        assert 'authenticated_input' not in kwargs
        return await self.memory.create_run(tenant_id=tenant, actor_id=actor, request=request,
            mode=mode, status=status, idempotency_key=key, actor_role=role,
            routing_decision=None if route is None else mutable(route), enqueue=enqueue)

    async def get(self, tenant_id: UUID, run_id: UUID, *, actor_id: UUID | None = None) -> RunRecord:
        return await self.memory.get(tenant_id, run_id)


class NoAuthorization(HarnessToolGateway):
    def __init__(self) -> None:
        pass

    def __getattribute__(self, name: str) -> Never:
        raise AssertionError('selection must not authorize or execute: ' + name)


class SelectionRepository(RepairRepository):
    async def run_transaction(self) -> AsyncSession:
        import asyncio
        return LockSession(asyncio.Lock())


class RecordingAuthorizer(HarnessToolGateway):
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def authorize(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                        user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        self.calls.append('authorize')
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                     status='succeeded', payload={'authorized': True})


class RecordingVerifier(ProjectVerificationService):
    def __init__(self, observer: Observer, root: Path, repository: RunRepository) -> None:
        super().__init__(root=root, repository=repository, executor=None)
        self.observer = observer

    async def observe_original(self, **kwargs: object) -> Mapping[str, JsonValue]:
        return await self.observer.observe_original(**kwargs)


async def preparation_fixture(tmp_path: Path) -> tuple[
    OriginalProjectPreparer, RepairRepository, TaskContext, Observer, list[str], Path, ProjectInvocation,
]:
    from agent_hub.domain.runs import TaskMode
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    repository = SelectionRepository(routing_decision={
        'project_id': invocation.project_id, 'workspace_session_id': invocation.session_id,
        'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd',
        'requested_permissions': ['workspace.read', 'workspace.write', 'command.run'],
        'project_input_contract_version': 1,
        'project_input': {'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['src']},
    })
    repository.run_id = repository.row.id = invocation.run_id
    repository.row.tenant_id = invocation.tenant_id
    repository.row.actor_role = Role.OPERATOR.value
    token = uuid4()
    async with await repository.run_transaction() as session:
        await repository.claim_for_execution(session, invocation.run_id, allow_running_recovery=False,
            worker_id='test-worker', worker_lease_token=token,
            worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5))
    context = TaskContext(run_id=invocation.run_id, tenant_id=invocation.tenant_id,
        actor_id=repository.row.actor_id, actor_role=Role.OPERATOR, execution_token=token,
        mode=TaskMode.DISPATCH, request=repository.row.request,
        routing_decision=json_map(repository.row.routing_decision))
    calls: list[str] = []
    observer = Observer(calls, root, invocation)
    preparer = OriginalProjectPreparer(root=root, repository=repository,
        authorizer=RecordingAuthorizer(calls), verifier=RecordingVerifier(observer, root, repository))
    return preparer, repository, context, observer, calls, root, invocation


class RuntimeAdapter:
    async def reload(self, tenant_id: UUID | None = None) -> None:
        pass

    async def start(self) -> None:
        pass

    def capability_manifest_source(self) -> RuntimeAdapter:
        return self

    def manifests_for_tenant(self, tenant_id: UUID) -> dict[str, object]:
        return {'schema_version': 1, 'capabilities': ()}


class MetadataDatabase:
    def __init__(self) -> None:
        # A genuine factory identity, with no engine or connection. Any SQL use fails.
        self.session_factory = async_sessionmaker(expire_on_commit=False)

    async def dispose(self) -> None:
        pass


class Captured(TypedDict):
    stacks: list[tuple[dict[str, object], RuntimeCapabilityStack]]
    artifacts: list[PostgresArtifactRepository]
    registry: dict[str, object]


def settings(tmp_path: Path) -> Settings:
    return valid_settings(tmp_path / 'attachments', skill_store_dir=tmp_path / 'skills',
        generated_artifact_dir=tmp_path / 'generated', project_workspace_dir=tmp_path / 'workspaces',
        capability_environment_root_dir=tmp_path / 'environments', plugin_package_store_dir=tmp_path / 'plugins',
        plugin_package_dependency_cache_dir=tmp_path / 'dependency-cache')


def record_call[**P, R](function: Callable[P, R],
                       sink: list[tuple[dict[str, object], R]]) -> Callable[P, R]:
    def call(*args: P.args, **kwargs: P.kwargs) -> R:
        result = function(*args, **kwargs)
        sink.append((dict(kwargs), result))
        return result
    return call


def capture_builds(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> Captured:
    captured: Captured = {'stacks': [], 'artifacts': [], 'registry': {}}
    def artifacts(factory: async_sessionmaker[AsyncSession]) -> PostgresArtifactRepository:
        repository = PostgresArtifactRepository(factory)
        captured['artifacts'].append(repository)
        return repository
    def registry(**kwargs: object) -> RuntimeRegistry:
        captured['registry'] = kwargs
        return RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),))
    # Observe the stack actually returned to app/worker. The assembly function
    # replaces its inner stack's harness with the real profiled dispatcher;
    # capturing only the nested raw builder produces a different object.
    monkeypatch.setattr(module, 'assemble_review_capability_stack', record_call(assemble_review_capability_stack, captured['stacks']))
    monkeypatch.setattr(module, 'PostgresArtifactRepository', artifacts)
    monkeypatch.setattr(module, 'configured_runtime_registry', registry)
    return captured


def isolated_stacks(captured: Captured, settings: Settings,
                    factory: async_sessionmaker[AsyncSession], *,
                    legacy_artifacts: bool = False) -> tuple[RuntimeCapabilityStack, RuntimeCapabilityStack]:
    assert len(captured['stacks']) == 2
    registry = captured['registry']
    legacy_options, legacy = next((options, stack) for options, stack in captured['stacks']
        if stack.runtime_gateway is registry['capability_gateway'])
    action_options, action = next((options, stack) for options, stack in captured['stacks']
        if stack.runtime_gateway is registry['action_capability_gateway'])
    assert legacy is not action
    assert legacy.policy_gateway is not action.policy_gateway
    assert registry['harness_tool_gateway'] is legacy.harness_tool_gateway
    assert registry['action_harness_tool_gateway'] is action.harness_tool_gateway
    assert legacy_options.get('receipt_repository') is None
    assert present(legacy.runtime_gateway._project_verification)._durable is None
    assert len(captured['artifacts']) == 1
    artifacts = captured['artifacts'][0]
    if legacy_artifacts:
        assert legacy_options['artifact_repository'] is artifacts
        assert legacy.runtime_gateway._artifact_repository is artifacts
    else:
        assert legacy_options.get('artifact_repository') is None
        assert legacy.runtime_gateway._artifact_repository is None
    assert registry['artifact_repository'] is artifacts
    assert action_options['artifact_repository'] is artifacts
    assert artifacts._session_factory is factory
    receipts = action_options['receipt_repository']
    assert isinstance(receipts, ProjectVerificationReceiptRepository)
    assert receipts._session_factory is factory
    durable = present(present(action.runtime_gateway._project_verification)._durable)
    assert durable.receipts is receipts
    assert durable.artifacts is artifacts
    for name in ('run_repository', 'require_approval_for_tools', 'tool_approval_mode',
                 'tool_registry', 'mcp_backend', 'plugin_backend'):
        assert action_options[name] is legacy_options[name]
    for stack in (legacy, action):
        assert stack.harness_tool_gateway._backend is stack.runtime_gateway
        assert stack.harness_tool_gateway._policy_gateway is stack.policy_gateway
        assert stack.harness_tool_gateway._mcp_backend is legacy_options['mcp_backend']
        assert stack.harness_tool_gateway._plugin_backend is legacy_options['plugin_backend']
        assert stack.runtime_gateway._project_workspace_dir == settings.project_workspace_dir
        assert stack.runtime_gateway._run_repository is legacy_options['run_repository']
        assert stack.runtime_gateway._tenant_id == settings.bootstrap_tenant_id
        assert present(stack.project_cache_cleanup_hook).verifier is stack.runtime_gateway._project_verification
    return legacy, action


def build_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[worker.WorkerResources, Captured, Settings, MetadataDatabase]:
    captured = capture_builds(monkeypatch, worker)
    database, config = MetadataDatabase(), settings(tmp_path)
    monkeypatch.setattr(worker, 'build_database', lambda url: database)
    monkeypatch.setattr(Redis, 'from_url', lambda url: FakeRedis())
    monkeypatch.setattr(worker, 'ConfigService', lambda factory: StubConfigService())
    monkeypatch.setattr(worker, 'RuntimeMcpService', lambda **kwargs: RuntimeAdapter())
    monkeypatch.setattr(worker, 'RuntimePluginService', lambda **kwargs: RuntimeAdapter())
    evolution_hook = object()
    monkeypatch.setattr(worker, '_evolution_terminal_hooks', lambda **kwargs: (evolution_hook,))
    resources = worker.build_worker_service(config)
    return resources, captured, config, database
