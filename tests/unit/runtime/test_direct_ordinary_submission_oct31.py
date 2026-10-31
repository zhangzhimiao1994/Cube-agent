"""Ordinary API submission regression; no prefilled engineering route or provider IO."""
from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest

from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.conversations import ConversationRecord
from agent_hub.runs.service import RunService
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.api.test_runs_api import StubAuthService, bearer
from tests.unit.runs.test_conversation_mode import (
    ConversationMetadataRepository,
    ConversationModeRepository,
)
from tests.unit.runs.test_harness_decision import RecordingQueue

MESSAGE = '我需要你开发一个 Python 命令行项目，读取本地 CSV 并汇总金额；请实际编写代码和测试。'


class SubmissionRepository(ConversationModeRepository):
    """In-memory persistence boundary; keep the real API's original input/actor."""

    def __init__(self) -> None:
        super().__init__(TaskMode.DIRECT)
        self.evidence: Any = None

    async def create_run(self, **values: Any) -> Any:
        self.evidence = values.pop('authenticated_input', None)
        record = await super().create_run(**values)
        self.record = replace(record, actor_role=values['actor_role'])
        return self.record


class SubmissionOnlyPreparer:
    async def __call__(self, context: Any) -> Any:
        raise AssertionError('API submission must not execute project preparation')

    async def select_existing(self, context: Any) -> Any:
        raise AssertionError('API submission must not inspect the workspace')


