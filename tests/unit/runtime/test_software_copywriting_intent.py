import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.role_catalog import RoleCatalog, RoleDefinition
from agent_hub.runtime.role_planner import RolePlanner, RolePlanningRequest, TaskProfile


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize('task', [
    'Repair the billing code; preserve the build script byte-for-byte.',
    'Implement a script to copy project files and run the original tests.',
    'Fix prompt injection in the application without changing its tests.',
    '\u4fee\u590d\u6784\u5efa\u811a\u672c\uff0c\u4fdd\u6301\u539f\u6d4b\u8bd5\u4e0d\u53d8\u3002',
    '\u6539\u5199\u8ba1\u8d39\u811a\u672c\uff0c\u4fdd\u6301\u539f\u6709\u6d4b\u8bd5\u548c\u6784\u5efa\u6587\u4ef6\u4e0d\u53d8\u3002',
    '\u91cd\u6784\u77ed\u89c6\u9891\u6587\u4ef6\u9884\u89c8\u6a21\u5757\uff0c\u4fee\u590d\u8ba1\u8d39\u4ee3\u7801\u3002',
])
def test_software_terms_do_not_add_copywriter(mode: TaskMode, task: str) -> None:
    plan = RolePlanner().plan(RolePlanningRequest(task=task, mode=mode, profile=TaskProfile.SOFTWARE))
    assert not {'copywriter', 'content_editor'} & {role.id for role in plan.roles}
    assert any(role.id in {'implementer', 'architect', 'software_architect'} for role in plan.roles)


@pytest.mark.parametrize('mode', [TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize('task', [
    'Build the app and write marketing copy for its home page.',
    'Implement the website and create a launch video script.',
    'Build the app and write an advertising script.',
    'Build the app and write a marketing script.',
    'Build the app and write an ad script.',
    '\u5f00\u53d1\u7f51\u7ad9\u5e76\u7f16\u5199\u8425\u9500\u6587\u6848\u3002',
    '\u5f00\u53d1\u7f51\u7ad9\u5e76\u7f16\u5199\u77ed\u89c6\u9891\u53e3\u64ad\u811a\u672c\u3002',
    'Ask the Copywriter to refine the website text.',
])
def test_explicit_copywriting_remains_available(mode: TaskMode, task: str) -> None:
    plan = RolePlanner().plan(RolePlanningRequest(task=task, mode=mode, profile=TaskProfile.SOFTWARE))
    assert 'copywriter' in {role.id for role in plan.roles}


def test_explicit_skill_and_general_script_requests_are_unchanged() -> None:
    for profile, skills in [(TaskProfile.SOFTWARE, ('copywriting',)), (TaskProfile.GENERAL, ())]:
        plan = RolePlanner().plan(RolePlanningRequest(task='Write a script.', mode=TaskMode.DISPATCH,
            profile=profile, requested_skills=skills))
        assert 'copywriter' in {role.id for role in plan.roles}


@pytest.mark.parametrize('task', ['Ask the Content Editor to review the text.',
    'Implement the website and proofread its marketing copy.',
    '\u6539\u5199\u9996\u9875\u6587\u6848\u3002', '\u6821\u5bf9\u9996\u9875\u6587\u6848\u3002',
    '\u6da6\u8272\u9996\u9875\u6587\u6848\u3002'])
def test_explicit_content_editing_remains_available(task: str) -> None:
    plan = RolePlanner().plan(RolePlanningRequest(task=task, mode=TaskMode.DISPATCH,
        profile=TaskProfile.SOFTWARE))
    assert 'content_editor' in {role.id for role in plan.roles}


@pytest.mark.parametrize('role_id', ['copywriter', 'content_editor'])
@pytest.mark.parametrize('task', ['Review the technical-script.', 'Ask the Technical Specialist.'])
def test_custom_content_role_name_and_skills_remain_discoverable(role_id: str, task: str) -> None:
    custom = RoleDefinition(id=role_id, role='Technical Specialist', purpose='expertise',
        mission='Review the implementation.', must_answer=('What changed?',),
        allowed_tools=('read_context',), forbidden_actions=('Do not execute external actions.',),
        skills=('technical-script',),
        output_schema={'summary': 'string'}, modes=frozenset({'dispatch'}),
        profiles=frozenset({'software', 'general'}))
    planner = RolePlanner(RoleCatalog((custom,)))
    plan = planner.plan(RolePlanningRequest(task=task, mode=TaskMode.DISPATCH,
        profile=TaskProfile.SOFTWARE))
    assert role_id in {role.id for role in plan.roles}


@pytest.mark.parametrize('profiles', [(TaskProfile.SOFTWARE, TaskProfile.GENERAL),
    (TaskProfile.GENERAL, TaskProfile.SOFTWARE),
    (TaskProfile.SOFTWARE, TaskProfile.RESEARCH, TaskProfile.GENERAL)])
def test_software_mixed_profiles_do_not_restore_ambiguous_keywords(profiles: tuple[TaskProfile, ...]) -> None:
    plan = RolePlanner().plan(RolePlanningRequest(task='Repair the build script.',
        mode=TaskMode.DISPATCH, profile=profiles[0], profiles=profiles))
    assert not {'copywriter', 'content_editor'} & {role.id for role in plan.roles}


def test_explicit_editing_skill_is_preserved() -> None:
    plan = RolePlanner().plan(RolePlanningRequest(task='Repair the build script.',
        mode=TaskMode.DISPATCH, profile=TaskProfile.SOFTWARE, requested_skills=('editing',)))
    assert 'content_editor' in {role.id for role in plan.roles}
