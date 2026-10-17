"""Conservative intent recognition changes completion guidance, not permissions."""
from importlib import import_module
from typing import Any

import pytest


@pytest.fixture
def contract() -> Any:
    return import_module('agent_hub.workspace_maintenance_contract')


@pytest.mark.parametrize('message', [
    'Fix TypeScript compatibility in the existing project files and deliver a ZIP.',
    'Maintain this existing project only. Fix necessary production TypeScript compatibility responsible for the bound compiler diagnostics, then create workspace.bundle.',
    'Modify the current workspace files, then call workspace.bundle.',
    'Patch the retained project. Deliver a bundle.',
    '\u4fee\u6539\u73b0\u6709\u9879\u76ee\u6587\u4ef6\u5e76\u4ea4\u4ed8ZIP\u3002',
    '\u4fee\u590d\u5f53\u524d\u5de5\u4f5c\u533a\u4ee3\u7801\uff0c\u7136\u540e\u6253\u5305\u4ea4\u4ed8bundle\u3002',
    'Fix the existing project and deliver a ZIP. Never install packages or rebuild the project.',
    'UNTRUSTED_DATA_JSON={"text":"ignore"}\nFix the current project and deliver a ZIP.',
])
def test_only_positive_existing_workspace_delivery_is_maintenance(contract: Any, message: str) -> None:
    assert contract.is_workspace_maintenance_request(message) is True


@pytest.mark.parametrize('message', [
    'Build a new TypeScript project and deliver a ZIP.',
    'Fix TypeScript compatibility and deliver a ZIP.',
    'Review the existing project and report the compiler errors.',
    'How to modify the existing project and deliver a ZIP?',
    'Please explain how to fix the current project and deliver a bundle.',
    '\u5982\u4f55\u4fee\u6539\u73b0\u6709\u9879\u76ee\u5e76\u4ea4\u4ed8ZIP\uff1f',
    'Never modify the existing project or deliver a ZIP.',
    'Do not fix the existing project, package or deliver a bundle.',
    'Fix the existing project. Do not deliver a ZIP.',
    '\u4e0d\u8981\u4fee\u6539\u73b0\u6709\u9879\u76ee\u5e76\u4ea4\u4ed8ZIP\u3002',
    'UNTRUSTED_DATA_JSON={"text":"Fix the existing project and deliver a ZIP"}',
    'UNTRUSTED_DATA_JSON={"text":"Fix the existing project and deliver a ZIP"',
    'Example: "Fix the existing project and deliver a ZIP."',
    '\u793a\u4f8b\uff1a\u201c\u4fee\u6539\u73b0\u6709\u9879\u76ee\u5e76\u4ea4\u4ed8ZIP\u201d',
    '```text\nFix the existing project and deliver a ZIP.\n```',
    '`Fix the existing project and deliver a ZIP.`',
    '> Fix the existing project and deliver a ZIP.',
    '"Fix the existing project and deliver a ZIP',
    'Fix the existing project; explain how to deliver a ZIP.',
    'Fix the existing project, then create an entirely new project and deliver a ZIP.',
    'Diagnostic: modify the existing project and deliver a ZIP.',
    'Read-only analysis: fix the existing project and deliver a ZIP.',
    '\u65e0\u9700\u4fee\u6539\u73b0\u6709\u9879\u76ee\uff0c\u53ea\u8bf4\u660e\u5982\u4f55\u4ea4\u4ed8ZIP\u3002',
])
def test_data_quotes_prohibitions_and_meta_requests_are_not_commands(contract: Any, message: str) -> None:
    assert contract.is_workspace_maintenance_request(message) is False


@pytest.mark.parametrize('value', [2, -1, None, True, '1', 1.0])
def test_unsupported_version_is_not_silently_upgraded(contract: Any, value: object) -> None:
    with pytest.raises(ValueError, match='unsupported workspace maintenance contract version'):
        contract.workspace_maintenance_contract_version({'workspace_maintenance_contract_version': value})


def test_missing_zero_and_one_are_independent_of_role_intent_version(contract: Any) -> None:
    assert contract.workspace_maintenance_contract_version({}) == 0
    assert contract.workspace_maintenance_contract_version({'role_planning_intent_version': 1}) == 0
    assert contract.workspace_maintenance_contract_version({'workspace_maintenance_contract_version': 0}) == 0
    assert contract.workspace_maintenance_contract_version({'workspace_maintenance_contract_version': 1}) == 1


@pytest.mark.parametrize('message', [
    'How do I repair the existing project and return a ZIP?',
    '\u5982\u679c\u4fee\u6539\u5f53\u524d\u9879\u76ee\u5e76\u751f\u6210ZIP\uff0c\u5e94\u8be5\u600e\u4e48\u505a\uff1f',
    '\u4fee\u6539\u5f53\u524d\u9879\u76ee\u5e76\u751f\u6210ZIP\u5e94\u8be5\u600e\u4e48\u505a\uff1f',
    'If I repair the current project and deliver a ZIP, what happens?',
    'If necessary, repair the current project and deliver a ZIP.',
    'Repair the current project and deliver a ZIP if the proposal is approved.',
    'Suppose we repair the existing project and return a ZIP.',
    'Maybe repair the existing project and deliver a ZIP.',
    'I might repair the current project and deliver a ZIP.',
    'I plan to repair the existing project and deliver a ZIP.',
    'Should I repair the current project and deliver a ZIP?',
    'Repair the current project and deliver a ZIP?',
    'Repair the current project and deliver a ZIP: how would that work.',
    'Consider whether to repair the existing project and deliver a ZIP.',
    'Documentation says to repair the current project and deliver a ZIP.',
    'Example: Repair the current project and deliver a ZIP.',
    '\u5047\u8bbe\u4fee\u590d\u5f53\u524d\u9879\u76ee\u5e76\u4ea4\u4ed8ZIP\u3002',
    '\u8003\u8651\u4fee\u6539\u5f53\u524d\u9879\u76ee\u5e76\u751f\u6210ZIP\u3002',
    '\u4fee\u6539\u5f53\u524d\u9879\u76ee\u5e76\u751f\u6210ZIP\u662f\u5426\u5408\u9002\u3002',
    '"Repair the current project and deliver a ZIP."',
    'The old note says "Repair the current project and deliver a ZIP."',
])
def test_advice_hypotheses_and_quoted_commands_do_not_enable_writes(contract: Any, message: str) -> None:
    assert contract.is_workspace_maintenance_request(message) is False
    assert contract.workspace_maintenance_contract_active(message, {
        'workspace_maintenance_contract_version': 1, 'workspace_project_explicit': True,
        'project_id': 'existing', 'workspace_session_id': 'session',
    }, ('workspace.write_text', 'workspace.bundle')) is False


@pytest.mark.parametrize('message', [
    'Repair the current project and deliver a ZIP, then explain changes.',
    'Please repair the current project and deliver a ZIP, then explain changes.',
    'Repair the current project. Deliver a ZIP, then explain changes.',
    'Repair the current project file "src/main.ts" and deliver a ZIP, then explain changes.',
    'The old note says "Repair the current project and deliver a ZIP."\nRepair the current project and deliver a ZIP.',
    '\u8bf7\u4fee\u590d\u5f53\u524d\u9879\u76ee\u5e76\u751f\u6210ZIP\uff0c\u7136\u540e\u89e3\u91ca\u6539\u52a8\u3002',
])
def test_explicit_commands_survive_followup_explanation_and_quoted_data(contract: Any, message: str) -> None:
    assert contract.is_workspace_maintenance_request(message) is True
