"""Build classification distinguishes commands from planning and quoted data."""
import pytest

from agent_hub.runs.service import _project_delivery_assessment


@pytest.mark.parametrize('message', [
    '请只规划构建一个 Python CLI 项目，不要执行',
    '"构建一个 Python CLI 项目"',
    '> 构建一个 Python CLI 项目',
    '如何开发当前项目？',
    'How do I build a Python CLI project?',
])
def test_planning_and_quoted_builds_do_not_request_workspace_delivery(message: str) -> None:
    assert _project_delivery_assessment(message) is None


@pytest.mark.parametrize('message', [
    '请在本次会话内自主完成一个可安装的真实 Python CLI 项目，构建 wheel 并实际验证后交付 ZIP。',
    '请构建名为“Expense CLI”的 Python 项目并交付可安装的 wheel。',
    '请构建一个 Python CLI 项目，但不要执行部署或启动预览服务。',
    '能不能帮我开发一个 Python CLI 项目？',
])
def test_active_build_retains_delivery_with_context_or_quoted_product_name(message: str) -> None:
    assessment = _project_delivery_assessment(message)
    assert assessment is not None
    assert assessment['project_delivery'] == 'workspace'
    assert assessment['artifact_strategy'] == 'workspace_bundle'
