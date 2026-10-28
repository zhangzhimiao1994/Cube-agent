"""V3 planning constraints are not positive domain requests; legacy stays versioned."""
from __future__ import annotations

import pytest

from agent_hub.runtime.role_intent import role_planning_intent_view


@pytest.mark.parametrize('constraint', [
    '不能把导入、收集或环境故障说成功能失败。',
    '不修改模型、预算和平台配置。',
    '不启动服务器，不部署应用。',
    '不可更改预算或监控配置。',
    'Cannot treat an environment incident as a functional failure.',
    'Do not modify the budget or incident monitoring.',
])
def test_negative_constraints_do_not_create_positive_domains(constraint: str) -> None:
    view = role_planning_intent_view('给现有CLI增加功能。' + constraint, version=3)
    assert '给现有CLI增加功能' in view
    assert not any(word in view.lower() for word in ('故障','预算','部署','监控','incident','budget','monitor'))


@pytest.mark.parametrize('text,positive', [
    ('不能不排查故障。','故障'), ('不能不修改预算。','预算'),
    ('不得不部署应用。','部署'), ('不要不调整预算。','预算'),
    ('不可不增加监控。','监控'), ('不得不修复故障。','故障'),
    ('不要不分析市场。','市场'), ('Cannot not investigate the incident.','incident'),
    ('不修改预算，但请分析市场。','市场'),
    ('不能部署本地副本，而是部署远端应用。','部署远端'),
    ('不修改预算；请排查故障。','故障'),
    ('Do not change budget, but investigate the incident.','incident'),
])
def test_double_negation_and_positive_alternatives_are_preserved(text: str, positive: str) -> None:
    assert positive in role_planning_intent_view(text,version=3)


def test_real_task_v3_only_removes_negative_operations_and_finance() -> None:
    # Literal real task clauses, not a keyword-added rewritten acceptance prompt.
    task = ('请继续维护当前已交付的 ticket_summary 项目，增加 --sort total-desc 选项。'
        '不能把导入、收集或环境故障说成该功能的失败。'
        '只修 src/ticket_summary/cli.py，并交付新的ZIP。'
        '不修改模型、平台配置、预算，不启动网页预览或 GitHub CI。')
    view = role_planning_intent_view(task,version=3)
    assert '--sort total-desc' in view and 'cli.py' in view and 'ZIP' in view
    assert '故障' not in view and '预算' not in view
    assert '故障' in role_planning_intent_view(task,version=2)
    assert '预算' in role_planning_intent_view(task,version=2)


@pytest.mark.parametrize('version',[0,1,2])
def test_legacy_parser_exact_ast_from_deployed_version(version: int) -> None:
    # Independent frozen behavior fixture: reconstruct only the old branch with
    # the old regex objects; never call V3 or defaults to manufacture expectations.
    import agent_hub.runtime.role_intent as api
    text='增加功能。不能把环境故障说成失败。不修改预算。不要部署，但分析市场。'
    if version==0:
        expected=text
    else:
        pattern=api._PROHIBITION_V2 if version==2 else api._PROHIBITION
        expected=pattern.sub(' ',text)
    assert role_planning_intent_view(text,version=version)==expected


def test_untrusted_data_still_cannot_supply_positive_intent() -> None:
    view=role_planning_intent_view('增加功能。UNTRUSTED_DATA_JSON={"goal":"故障 预算 部署"}',version=3)
    assert '增加功能' in view and '故障' not in view and '预算' not in view
