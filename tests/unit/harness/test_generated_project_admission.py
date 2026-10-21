from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.runtime.contracts import JsonValue
from tests.unit.harness.test_tool_gateway import (
    RUN_ID,
    TENANT_ID,
    USER_ID,
    FakePolicyGateway,
    FakeRuntimeCapabilityGateway,
)


class GeneratedBackend(FakeRuntimeCapabilityGateway):
    def __init__(self) -> None:
        super().__init__()
        self.static_calls = 0
        self.preparation_calls = 0

    async def project_verification_static_authorization(self, request: CapabilityRequest) -> None:
        self.static_calls += 1

    async def project_verification_authorization(self, request: CapabilityRequest) -> None:
        self.preparation_calls += 1


async def test_static_engineering_admission_never_captures_generated_scaffold() -> None:
    backend = GeneratedBackend()
    gateway = HarnessToolGateway(backend, policy_gateway=FakePolicyGateway(CapabilityStatus.ALLOWED))
    request = HarnessToolCallRequest(
        run_id=RUN_ID, actor='main_agent', tool_name='project.verify', arguments={},
        sandbox='workspace_write', approval_required=False, idempotency_key='generated-admission',
    )
    admitted = await gateway.authorize(TENANT_ID, request, user_id=USER_ID, role=Role.OPERATOR)
    assert admitted.status == 'succeeded'
    assert backend.static_calls == 1
    assert backend.preparation_calls == 0
    assert not any(call[0] == 'execute' for call in backend.calls)
    executed = await gateway.invoke(TENANT_ID, request, user_id=USER_ID, role=Role.OPERATOR)
    assert executed.status == 'succeeded'
    assert backend.preparation_calls == 1


async def test_supply_completion_requests_distinct_recipe_without_reexecution() -> None:
    from agent_hub.capabilities.python_generated_service import (
        PythonGeneratedRecipeApprovalRequired,
    )

    recipe = CapabilityRequest(tenant_id=TENANT_ID, user_id=USER_ID, run_id=RUN_ID,
        agent_id='main_agent', capability='command', operation='run',
        resource='workspace/current/project-verification/python/' + 'a' * 64,
        arguments={'schema_version': 1, 'recipe_sha256': 'a' * 64, 'environment_input_sha256': 'b' * 64,
                   'project_id': 'project', 'workspace_session_id': 'session'}, idempotency_key='internal')

    class Backend(GeneratedBackend):
        async def execute(self, **kw: Any) -> Mapping[str, JsonValue]:
            self.calls.append(('execute', {}, ''))
            raise PythonGeneratedRecipeApprovalRequired(recipe)

    class Policy(FakePolicyGateway):
        async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
            self.requests.append((request, role))
            return CapabilityResult(CapabilityStatus.ALLOWED if len(self.requests) == 1
                                    else CapabilityStatus.WAITING_APPROVAL, request.run_id,
                                    approval_id=None if len(self.requests) == 1 else 'recipe-approval')

    backend, policy = Backend(), Policy(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    call = HarnessToolCallRequest(run_id=RUN_ID, actor='main_agent', tool_name='project.verify',
        arguments={}, sandbox='workspace_write', approval_required=False, idempotency_key='verify-call')
    result = await gateway.invoke(TENANT_ID, call, user_id=USER_ID, role=Role.OPERATOR)
    assert result.status == 'failed' and result.payload == {'approval_id': 'recipe-approval'}
    assert len([call for call in backend.calls if call[0] == 'execute']) == 1
    assert len(policy.requests) == 2
    assert policy.requests[-1][0].resource == recipe.resource
