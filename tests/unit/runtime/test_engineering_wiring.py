from __future__ import annotations

from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import defaults
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.defaults import ConfigBackedDirectRuntime, _role_allowed_tools
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose
from tests.unit.runtime.test_configured_runtime import (
    TENANT_ID,
    FakeCapabilityAvailability,
    FakeConfigService,
    FakeSecretService,
    FakeTransport,
    _immediate_capacity,
)


@pytest.mark.parametrize('tools', [('workspace.read',), ('run_safe_command',)])
def test_engineering_roles_expose_real_verifier_not_unimplemented_command(
    tools: tuple[str, ...],
) -> None:
    role = RoleAssignment(
        id='engineer', role='Engineer', purpose=RolePurpose.EXECUTE,
        mission='Implement project.', must_answer=('What passed?',), allowed_tools=tools,
        forbidden_actions=('Do not escape scope.',), skills=(),
        output_schema={'summary': 'string'}, model='main',
    )
    result = _role_allowed_tools(role, TaskContext(
        run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH, request='Implement project.',
    ), capability_gateway=FakeCapabilityAvailability({'project.verify', 'workspace.read'}))
    assert 'project.verify' in result
    assert 'run_safe_command' not in result


async def test_config_backed_direct_injects_common_harness_policy_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def direct(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(defaults, 'DirectRuntime', direct)
    harness = object()
    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService({'models': {'main': {'deployments': [{
            'provider': 'deepseek', 'model': 'deepseek-v4-flash',
            'api_base': 'https://api.deepseek.com/v1', 'credential_ref': 'secret://main',
            'quota_scope_id': 'account', 'max_concurrency': 2, 'target_utilization': 0.8,
            'reserved_slots': 0, 'capabilities': ['text', 'tool_calling', 'structured_output'],
        }]}}, 'agents': []}),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(tenant_id, deployments),
        transport=FakeTransport(), harness_tool_gateway=harness,  # type: ignore[arg-type]
    )
    await runtime._runtime_for(TaskContext(
        run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DIRECT, request='Build a project.',
    ))
    assert captured['harness_tool_gateway'] is harness


def test_discussion_engineering_owner_survives_role_limit_and_uses_common_tools() -> None:
    context = TaskContext(run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISCUSS,
        request='Build a Node project.', routing_decision={
            'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle',
            'project_scale': 'small',
        })
    readers = tuple(RoleAssignment(
        id=f'reader_{number}', role='Analyst', purpose=RolePurpose.EXPERTISE,
        mission='Review requirements.', must_answer=('What is needed?',), allowed_tools=(),
        forbidden_actions=('Do not escape scope.',), skills=(), output_schema={}, model='main',
    ) for number in range(8))
    roles = defaults._discussion_engineering_roles(readers, context, 'main')
    available = FakeCapabilityAvailability({
        'workspace.read', 'workspace.write_text', 'workspace.list', 'workspace.bundle',
        'project.inspect', 'project.verify',
    })
    plan = defaults._discussion_plan(roles, 'main', context, capability_gateway=available)
    owners = [role for role in plan.participants if 'workspace.write_text' in role.allowed_tools]
    assert len(owners) == 1
    assert {'project.verify', 'workspace.bundle'} <= set(owners[0].allowed_tools)
    assert defaults._discussion_engineering_roles(readers, TaskContext(
        run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISCUSS, request='Discuss this idea.',
    ), 'main') == readers
