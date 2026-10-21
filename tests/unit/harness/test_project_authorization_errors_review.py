"""Only typed, enumerated resolver failures reach model-visible diagnostics."""

from __future__ import annotations

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.generated_project_service import GeneratedProjectError
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from tests.unit.harness.test_tool_gateway import (
    RUN_ID,
    TENANT_ID,
    USER_ID,
    FakePolicyGateway,
    FakeRuntimeCapabilityGateway,
)


@pytest.mark.parametrize('method', ['authorize', 'invoke'])
@pytest.mark.parametrize('kind,code,public', [
    (ProjectVerificationError, 'python_project_recipe_invalid', True),
    (ProjectVerificationError, 'python_project_lock_invalid', True),
    (ProjectVerificationError, 'python_project_wheel_missing', True),
    (ProjectVerificationError, 'python_project_cleanup_incomplete', True),
    (ProjectVerificationError, 'input_supply_catalog_unavailable', True),
    (ProjectVerificationError, 'generated_python_project_unavailable', True),
    (GeneratedProjectError, 'generated_acceptance_scaffold_incomplete', True),
    (ProjectVerificationError, 'python_project_PRIVATE_SECRET', False),
    (GeneratedProjectError, 'generated_python_private_secret', False),
    (ProjectVerificationError, 'python_project_recipe_invalid\nPRIVATE', False),
    (ProjectVerificationError, '/private/host/credentials', False),
    (RuntimeError, 'python_project_recipe_invalid', False),
])
async def test_resolver_failure_is_typed_and_allowlisted_without_policy_or_execution(
    method: str, kind: type[Exception], code: str, public: bool,
) -> None:
    class Backend(FakeRuntimeCapabilityGateway):
        async def project_verification_authorization(self, request: CapabilityRequest) -> None:
            raise kind(code)

        async def project_verification_static_authorization(self, request: CapabilityRequest) -> None:
            raise kind(code)

    backend, policy = Backend(), FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    call = HarnessToolCallRequest(run_id=RUN_ID, actor='engineer', tool_name='project.verify',
        arguments={}, sandbox='workspace_write', approval_required=False, idempotency_key='review-errors')
    result = await getattr(gateway, method)(TENANT_ID, call, user_id=USER_ID, role=Role.OPERATOR)
    assert result.status == 'failed'
    assert result.failure_reason == (code if public else 'capability authorization unavailable')
    assert result.payload == {} and not policy.requests
    assert not any(item[0] == 'execute' for item in backend.calls)
