"""Maintenance is a versioned completion contract, never additional authority."""
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import defaults as d
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose
from tests.unit.runtime.test_configured_runtime import TENANT_ID, FakeCapabilityAvailability
from tests.unit.test_workspace_maintenance_contract import (
    SEMANTIC_REPAIR_REQUEST,
    V2_COMMANDS_WITH_REFERENCES,
    V2_NON_EXECUTING_REQUESTS,
)

MARKER = 'Existing workspace maintenance contract:'
MESSAGE = 'Fix TypeScript compatibility in the existing project files, then deliver workspace.bundle.'
TOOLS = ('read_context', 'project.generate_zip', 'workspace.list', 'workspace.read',
         'workspace.write_text', 'workspace.bundle')


def context(version: object = 1, **route: Any) -> TaskContext:
    return TaskContext(run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH, request=MESSAGE,
        token_budget=100000, timeout_seconds=600,
        routing_decision={'role_planning_intent_version': 1, 'workspace_project_explicit': True,
            'project_id': 'existing', 'workspace_session_id': 'session',
            'workspace_maintenance_contract_version': cast(JsonValue, version), 'sandbox_profile': 'workspace_write',
            'requested_permissions': ('workspace.read', 'workspace.write'), **route})


def role(**changes: Any) -> RoleAssignment:
    return replace(RoleAssignment(id='implementer', role='Implementer', purpose=RolePurpose.EXECUTE,
        mission='Repair the existing files.', must_answer=('What changed?',), allowed_tools=TOOLS,
        forbidden_actions=('Do not bypass permissions.',), skills=(), output_schema={'summary': 'string'},
        model='fixture'), **changes)


def plan(ctx: TaskContext, roles: tuple[RoleAssignment, ...] | None = None) -> Any:
    return d._dispatch_plan(roles or (role(),), ctx, capability_gateway=FakeCapabilityAvailability(set(TOOLS)))


def test_v1_marks_only_existing_producer_and_keeps_execution_input_and_budgets() -> None:
    ctx = context()
    before = ctx.to_payload()
    legacy = plan(context(0))
    current = plan(ctx)
    step = current.steps[0]
    assert MARKER in step.task
    assert step.workspace_maintenance_contract_version == 1
    assert current.to_payload()['steps'][0]['workspace_maintenance_contract_version'] == 1
    assert all(step.workspace_maintenance_contract_version == 0 for step in current.steps[1:])
    assert 'Build the project incrementally' not in step.task
    assert 'User task: ' + MESSAGE in step.task
    assert step.tools == ('workspace.list', 'workspace.read', 'workspace.write_text', 'workspace.bundle')
    assert current.agents[0].allowed_tools == step.tools == current.allowed_tools
    assert 'tests' in step.task and 'package' in step.task and 'config' in step.task
    assert ctx.to_payload() == before
    assert current.total_token_budget == legacy.total_token_budget
    for old, new in zip(legacy.steps, current.steps, strict=True):
        assert (new.token_budget, new.timeout_seconds, new.cost_budget_usd) == (
            old.token_budget, old.timeout_seconds, old.cost_budget_usd)
    assert current.agents[0].max_output_tokens == legacy.agents[0].max_output_tokens


def test_legacy_missing_and_zero_preserve_exact_plan_payload() -> None:
    zero = context(0)
    missing = zero.model_copy(update={'routing_decision': {
        k: v for k, v in zero.routing_decision.items() if k != 'workspace_maintenance_contract_version'}})
    assert plan(zero).to_payload() == plan(missing).to_payload()
    assert MARKER not in plan(zero).steps[0].task
    assert plan(zero).steps[0].tools == TOOLS
    assert all('workspace_maintenance_contract_version' not in row for row in plan(zero).to_payload()['steps'])


def test_user_marker_does_not_enable_legacy_contract_or_change_legacy_tools() -> None:
    ctx = context(0).model_copy(update={'request': MESSAGE + '\n' + MARKER})
    step = plan(ctx).steps[0]
    assert step.task.count(MARKER) == 1  # Only the unchanged user request, no planner injection.
    assert step.tools == TOOLS


@pytest.mark.parametrize('missing', ['workspace_project_explicit', 'project_id', 'workspace_session_id'])
def test_no_explicit_workspace_no_contract(missing: str) -> None:
    ctx = context()
    ctx = ctx.model_copy(update={'routing_decision': {
        k: v for k, v in ctx.routing_decision.items() if k != missing}})
    assert MARKER not in plan(ctx).steps[0].task


@pytest.mark.parametrize('absent', ['workspace.write_text', 'workspace.bundle'])
def test_missing_existing_tool_is_not_added(absent: str) -> None:
    assigned = role(allowed_tools=tuple(t for t in TOOLS if t != absent))
    step = plan(context(), (assigned,)).steps[0]
    assert MARKER not in step.task and absent not in step.tools


