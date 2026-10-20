"""Typed native-decoder plumbing only; fixture bytes do not prove execution."""

import importlib
from typing import Any
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.skills.sandbox.base import SkillResult
from tests.unit.capabilities.test_python_project_reports import decode, fail, fixture
from tests.unit.harness.test_tool_gateway import FakeRuntimeCapabilityGateway


def failure() -> Any:
    api = importlib.import_module('agent_hub.capabilities.python_project_reports')
    context = fixture()
    fail(context[3], 2, stderr='original tests: normalization failed', stderr_observed_bytes=36)
    context[3]['checks'][-1]['stderr_observed_bytes'] = len(context[3]['checks'][-1]['stderr'].encode())
    return decode(api, context)


def test_runtime_keeps_python_failure_separate_from_node_and_binds_native_preview() -> None:
    observed = failure()
    error = RuntimeCapabilityError('project test failed', execution_diagnostic=observed.diagnostic,
        python_project_failure=observed)
    assert error.project_failure is None and error.python_project_failure is observed
    assert error.execution_diagnostic is observed.diagnostic


@pytest.mark.parametrize('preview', [None, object()])
def test_python_error_cannot_replace_a_native_preview_with_untrusted_data(preview: object) -> None:
    with pytest.raises(TypeError):
        RuntimeCapabilityError('failed', execution_diagnostic=preview, python_project_failure=failure())


def test_unrelated_real_diagnostic_cannot_be_substituted() -> None:
    preview = ExecutionDiagnostic('systemd', SkillResult(exit_code=1, timed_out=False, stdout='', stderr='other'))
    with pytest.raises(TypeError):
        RuntimeCapabilityError('failed', execution_diagnostic=preview, python_project_failure=failure())


@pytest.mark.asyncio
async def test_gateway_delivers_safe_python_failure_and_actual_error_preview_to_agent() -> None:
    observed = failure()
    class Backend(FakeRuntimeCapabilityGateway):
        async def execute(self, **kwargs: Any) -> Any:
            raise RuntimeCapabilityError('project test failed', execution_diagnostic=observed.diagnostic,
                python_project_failure=observed)
    gateway = HarnessToolGateway(Backend())
    request = HarnessToolCallRequest(run_id=UUID(int=2), actor='main_agent', tool_name='project.verify',
        arguments={}, approval_required=False, sandbox='workspace_write', idempotency_key='verify')
    result = await gateway.invoke(UUID(int=1), request, user_id=UUID(int=3), role=Role.OPERATOR)
    assert result.status == 'failed'
    assert 'normalization failed' in result.payload['execution_diagnostic']['stderr']
    assert result.payload['python_verification_failure']['schema_version'] == 2
    assert result.payload['python_environment_reference'] == observed.environment_reference.model_dump(mode='json')
    assert 'verification_failure' not in result.payload
