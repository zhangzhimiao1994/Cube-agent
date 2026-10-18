from __future__ import annotations

from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import defaults
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.crew.adapter import _is_incremental_workspace_contract_step
from agent_hub.runtime.crew.plan import DispatchPlan, DispatchStep
from agent_hub.runtime.defaults import ConfigBackedDirectRuntime, _role_allowed_tools
from agent_hub.runtime.role_planner import (
    RoleAssignment,
    RolePlanner,
    RolePlanningRequest,
    RolePurpose,
    TaskProfile,
)
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


@pytest.mark.parametrize('purpose', [RolePurpose.PLAN, RolePurpose.EXPERTISE, RolePurpose.RISK_REVIEW])
def test_read_only_roles_do_not_gain_execution_from_workspace_reads(purpose: RolePurpose) -> None:
    role = RoleAssignment(id='reader', role='Reader', purpose=purpose,
        mission='Inspect requirements.', must_answer=('What is required?',), allowed_tools=('workspace.read',),
        forbidden_actions=('Do not modify files.',), skills=(), output_schema={}, model='main')
    tools = _role_allowed_tools(role, TaskContext(run_id=uuid4(), tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH, request='Create a small Node.js billing module.',
        routing_decision={'engineering_delivery_contract_version':1}),
        capability_gateway=FakeCapabilityAvailability({'workspace.read','project.inspect','project.verify'}))
    assert 'workspace.read' in tools and 'project.inspect' in tools
    assert 'project.verify' not in tools


def test_verified_workspace_contract_does_not_require_acceptance_prompt_markers() -> None:
    step = DispatchStep(id='implement', agent='engineer', task='Create a small Node.js billing module.',
        engineering_delivery_contract_version=1,
        tools=('workspace.write_text','workspace.list','workspace.bundle','project.inspect','project.verify'))
    assert _is_incremental_workspace_contract_step(step)
    legacy = step.model_copy(update={'engineering_delivery_contract_version':0})
    assert not _is_incremental_workspace_contract_step(legacy)
    assert 'engineering_delivery_contract_version' not in legacy.model_dump(mode='json')
    assert step.model_dump(mode='json')['engineering_delivery_contract_version'] == 1


def test_generated_engineering_plan_persists_new_contract_without_reclassifying_legacy() -> None:
    role = RoleAssignment(id='builder', role='Builder', purpose=RolePurpose.EXECUTE,
        mission='Build the module.', must_answer=('What works?',),
        allowed_tools=('workspace.write_text','workspace.list','workspace.read','workspace.bundle'),
        forbidden_actions=('Do not escape scope.',), skills=(), output_schema={'summary':'string'}, model='main')
    context = TaskContext(run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH,
        request='Create a small Node.js billing module.')
    gateway = FakeCapabilityAvailability({'workspace.read','workspace.write_text','workspace.list',
        'workspace.bundle','project.inspect','project.verify'})
    legacy = defaults._dispatch_plan((role,), context, capability_gateway=gateway)
    current = defaults._dispatch_plan((role,), context.model_copy(update={'routing_decision':{
        'engineering_delivery_contract_version':1}}), capability_gateway=gateway)
    assert legacy.steps[0].engineering_delivery_contract_version == 0
    assert not _is_incremental_workspace_contract_step(legacy.steps[0])
    assert current.steps[0].engineering_delivery_contract_version == 1
    assert _is_incremental_workspace_contract_step(current.steps[0])
    assert legacy.digest != current.digest
    assert DispatchPlan.from_payload(legacy.to_payload()).digest == legacy.digest
    assert DispatchPlan.from_payload(current.to_payload()).digest == current.digest


@pytest.mark.parametrize('version', [0, 1])
@pytest.mark.parametrize('mode', [TaskMode.DISPATCH, TaskMode.HYBRID])
def test_mixed_engineering_installation_waits_for_source_without_dependency_cycle(
    version: int, mode: TaskMode,
) -> None:
    roles = RolePlanner().plan(RolePlanningRequest(
        task='Build a Node.js module and install its dependencies.', mode=mode,
        profile=TaskProfile.DEPLOYMENT, profiles=(TaskProfile.DEPLOYMENT, TaskProfile.SOFTWARE),
        default_model='main',
    )).roles
    context = TaskContext(run_id=uuid4(), tenant_id=TENANT_ID, mode=mode,
        request='Build a Node.js module and install its dependencies.',
        routing_decision={'engineering_delivery_contract_version': version})
    gateway = FakeCapabilityAvailability({'read_context', 'workspace.read', 'workspace.write_text',
        'workspace.list', 'workspace.bundle', 'project.inspect', 'project.verify'})
    plan = defaults._dispatch_plan(roles, context, capability_gateway=gateway)
    steps = {step.id: step for step in plan.steps}
    assert 'project.verify' in steps['installer_step'].tools
    if version == 1:
        assert 'implementer_step' in steps['installer_step'].depends_on
        assert 'installer_step' not in steps['implementer_step'].depends_on
        assert steps['implementer_step'].engineering_delivery_contract_version == 1
    else:
        assert 'installer_step' in steps['implementer_step'].depends_on
        assert 'implementer_step' not in steps['installer_step'].depends_on


