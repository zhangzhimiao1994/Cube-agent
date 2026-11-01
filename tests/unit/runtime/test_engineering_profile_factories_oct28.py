"""Actual role factories must consume domain hints without changing execution scope."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.config.schema import PlatformConfig
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import defaults as d
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.role_planner import RolePlan, RolePlanner, RolePlanningRequest, TaskProfile


class PlanningBoundary(Exception):
    """Stop before any child runtime or model can execute."""


@pytest.mark.parametrize('mode', (TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID))
@pytest.mark.parametrize('trusted_domain', (False, True))
def test_actual_factories_consume_validated_domain_only_for_new_policy(
    monkeypatch: pytest.MonkeyPatch, mode: TaskMode, trusted_domain: bool,
) -> None:
    config = PlatformConfig.model_validate({'models': {'fixture': {'deployments': [{
        'provider': 'openai', 'model': 'fixture', 'secret_ref': 'fixture', 'quota_scope_id': 'fixture',
        'max_concurrency': 1, 'capabilities': ['text', 'tool_calling', 'structured_output'],
    }]}}, 'agents': []})
    monkeypatch.setattr(d, '_current_platform_config', AsyncMock(return_value=config))
    monkeypatch.setattr(d, '_gateway_for_config', AsyncMock(return_value=(object(), 'fixture', None)))
    seen: list[RolePlanningRequest] = []
    checked: list[TaskContext] = []

    def domain(context: TaskContext) -> bool:
        checked.append(context)
        return trusted_domain

    monkeypatch.setattr(d, 'engineering_software_context', domain, raising=False)

    class Capture(RolePlanner):
        def plan(self, request: RolePlanningRequest) -> RolePlan:
            seen.append(request)
            raise PlanningBoundary

    runtime_type = {TaskMode.DISPATCH: d.ConfigBackedDispatchRuntime,
        TaskMode.DISCUSS: d.ConfigBackedDiscussionRuntime, TaskMode.HYBRID: d.ConfigBackedHybridRuntime}[mode]
    runtime = runtime_type(config_service=cast(Any, object()), secret_service=cast(Any, object()),
        capacity_factory=cast(Any, object()), transport=cast(Any, object()), role_planner=Capture())
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role=Role.OPERATOR,
        mode=mode, request='Add CSV export.', routing_decision={
            'role_planning_intent_version': 3, 'direct_model': 'fixture', 'allowed_models': ('fixture',),
            'sandbox_profile': 'read_only', 'requested_permissions': ('workspace.read',),
        })
    before = context.to_payload()
    pending = runtime._runtime_for(context)
    try:
        with pytest.raises(PlanningBoundary):
            pending.send(None)
    finally:
        pending.close()
    assert len(seen) == 1 and checked == [context]
    assert seen[0].profile is (TaskProfile.SOFTWARE if trusted_domain else TaskProfile.GENERAL)
    assert seen[0].profiles == ((TaskProfile.SOFTWARE, TaskProfile.GENERAL)
                                if trusted_domain else (TaskProfile.GENERAL,))
    assert context.to_payload() == before


@pytest.mark.parametrize('version', (0, 1, 2))
def test_legacy_context_does_not_consume_new_domain_hint(
    monkeypatch: pytest.MonkeyPatch, version: int,
) -> None:
    def forbidden(_: TaskContext) -> bool:
        pytest.fail('legacy planning must retain its persisted domain')

    monkeypatch.setattr(d, 'engineering_software_context', forbidden, raising=False)
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.HYBRID,
        request='Add CSV export.', routing_decision={'role_planning_intent_version': version})
    profile, profiles = d._context_task_profiles(context, intent_version=version)
    assert profile is TaskProfile.GENERAL and profiles == (TaskProfile.GENERAL,)


def test_new_policy_profiles_ignore_negative_incident_and_budget_advice() -> None:
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.HYBRID,
        request='维护现有代码并增加排序。不能把环境故障当作功能失败。不修改平台配置、预算。',
        routing_decision={'role_planning_intent_version': 3})
    profile, profiles = d._context_task_profiles(context, intent_version=3)
    assert profile is TaskProfile.SOFTWARE
    assert profiles == (TaskProfile.SOFTWARE, TaskProfile.GENERAL)


@pytest.mark.parametrize('prompt', (
    'Create a CLI for aggregating CSV totals.',
    'Create a command-line utility for CSV totals.',
    'Build a command line utility for CSV totals.',
    '请创建一个命令行工具，汇总 CSV 的金额。',
))
def test_first_command_line_project_does_not_require_prior_success(prompt: str) -> None:
    assert d._task_profile(prompt, intent_version=3) is TaskProfile.SOFTWARE
    assert d._task_profiles(prompt, intent_version=3) == (
        TaskProfile.SOFTWARE, TaskProfile.GENERAL,
    )


@pytest.mark.parametrize('prompt', (
    'Conduct a clinical survey.',
    'Summarize the client feedback.',
    'Do not build a CLI; summarize the totals.',
))
def test_command_line_domain_uses_word_boundaries(prompt: str) -> None:
    assert d._task_profile(prompt, intent_version=3) is TaskProfile.GENERAL
    assert d._task_profiles(prompt, intent_version=3) == (TaskProfile.GENERAL,)


@pytest.mark.parametrize('version', (0, 1, 2))
def test_command_line_domain_does_not_change_legacy_policy(version: int) -> None:
    assert d._task_profile('Create a CLI for aggregating CSV totals.', intent_version=version) is TaskProfile.GENERAL


@pytest.mark.parametrize('mode', (TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID))
async def test_real_prepared_original_context_reaches_each_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: TaskMode,
) -> None:
    from agent_hub.runs.automatic_project_inputs import _binding
    from tests.unit.runtime.test_engineering_role_context_oct28 import clone, original

    context = clone(await original(tmp_path), mode=mode, request='给现有工具增加功能。')
    route = json.loads(context.model_dump_json())['routing_decision']
    route['automatic_project_input_selection']['binding_sha256'] = _binding(context)
    context = clone(context, routing_decision=route)
    before = context.model_dump_json()
    assert d._task_profile(context.request, intent_version=3) is TaskProfile.GENERAL
    config = PlatformConfig.model_validate({'models': {'fixture': {'deployments': [{
        'provider': 'openai', 'model': 'fixture', 'secret_ref': 'fixture', 'quota_scope_id': 'fixture',
        'max_concurrency': 1, 'capabilities': ['text', 'tool_calling', 'structured_output'],
    }]}}, 'agents': []})
    monkeypatch.setattr(d, '_current_platform_config', AsyncMock(return_value=config))
    monkeypatch.setattr(d, '_gateway_for_config', AsyncMock(return_value=(object(), 'fixture', None)))
    seen: list[RolePlanningRequest] = []

    class Capture(RolePlanner):
        def plan(self, request: RolePlanningRequest) -> RolePlan:
            seen.append(request)
            raise PlanningBoundary

    runtime_type = {TaskMode.DISPATCH: d.ConfigBackedDispatchRuntime,
        TaskMode.DISCUSS: d.ConfigBackedDiscussionRuntime, TaskMode.HYBRID: d.ConfigBackedHybridRuntime}[mode]
    runtime = runtime_type(config_service=cast(Any, object()), secret_service=cast(Any, object()),
        capacity_factory=cast(Any, object()), transport=cast(Any, object()), role_planner=Capture())
    with pytest.raises(PlanningBoundary):
        await runtime._runtime_for(context)
    assert len(seen) == 1 and seen[0].profile is TaskProfile.SOFTWARE
    assert seen[0].profiles == (TaskProfile.SOFTWARE, TaskProfile.GENERAL)
    assert context.model_dump_json() == before
