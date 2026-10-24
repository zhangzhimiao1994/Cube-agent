"""Only trusted scoped recipe data may specialize a project installation grant."""

from typing import Any
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from tests.unit.harness.test_tool_gateway import FakePolicyGateway, FakeRuntimeCapabilityGateway


class RecipeBackend(FakeRuntimeCapabilityGateway):
    def __init__(
        self, changes: dict[str, Any] | None = None, *, error: bool = False, node: bool = False
    ) -> None:
        super().__init__()
        self.changes, self.error, self.node = changes or {}, error, node
        self.resolutions: list[CapabilityRequest] = []

    async def project_verification_authorization(
        self, request: CapabilityRequest
    ) -> CapabilityRequest | None:
        self.resolutions.append(request)
        if self.error:
            raise RuntimeError("private source or host path must not leak")
        if self.node:
            return None
        fields = request.model_dump() | {
            "resource": "workspace/current/project-verification/python/" + "a" * 64,
            "arguments": {
                "schema_version": 1,
                "recipe_sha256": "a" * 64,
                "environment_input_sha256": "b" * 64,
                "project_id": "billing",
                "workspace_session_id": "one",
            },
        }
        return CapabilityRequest.model_validate(fields | self.changes, strict=True)


async def authorize(backend: Any) -> tuple[Any, FakePolicyGateway]:
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    call = HarnessToolCallRequest(
        run_id=UUID(int=2),
        actor="main_agent",
        tool_name="project.verify",
        arguments={},
        approval_required=False,
        sandbox="workspace_write",
        idempotency_key="approval",
    )
    response = await gateway.authorize(UUID(int=1), call, user_id=UUID(int=3), role=Role.OPERATOR)
    return response, policy


@pytest.mark.asyncio
async def test_recipe_specific_resource_and_arguments_reach_policy_without_execution() -> None:
    backend = RecipeBackend()
    result, policy = await authorize(backend)
    assert result.status == "succeeded"
    assert (
        policy.requests[0][0].resource
        == "workspace/current/project-verification/python/" + "a" * 64
    )
    assert policy.requests[0][0].arguments["environment_input_sha256"] == "b" * 64
    assert len(backend.resolutions) == 1
    assert not any(item[0] == "execute" for item in backend.calls)


@pytest.mark.asyncio
async def test_explicit_node_result_preserves_exact_legacy_policy_request() -> None:
    result, policy = await authorize(RecipeBackend(node=True))
    assert result.status == "succeeded"
    assert policy.requests[0][0].resource == "workspace/current/project-verification"
    assert policy.requests[0][0].arguments == {}


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": UUID(int=9)},
        {"run_id": UUID(int=9)},
        {"user_id": UUID(int=9)},
        {"agent_id": "different"},
        {"capability": "network"},
        {"operation": "install"},
        {"resource": "workspace/current/project-verification"},
        {"idempotency_key": "different"},
        {"arguments": {"schema_version": 1}},
    ],
)
@pytest.mark.asyncio
async def test_mismatched_or_generic_recipe_selection_cannot_fall_back_to_legacy_grant(
    change: dict[str, Any],
) -> None:
    result, policy = await authorize(RecipeBackend(change))
    assert result.status == "failed" and policy.requests == []


@pytest.mark.asyncio
async def test_resolution_error_does_not_request_a_generic_installation_approval() -> None:
    result, policy = await authorize(RecipeBackend(error=True))
    assert result.status == "failed" and policy.requests == []
    assert "private" not in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_model_arguments_cannot_trigger_scope_discovery_or_an_install_grant() -> None:
    backend, policy = RecipeBackend(), FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    request = HarnessToolCallRequest(
        run_id=UUID(int=2),
        actor="main_agent",
        tool_name="project.verify",
        arguments={"cwd": "/another/project"},
        approval_required=False,
        sandbox="workspace_write",
        idempotency_key="invalid-scope",
    )
    result = await gateway.authorize(UUID(int=1), request, user_id=UUID(int=3), role=Role.OPERATOR)
    assert result.status == "failed" and backend.resolutions == [] and policy.requests == []


@pytest.mark.parametrize('backend', [RecipeBackend(error=True), RecipeBackend({'run_id':UUID(int=9)})])
@pytest.mark.asyncio
async def test_public_invoke_contains_resolution_failure_without_generic_authority(backend: Any) -> None:
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    call = HarnessToolCallRequest(run_id=UUID(int=2), actor='main_agent',
        tool_name='project.verify', arguments={}, approval_required=False,
        sandbox='workspace_write', idempotency_key='failed-resolution')
    result = await gateway.invoke(UUID(int=1), call, user_id=UUID(int=3), role=Role.OPERATOR)
    assert result.status == 'failed' and not policy.requests
    assert result.failure_reason == 'capability authorization unavailable'
    assert 'private' not in repr(result)
    assert not any(item[0] == 'execute' for item in backend.calls)