@pytest.mark.parametrize('missing', ['workspace.write_text', 'workspace.bundle', 'project.verify'])
def test_engineering_writer_cannot_silently_downgrade_when_capability_is_missing(missing: str) -> None:
    tools = {'workspace.write_text', 'workspace.read', 'workspace.bundle', 'project.verify'}
    role = RoleAssignment(id='builder', role='Builder', purpose=RolePurpose.EXECUTE,
        mission='Build the module.', must_answer=('What works?',), allowed_tools=tuple(sorted(tools)),
        forbidden_actions=('Do not escape scope.',), skills=(), output_schema={'summary': 'string'}, model='main')
    context = TaskContext(run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH,
        request='Build a Node.js module.', routing_decision={'engineering_delivery_contract_version': 1})
    with pytest.raises(ValueError, match='engineering delivery requires'):
        defaults._dispatch_plan((role,), context,
            capability_gateway=FakeCapabilityAvailability(tools - {missing}))


def test_writable_verifier_does_not_leave_installer_outside_both_dependency_sets() -> None:
    def role(identifier: str, purpose: RolePurpose, tools: tuple[str, ...]) -> RoleAssignment:
        return RoleAssignment(id=identifier, role=identifier, purpose=purpose, mission='Check project.',
            must_answer=('What passed?',), allowed_tools=tools, forbidden_actions=('Do not escape scope.',), skills=(),
            output_schema={'summary': 'string'}, model='main')

    roles = (role('installer', RolePurpose.EXECUTE, ('run_safe_command',)),
        role('tester', RolePurpose.VERIFY, ('workspace.write_text', 'workspace.bundle', 'workspace.read')))
    context = TaskContext(run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH,
        request='Verify the project.', routing_decision={'engineering_delivery_contract_version': 1})
    available = FakeCapabilityAvailability({'workspace.write_text', 'workspace.bundle',
        'workspace.read', 'project.verify', 'project.inspect'})
    plan = defaults._dispatch_plan(roles, context, capability_gateway=available)
    steps = {s.id: s for s in plan.steps}
    assert steps['installer_step'].depends_on == ()
    assert steps['tester_step'].depends_on == ('installer_step',)


def test_new_action_contract_is_versioned_and_requires_inspection() -> None:
    tools = ('workspace.write_text', 'project.verify', 'workspace.bundle')
    legacy = DispatchStep(id='build', agent='builder', task='Build.', tools=tools,
        engineering_delivery_contract_version=1)
    assert 'engineering_action_version' not in legacy.model_dump(mode='json')
    with pytest.raises(ValueError, match='inspection'):
        DispatchStep(id='build', agent='builder', task='Build.', tools=tools,
            engineering_delivery_contract_version=1, engineering_action_version=1)
    current = DispatchStep(id='build', agent='builder', task='Build.',
        tools=(*tools, 'project.inspect'), engineering_delivery_contract_version=1,
        engineering_action_version=1)
    assert current.engineering_action_version == 1


def test_action_writer_exposes_inspection_even_without_workspace_read_tool() -> None:
    role = RoleAssignment(id='builder', role='Builder', purpose=RolePurpose.EXECUTE,
        mission='Build the module.', must_answer=('What passed?',),
        allowed_tools=('workspace.write_text', 'project.verify', 'workspace.bundle'),
        forbidden_actions=('Do not escape scope.',), skills=(), output_schema={'summary': 'string'}, model='main')
    context = TaskContext(run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH,
        request='Build a Node.js module.', routing_decision={
            'engineering_delivery_contract_version': 1, 'engineering_action_version': 1})
    plan = defaults._dispatch_plan((role,), context, capability_gateway=FakeCapabilityAvailability({
        'workspace.write_text', 'project.verify', 'workspace.bundle', 'project.inspect'}))
    assert plan.steps[0].engineering_action_version == 1
    assert 'project.inspect' in plan.steps[0].tools


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
    current = context.model_copy(update={'routing_decision': {
        **context.routing_decision, 'engineering_action_version': 1}})
    new_plan = defaults._discussion_plan(roles, 'main', current, capability_gateway=available)
    writer = next(participant for participant in new_plan.participants if 'workspace.write_text' in participant.allowed_tools)
    assert writer.max_output_tokens == min(current.token_budget, 24_576)
    assert defaults._discussion_engineering_roles(readers, TaskContext(
        run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISCUSS, request='Discuss this idea.',
    ), 'main') == readers
