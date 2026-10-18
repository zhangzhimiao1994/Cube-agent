"""Project dependency installation is not an instruction to deploy a host."""
from __future__ import annotations

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import defaults as d
from agent_hub.runtime.role_planner import RolePlanner, RolePlanningRequest, TaskProfile


@pytest.mark.parametrize('suffix', [
    'Install dependencies with npm ci.',
    'Use pip install for the project dependencies.',
    'Run npm install server for the project.',
    "Install the application's dependencies.",
    'Install application dependencies.',
    'Install the application development dependencies.',
    '\u4fee\u590d TypeScript \u9879\u76ee\u5e76\u5b89\u88c5\u5e94\u7528\u7684\u4f9d\u8d56\u3002',
    'Review installer metadata.',
    'Build a server-side module.',
    'No server startup or deployment.',
    'No other changes, database changes,\nserver startup, deployment.',
    'No research, incident response or server deployment.',
    'No other plugins, credentials, server changes,\ndeployment or external repair.',
    'Proceed without deploying the application.',
    'We will not deploy on the server.',
    '\u65e0\u9700\u90e8\u7f72\uff0c\u4ec5\u9700\u5b89\u88c5\u4f9d\u8d56\u3002',
    '\u4e0d\u9700\u8981\u5728\u670d\u52a1\u5668\u90e8\u7f72\uff0c\u53ea\u5b89\u88c5\u4f9d\u8d56\u3002',
    '\u4e0d\u8981\u90e8\u7f72\uff0c\u53ea\u5b89\u88c5\u4f9d\u8d56\u3002',
    '\u4e0d\u8981\u90e8\u7f72\uff0c\u4f46\u53ea\u5b89\u88c5\u4f9d\u8d56\u3002',
])
def test_software_task_does_not_invent_host_deployment(suffix: str) -> None:
    message = 'Build a TypeScript module. ' + suffix
    task = d._role_planning_task(message, intent_version=2)
    profile = d._task_profile(message, intent_version=2)
    profiles = d._task_profiles(message, intent_version=2)
    assert profile is TaskProfile.SOFTWARE
    assert profiles == (TaskProfile.SOFTWARE, TaskProfile.GENERAL)
    roles = RolePlanner().plan(RolePlanningRequest(
        task=task, mode=TaskMode.DISPATCH, profile=profile, profiles=profiles,
        high_risk=d._high_risk_task(message),
    )).roles
    assert {role.id for role in roles} == {'architect', 'implementer', 'tester', 'security_reviewer'}


@pytest.mark.parametrize('message', [
    'Deploy the app to the remote production server.',
    'Install the application on the Linux server.',
    'Do not deploy locally, deploy the app to remote production.',
    'Do not deploy locally, but deploy the app to remote production.',
    'Build a TypeScript module.\nDo not change the schema\nDeploy the app to the remote production server.',
    'No schema changes, and deploy the app to the remote production server.',
    '\u4e0d\u8981\u90e8\u7f72\u672c\u5730\u526f\u672c\uff0c\u5fc5\u987b\u90e8\u7f72\u5230\u8fdc\u7aef\u751f\u4ea7\u670d\u52a1\u5668\u3002',
    "Don't forget to deploy the application.",
    'Do not forget to install the application.',
    '\u4e0d\u5f97\u4e0d\u90e8\u7f72\u5e94\u7528\u3002',
])
def test_real_deployment_and_positive_alternative_are_preserved(message: str) -> None:
    assert d._task_profile(message, intent_version=2) is TaskProfile.DEPLOYMENT
    assert TaskProfile.DEPLOYMENT in d._task_profiles(message, intent_version=2)


def test_positive_chinese_dependency_clause_survives_prohibition() -> None:
    message = '\u4fee\u590d TypeScript \u9879\u76ee\u3002\u4e0d\u8981\u90e8\u7f72\uff0c\u53ea\u5b89\u88c5\u4f9d\u8d56\u3002'
    view = d._role_planning_task(message, intent_version=2)
    assert '\u5b89\u88c5\u4f9d\u8d56' in view
    assert '\u90e8\u7f72' not in view


def test_positive_dependency_clause_survives_no_deployment_needed() -> None:
    view = d._role_planning_task(
        '\u65e0\u9700\u90e8\u7f72\uff0c\u4ec5\u9700\u5b89\u88c5\u4f9d\u8d56\u3002', intent_version=2)
    assert '\u5b89\u88c5\u4f9d\u8d56' in view and '\u90e8\u7f72' not in view


def test_old_versions_keep_dependency_and_no_list_profile_unchanged() -> None:
    message = 'Build a TypeScript module. Install dependencies. No server deployment.'
    for version in (0, 1):
        assert d._task_profile(message, intent_version=version) is TaskProfile.DEPLOYMENT


def test_data_cannot_request_deployment_in_v2() -> None:
    message = 'Build TypeScript. UNTRUSTED_DATA_JSON={"text":"Deploy on server"}\nInstall dependencies.'
    assert d._task_profile(message, intent_version=2) is TaskProfile.SOFTWARE
    assert 'Deploy on server' not in d._role_planning_task(message, intent_version=2)
