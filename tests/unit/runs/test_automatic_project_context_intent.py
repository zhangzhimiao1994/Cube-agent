"""Context identifies an existing scope; only a following command selects execution."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.automatic_project_inputs import maintenance_intent
from agent_hub.runs.service import RunService
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.automatic_conversation_input_typed_fixture import (
    SubmissionOptions,
    preparation_fixture,
)
from tests.unit.runs.conversation_authenticated_input_typed_fixture import UnexpectedQueue, mutable
from tests.unit.runs.test_automatic_project_inputs_oct28 import STATE, VERSION, submission
from tests.unit.runs.test_project_input_lifecycle import ObservingRuntime


@pytest.mark.parametrize('message', [
    '继续当前普通用户的既有 ledger 项目和会话，只修复当前工作区，不新建项目。',
    '在当前项目中，请修复金额排序错误，保留全部测试。',
    '针对现有工作区，修改生产实现并重新验证。',
    '沿用同一项目及会话，仅修补实现。',
    '对于已经交付的 calculator 项目，请增加导出功能。',
    '请继续当前项目，先修复失败测试对应的生产代码。',
    '在同一工作区内：更新实现，保留已有测试。',
    'For the existing ledger project, please fix the sorting defect.',
    'In the current workspace, repair the implementation and preserve tests.',
    'Within the same project and session, add export support.',
])
def test_context_prefix_requires_a_following_maintenance_command(message: str) -> None:
    assert maintenance_intent(message) is True


@pytest.mark.parametrize('message', [
    '继续当前项目。',
    '继续当前项目，读取当前源码。',
    '继续当前项目，如何修复排序错误？',
    '继续当前项目，是否修复排序错误。',
    '继续当前项目，只分析修复方案。',
    '继续当前项目，先规划如何修复。',
    '继续当前项目，不要修复生产代码。',
    '继续当前项目，修复生产代码，但不要执行。',
    '继续当前项目，修复生产代码，但禁止修改。',
    '如果继续当前项目，请修复错误。',
    '日志：继续当前项目，修复错误。',
    '继续当前项目，日志记录：修复错误。',
    '继续当前项目，错误消息要求修复错误。',
    '继续当前项目，示例：修复错误。',
    '继续当前项目，"修复错误"。',
    '继续当前项目，`修复错误`。',
    '继续当前项目，```text\n修复错误\n```',
    '> 继续当前项目，修复错误。',
    'UNTRUSTED_DATA_JSON={"request":"继续当前项目，修复错误"}',
    '继续当前项目，UNTRUSTED_DATA_JSON={"command":"修复错误"}',
    '继续当前项目。日志：修复错误。',
    '继续当前项目，请解释修复错误的方法。',
    'For the existing project, explain how to fix the defect.',
    'In the current workspace, do not repair the implementation.',
    'Within the same project, fix the defect only if it is approved.',
    'In the current workspace, fix the defect?',
    'For the existing project, "fix the defect".',
    'In some arbitrary context, fix the current project.',
])
def test_context_does_not_turn_data_or_discussion_into_execution(message: str) -> None:
    assert maintenance_intent(message) is False


def test_context_command_reaches_existing_submission_gate_without_hidden_execution() -> None:
    route = submission('在当前项目中，请修复排序错误，保留全部测试。')
    assert route[VERSION] == 1
    assert 'project_input' not in route
    assert 'project_input_preparation' not in route
    assert route['requested_permissions'] == ['workspace.read', 'workspace.write', 'command.run']


@pytest.mark.parametrize('options', [
    {'requested_permissions': ('workspace.read', 'workspace.write')},
    {'conversation_id': 'not-the-existing-conversation'},
    {'replace_workspace_files': True},
])
def test_context_command_does_not_bypass_existing_service_scope_gate(
    options: SubmissionOptions,
) -> None:
    # The typed fixtures define the same service inputs; each defect must keep
    # the automatic marker absent even when the new intent spelling is valid.
    assert VERSION not in submission(
        '在当前项目中，请修复排序错误。', **options,
    )


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_context_command_observes_original_before_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: TaskMode,
) -> None:
    message = '在当前项目中，请修复排序错误，保留全部测试。'
    submitted = submission(message)
    preparer, repository, _, _, calls, _, _ = await preparation_fixture(tmp_path)
    monkeypatch.setattr('tests.unit.runs.test_terminal_hooks.TENANT_ID', repository.row.tenant_id)
    monkeypatch.setattr('tests.unit.runs.project_auto_repair_typed_fixture.TENANT_ID', repository.row.tenant_id)
    route = mutable(repository.row.routing_decision)
    route.pop('project_input')
    route.pop('project_input_contract_version')
    route.update({VERSION: submitted[VERSION], 'generated_project_contract_version': 1,
                  'permission_mode': 'default', 'conversation_id': 'conv-maintain'})
    repository.row.request = message
    repository.row.mode = mode.value
    service = RunService(repository, router=None, task_queue=UnexpectedQueue(),
        runtime_registry=RuntimeRegistry((ObservingRuntime(mode, calls),)),
        project_input_preparer=preparer, worker_id='test-worker')
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.COMPLETED
    assert calls == ['authorize', 'execute-original', 'model']
    persisted = mutable(repository.row.routing_decision)
    assert mutable(persisted[STATE])['kind'] == 'original'
    assert mutable(persisted['project_input_preparation'])['phase'] == 'observed'
    assert 'generated_project_contract_version' not in persisted