def test_verifier_does_not_receive_producer_maintenance_guidance() -> None:
    verifier = role(id='tester', purpose=RolePurpose.VERIFY)
    step = plan(context(), (verifier,)).steps[0]
    assert MARKER not in step.task and 'read_context' in step.tools
    assert step.workspace_maintenance_contract_version == 0


@pytest.mark.parametrize('version', [2, -1, True, '1', None, 1.0])
def test_unknown_contract_version_rejected(version: object) -> None:
    with pytest.raises(ValueError, match='unsupported workspace maintenance contract version'):
        plan(context(version))


def test_build_path_is_not_converted_to_existing_maintenance() -> None:
    ctx = context().model_copy(update={'request': 'Build a new TypeScript project and deliver a ZIP.'})
    assert MARKER not in plan(ctx).steps[0].task


@pytest.mark.parametrize('tool', ['workspace.write_text', 'workspace.bundle'])
def test_unavailable_tool_is_not_granted_by_contract(tool: str) -> None:
    current = d._dispatch_plan((role(),), context(),
        capability_gateway=FakeCapabilityAvailability(set(TOOLS) - {tool}))
    assert tool not in current.steps[0].tools and MARKER not in current.steps[0].task


def fresh_checkpoint(ctx: TaskContext, current: Any) -> Any:
    """Exercise native checkpoint qualification without starting a model/tool run."""
    runtime = adapter.CrewDispatchRuntime(cast(Any, object()), current, crew_factory=cast(Any, object()))
    state = adapter._RunState(adapter._RunToken(1),
        post_tool_finalization_growth_version=cast(int, ctx.routing_decision.get('post_tool_finalization_growth_version', 0)),
        workspace_maintenance_feedback_version=cast(int, ctx.routing_decision.get('workspace_maintenance_feedback_version', 0)))
    return runtime._make_checkpoint(ctx, current, state, {}, {}, adapter._ToolLedger(), adapter._ModelLedger(),
        adapter._UsageLedger(), adapter._ReviewLedger(), next_sequence=1, terminal=False, phase='running',
        remaining_timeout_seconds=ctx.timeout_seconds)


@pytest.mark.parametrize('message', [
    SEMANTIC_REPAIR_REQUEST,
    'Maintain this existing project. Call workspace.write_text for changed files, then workspace.bundle.',
    '\u7ef4\u62a4\u73b0\u6709\u9879\u76ee\u3002\u4f7f\u7528 workspace.write_text \u4fee\u590d\u751f\u4ea7\u6587\u4ef6\uff0c\u7136\u540e workspace.bundle\u3002',
    '\u7ef4\u62a4\u73b0\u6709\u9879\u76ee\u3002\u7528 workspace.write_text \u4fee\u590d\u751f\u4ea7\u6587\u4ef6\u3002\u4f7f\u7528 workspace.bundle\u3002',
])
def test_tool_imperative_request_builds_fresh_producer_and_checkpoint(message: str) -> None:
    ctx = context(conversation_id='conversation', post_tool_finalization_growth_version=1,
        workspace_maintenance_feedback_version=1, workspace_maintenance_intent_version=2).model_copy(update={'request': message})
    before = ctx.to_payload()
    current = plan(ctx)
    legacy = plan(ctx.model_copy(update={'routing_decision': {**ctx.routing_decision, 'workspace_maintenance_contract_version': 0}}))
    step = current.steps[0]
    assert step.workspace_maintenance_contract_version == 1 and MARKER in step.task
    assert step.tools == ('workspace.list', 'workspace.read', 'workspace.write_text', 'workspace.bundle')
    assert current.agents[0].allowed_tools == current.allowed_tools == step.tools
    assert current.agents[0].logical_model == legacy.agents[0].logical_model
    assert current.agents[0].max_output_tokens == legacy.agents[0].max_output_tokens
    assert current.total_token_budget == legacy.total_token_budget
    assert [(s.token_budget, s.timeout_seconds, s.cost_budget_usd) for s in current.steps] == [
        (s.token_budget, s.timeout_seconds, s.cost_budget_usd) for s in legacy.steps]
    assert ctx.checkpoint is None and ctx.to_payload() == before
    checkpoint = fresh_checkpoint(ctx, current)
    assert checkpoint.state['workspace_productive_boundary'] is True
    assert checkpoint.state['workspace_meaningful_progress'] is True
    assert checkpoint.state['workspace_context_refresh'] is True
    assert checkpoint.state['workspace_refresh_round_continuation'] is True
    assert checkpoint.state['post_tool_finalization_growth_version'] == 1
    assert checkpoint.state['workspace_maintenance_feedback_version'] == 1
    assert checkpoint.state['models'] == checkpoint.state['tools'] == {}
    assert checkpoint.state['usage']['tokens'] == 0
    assert 'workspace_productive_boundary' not in fresh_checkpoint(ctx, legacy).state


