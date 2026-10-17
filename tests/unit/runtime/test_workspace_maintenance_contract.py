"""Maintenance is a versioned completion contract, never additional authority."""
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import defaults as d
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose
from tests.unit.runtime.test_configured_runtime import TENANT_ID, FakeCapabilityAvailability

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
    ctx = context(); before = ctx.to_payload()
    legacy = plan(context(0)); current = plan(ctx)
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
    zero = context(0); missing = zero.model_copy(update={'routing_decision': {
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
    ctx = context(); ctx = ctx.model_copy(update={'routing_decision': {
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
