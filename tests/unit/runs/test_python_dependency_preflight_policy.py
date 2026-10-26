"""Fresh server-owned dependency observation selection; no Root or model calls."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.action_direct import _binding
from tests.unit.runs.test_action_admission_policy import execute_policy
from tests.unit.runtime.test_direct_project_verification import context

SUBMISSION = 'python_dependency_preflight_submission_version'
POLICY = 'python_dependency_preflight_version'


def route() -> dict[str, Any]:
    return {SUBMISSION: 1, 'generated_project_contract_version': 1,
        'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle',
        'sandbox_profile': 'workspace_write', 'permission_mode': 'default',
        'execution_backend': 'systemd',
        'requested_permissions': ['workspace.read', 'workspace.write', 'command.run']}


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_all_fresh_modes_select_preflight_without_permission_growth(mode: TaskMode) -> None:
    original = route()
    baseline, _ = await execute_policy({k:v for k,v in original.items() if k != SUBMISSION}, mode=mode)
    for selected in await execute_policy(original, mode=mode):
        assert type(selected.get(POLICY)) is int and selected[POLICY] == 1
        assert {k:v for k,v in selected.items() if k not in {SUBMISSION, POLICY}} == baseline


@pytest.mark.parametrize('value', [None, True, False, '1', 1.0, 0, 2])
async def test_noninteger_submission_never_selects(value: Any) -> None:
    selected_route = route() | {SUBMISSION:value}
    for selected in await execute_policy(selected_route, mode=TaskMode.DIRECT):
        assert POLICY not in selected


@pytest.mark.parametrize('checkpoint,recovery', [(True,False),(False,True),(True,True)])
async def test_old_checkpoint_and_recovery_never_upgrade(checkpoint: bool, recovery: bool) -> None:
    for selected in await execute_policy(route(), mode=TaskMode.DIRECT,
                                         checkpoint=checkpoint, recovery=recovery):
        assert POLICY not in selected


@pytest.mark.parametrize('field,value', [
    ('generated_project_contract_version', True), ('generated_project_contract_version', None),
    ('permission_mode','read_only'), ('permission_mode','full_access'),
    ('sandbox_profile','read_only'), ('execution_backend','docker'),
    ('requested_permissions',['workspace.read']), ('project_input_preparation',{}),
    ('generated_project_preparation',{}), ('engineering_action_version',1),
])
async def test_nonfresh_or_wrong_authority_keeps_old_policy(field: str, value: Any) -> None:
    for selected in await execute_policy(route() | {field:value}, mode=TaskMode.DIRECT):
        assert POLICY not in selected


@pytest.mark.parametrize('value', [None, True, '1', 0, 1, 2])
async def test_existing_policy_presence_and_type_are_preserved(value: Any) -> None:
    for selected in await execute_policy(route() | {POLICY:value}, mode=TaskMode.DIRECT):
        assert type(selected[POLICY]) is type(value) and selected[POLICY] == value


def test_direct_checkpoint_binds_preflight_without_changing_legacy_scope() -> None:
    legacy = context(routing_decision={'generated_project_contract_version':1})
    current = legacy.model_copy(update={'routing_decision':dict(legacy.routing_decision) | {POLICY:1}})
    assert _binding(legacy, 'main') != _binding(current, 'main')