@pytest.mark.parametrize('guard', ['version_zero', 'version_missing', 'workspace_project_explicit',
    'project_id', 'workspace_session_id', 'workspace.write_text', 'workspace.bundle', 'unavailable_write', 'unavailable_bundle'])
def test_tool_imperative_does_not_bypass_contract_scope_or_tool_inventory(guard: str) -> None:
    ctx = context(workspace_maintenance_intent_version=2).model_copy(update={'request': SEMANTIC_REPAIR_REQUEST})
    route = dict(ctx.routing_decision)
    assigned = role()
    available = set(TOOLS)
    if guard == 'version_zero':
        route['workspace_maintenance_contract_version'] = 0
    elif guard == 'version_missing':
        del route['workspace_maintenance_contract_version']
    elif guard in {'workspace.write_text', 'workspace.bundle'}:
        assigned = role(allowed_tools=tuple(t for t in TOOLS if t != guard))
    elif guard.startswith('unavailable_'):
        available.remove('workspace.write_text' if guard == 'unavailable_write' else 'workspace.bundle')
    else:
        del route[guard]
    ctx = ctx.model_copy(update={'routing_decision': route})
    current = d._dispatch_plan((assigned,), ctx, capability_gateway=FakeCapabilityAvailability(available))
    assert current.steps[0].workspace_maintenance_contract_version == 0
    assert MARKER not in current.steps[0].task
    assert set(current.steps[0].tools) <= set(assigned.allowed_tools) & available
    assert 'workspace_productive_boundary' not in fresh_checkpoint(ctx, current).state


def test_missing_and_v1_intent_preserve_legacy_plan_digest_and_tools() -> None:
    missing = context().model_copy(update={'request': SEMANTIC_REPAIR_REQUEST})
    v1 = missing.model_copy(update={'routing_decision': {**missing.routing_decision, 'workspace_maintenance_intent_version': 1}})
    baseline = missing.model_copy(update={'routing_decision': {**missing.routing_decision, 'workspace_maintenance_contract_version': 0}})
    missing_plan, v1_plan, baseline_plan = plan(missing), plan(v1), plan(baseline)
    assert missing_plan.to_payload() == v1_plan.to_payload() == baseline_plan.to_payload()
    assert missing_plan.digest == v1_plan.digest == baseline_plan.digest
    assert missing_plan.steps[0].tools == TOOLS
    assert missing_plan.steps[0].workspace_maintenance_contract_version == 0
    for ctx, current in ((missing, missing_plan), (v1, v1_plan)):
        assert MARKER not in current.steps[0].task
        assert 'workspace_productive_boundary' not in fresh_checkpoint(ctx, current).state


@pytest.mark.parametrize('version', [0, 3, -1, None, True, False, '2', 2.0])
def test_dispatch_plan_rejects_unsupported_maintenance_intent_version(version: object) -> None:
    ctx = context(workspace_maintenance_intent_version=version).model_copy(update={'request': SEMANTIC_REPAIR_REQUEST})
    with pytest.raises(ValueError, match='unsupported workspace maintenance intent version'):
        plan(ctx)


@pytest.mark.parametrize('message', V2_NON_EXECUTING_REQUESTS)
def test_v2_nonexecuting_input_preserves_plan_and_never_qualifies_checkpoint(message: str) -> None:
    ctx = context(workspace_maintenance_intent_version=2, conversation_id='conversation',
        post_tool_finalization_growth_version=1, workspace_maintenance_feedback_version=1).model_copy(update={'request': message})
    before = ctx.to_payload()
    current = plan(ctx)
    baseline = plan(ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'workspace_maintenance_contract_version': 0}}))
    assert current.to_payload() == baseline.to_payload() and current.digest == baseline.digest
    assert current.steps[0].workspace_maintenance_contract_version == 0
    assert MARKER not in current.steps[0].task and current.steps[0].tools == TOOLS
    assert ctx.to_payload() == before
    checkpoint = fresh_checkpoint(ctx, current)
    assert 'workspace_productive_boundary' not in checkpoint.state
    assert checkpoint.state['models'] == checkpoint.state['tools'] == {}
    assert checkpoint.state['usage']['tokens'] == 0


@pytest.mark.parametrize('message', V2_COMMANDS_WITH_REFERENCES)
def test_v2_actual_command_with_reference_still_qualifies_fresh_checkpoint(message: str) -> None:
    ctx = context(workspace_maintenance_intent_version=2, conversation_id='conversation',
        post_tool_finalization_growth_version=1, workspace_maintenance_feedback_version=1).model_copy(update={'request': message})
    current = plan(ctx)
    assert current.steps[0].workspace_maintenance_contract_version == 1
    assert MARKER in current.steps[0].task
    assert current.steps[0].tools == ('workspace.list', 'workspace.read', 'workspace.write_text', 'workspace.bundle')
    checkpoint = fresh_checkpoint(ctx, current)
    assert checkpoint.state['workspace_productive_boundary'] is True
    assert checkpoint.state['models'] == checkpoint.state['tools'] == {}