async def ordinary_submission(message: str, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    import agent_hub.runs.service as service_module
    from agent_hub.api.routers import runs as run_api

    def installed_backend(backend: str, sandbox: str) -> None:
        assert (backend, sandbox) == ('systemd', 'workspace_write')

    # Only the host-discovery seam; no Root execution or capability authority.
    monkeypatch.setattr(run_api, 'execution_backend_unavailable_reason', installed_backend)
    expected = Path(__file__).resolve().parents[3] / 'src/agent_hub/runs/service.py'
    assert Path(service_module.__file__).resolve() == expected.resolve()
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    now = datetime.now(UTC)
    conversation = ConversationRecord(
        id=uuid4(), tenant_id=principal.tenant_id, conversation_id='existing-empty',
        title='Empty project', project_id='csv-project', project_label='CSV project',
        workspace_path='csv-session', archived_at=None, created_at=now, updated_at=now,
    )
    conversations = ConversationMetadataRepository(conversation)
    repository = SubmissionRepository()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None, task_queue=RecordingQueue(), project_input_preparer=SubmissionOnlyPreparer(),
        conversation_repository=conversations,
    )
    application = create_app(
        auth_service=StubAuthService(principal), rate_limiter=object(),
        config_service=object(), run_service=service,
    )
    body = {
        'message': message, 'mode': 'direct', 'conversation_id': conversation.conversation_id,
        'project_id': conversation.project_id, 'workspace_session_id': conversation.workspace_path,
        'sandbox_profile': 'workspace_write', 'permission_mode': 'default',
        'execution_backend': 'systemd',
        'requested_permissions': ['workspace.read', 'workspace.write', 'command.run', 'network.read'],
        'direct_model': 'deepseek', 'allowed_models': ['deepseek'],
        'skip_evolution_proposal': True, 'allow_scale_mode_upgrade': False,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v1/runs', headers=bearer(), json=body)
    assert response.status_code == 202, response.text
    assert conversations.lookups == [(principal.tenant_id, conversation.conversation_id)]
    assert repository.record is not None and repository.evidence is not None
    assert repository.evidence.message == message
    assert repository.record.actor_id == principal.user_id
    assert repository.record.actor_role is principal.role
    return repository, principal, body


async def test_matching_empty_conversation_keeps_ordinary_engineering_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, _ = await ordinary_submission(MESSAGE, monkeypatch)
    route = repository.record.routing_decision
    assert route['generated_project_contract_version'] == 1
    assert 'automatic_project_input_version' not in route
    assert 'project_input' not in route
    assert route.get('project_delivery') == 'workspace', route
    assert route.get('artifact_strategy') == 'workspace_bundle'
    assert route.get('project_scale') in {'small', 'medium', 'large', 'ultra'}


@pytest.mark.parametrize('message', [
    '请只规划构建一个 Python CLI 项目，不要执行',
    '"构建一个 Python CLI 项目"',
    '> 构建一个 Python CLI 项目',
])
async def test_planning_and_quoted_ordinary_requests_do_not_acquire_delivery(
    message: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, _ = await ordinary_submission(message, monkeypatch)
    route = repository.record.routing_decision
    assert 'automatic_project_input_version' not in route
    assert 'automatic_project_input_selection' not in route
    assert 'project_delivery' not in route, route
    assert 'artifact_strategy' not in route
    assert 'project_scale' not in route


async def test_ordinary_delivery_reaches_real_factory_request_and_workspace_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.capabilities.defaults import build_runtime_capability_stack
    from agent_hub.files.workspace import ProjectWorkspaceStore
    from agent_hub.models.litellm_client import ModelTransportError
    from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    from agent_hub.runtime.contracts import EventKind, TaskContext
    from agent_hub.runtime.defaults import ConfigBackedDirectRuntime
    from agent_hub.runtime.direct import RuntimeExecutionError
    from agent_hub.skills.sandbox import python_project_profile
    from tests.unit.runtime.test_action_admission_helper import configured_document
    from tests.unit.runtime.test_configured_runtime import FakeSecretService
    from tests.unit.runtime.test_plain_file_action_helper import LocalCapacityBoundary

    repository, principal, _ = await ordinary_submission(MESSAGE, monkeypatch)
    # Same in-memory persistence boundary as the existing executable repository
    # fixtures: claim changes lifecycle/lease only, never route or permission/grant.
    token = uuid4()
    repository.record = replace(repository.record, status=RunStatus.RUNNING,
        worker_id='ordinary-unit-worker', worker_lease_token=token,
        worker_lease_expires_at=datetime.now(UTC) + timedelta(seconds=30))
    record = repository.record
    route_before = dict(record.routing_decision)
    workspace = tmp_path / 'workspace'
    store = ProjectWorkspaceStore(workspace)
    assert store.list_files(principal.tenant_id, 'csv-project', 'csv-session') == ()

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('ordinary factory unit test attempted live network')

    # The pytest loop already exists. Preserve its local socketpair, deny live IO.
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket.socket, 'connect_ex', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    monkeypatch.setattr(socket, 'getaddrinfo', no_network)
    # Honest platform seam: run the real project.inspect, reporting profile unavailable;
    # this unit test does not contact a Root broker or fabricate interpreter/wheel pins.
    monkeypatch.setattr(python_project_profile, '_systemd_supported', lambda: False)

    async def approval_mode(tenant: Any) -> str:
        assert tenant == principal.tenant_id
        return 'ask'

    artifacts = InMemoryArtifactRepository()
    stack = build_runtime_capability_stack(
        tenant_id=principal.tenant_id, run_repository=repository,
        skill_store_dir=tmp_path / 'skills', workspace_root=tmp_path,
        project_workspace_dir=workspace, generated_artifact_dir=tmp_path / 'artifacts',
        artifact_repository=artifacts, tool_approval_mode=approval_mode,
    )
    calls: list[str] = []
    original_execute = stack.runtime_gateway.execute

    async def observed_execute(**values: Any) -> Any:
        calls.append(values['name'])
        return await original_execute(**values)

    monkeypatch.setattr(stack.runtime_gateway, 'execute', observed_execute)
    harness_results: list[Any] = []
    original_invoke = stack.harness_tool_gateway.invoke

    async def observed_invoke(*args: Any, **values: Any) -> Any:
        result = await original_invoke(*args, **values)
        harness_results.append((result.tool_name, result.status, result.failure_reason))
        return result

    monkeypatch.setattr(stack.harness_tool_gateway, 'invoke', observed_invoke)
    document = configured_document()
    definition = document['models'].pop('main')
    definition['deployments'][0].update(provider='deepseek', model='deepseek-chat')
    document['models']['deepseek'] = definition
    for agent in document['agents']:
        agent['model'] = 'deepseek'

    class Configs:
        async def get_current(self, tenant: Any) -> Any:
            assert tenant == principal.tenant_id
            return SimpleNamespace(version=1, document=document)

    content = 'def total(values):\n    return sum(values)\n'
    requests: list[ModelRequest] = []

    class Transport:
        async def complete(self, deployment: Any, request: ModelRequest, api_key: str) -> ModelResponse:
            requests.append(request)
            assert request.logical_model == 'deepseek'
            prompt = '\n'.join(str(message.content) for message in request.messages)
            # Assert advertisement BEFORE supplying any model batch, not a canned tool return.
            assert 'PROJECT_DELIVERY_CONTRACT' in prompt
            assert 'DIRECT_OUTPUT_CONTRACT' in prompt
            assert 'UNTRUSTED_PROJECT_INSPECTION_JSON' in prompt
            assert 'workspace_batch' in prompt and not request.tools
            if len(requests) > 1:
                raise ModelTransportError('intentional offline stop after first real write', status_code=400)
            return ModelResponse(text=json.dumps({'workspace_batch': {
                'files': {'src/report.py': content}, 'complete': False,
                'continuation': 'Add real project tests before verification.',
            }, 'summary': 'Implementation batch; not verified or delivered.'}),
                usage=TokenUsage(prompt_tokens=2000, completion_tokens=200, total_tokens=2200))

    capacities: list[Any] = []

    async def capacity(tenant: Any, deployments: Any) -> Any:
        assert tenant == principal.tenant_id
        value = LocalCapacityBoundary()
        capacities.append(value)
        return value

    runtime = ConfigBackedDirectRuntime(
        config_service=Configs(), secret_service=FakeSecretService(),
        capacity_factory=capacity, transport=Transport(),
        capability_gateway=stack.runtime_gateway, harness_tool_gateway=stack.harness_tool_gateway,
        action_capability_gateway=stack.runtime_gateway,
        action_harness_tool_gateway=stack.harness_tool_gateway, artifact_repository=artifacts,
    )
    # Exact output of the authenticated API/service; no engineering keys added here.
    context = TaskContext(tenant_id=record.tenant_id, run_id=record.id, actor_id=record.actor_id,
        actor_role=record.actor_role, execution_token=token, mode=TaskMode.DIRECT,
        request=record.request, routing_decision=record.routing_decision,
        token_budget=50_000, timeout_seconds=30)
    events = []
    with pytest.raises(RuntimeExecutionError) as stopped:
        async with asyncio.timeout(10):
            async for event in runtime.run(context):
                events.append(event)
    assert len(requests) == 2, (calls, harness_results, str(stopped.value), [(event.kind, event.reason) for event in events])
    assert calls == ['project.inspect', 'workspace.write_text']
    files = store.list_files(principal.tenant_id, 'csv-project', 'csv-session')
    assert [file.path for file in files] == ['src/report.py']
    actual = workspace / str(principal.tenant_id) / 'projects/csv-project/sessions/csv-session/src/report.py'
    assert actual.read_text(encoding='utf-8') == content
    assert repository.record.routing_decision == route_before
    assert all(value.acquired == value.released for value in capacities)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
