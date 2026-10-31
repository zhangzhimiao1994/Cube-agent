"""Static engineering admission owns a scope while granting no Root action."""
from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.python_resolution_authorization import resolution_policy_allowed
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.python_registry_protocol import digest


class StaticPolicy:
    def __init__(self, deny: bool = False) -> None:
        self.deny = deny
        self.static_calls = 0
        self.concrete_calls = 0

    async def authorize_static(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        assert role is Role.OPERATOR
        self.static_calls += 1
        return CapabilityResult(CapabilityStatus.DENIED if self.deny else CapabilityStatus.ALLOWED,
            request.run_id, reason="capability denied" if self.deny else None)

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        self.concrete_calls += 1
        return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason="capability denied")


class StaticBackend:
    def __init__(self, exercise_action: bool) -> None:
        self.exercise_action = exercise_action
        self.projections = 0
        self.executions = 0
        self.root_actions = 0

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        return name == "project.verify"

    async def project_verification_static_authorization(self, request: CapabilityRequest) -> None:
        self.projections += 1
        if self.exercise_action:
            arguments: dict[str, object] = {"purpose": "python_dependency_resolution", "key": "a" * 64,
                "source_sha256": "b" * 64, "root_sha256": "c" * 64, "catalog_sha256": "d" * 64,
                "permissions_sha256": "e" * 64, "original_deadline": "2099-01-01T00:00:00+00:00",
                "registry": "https://pypi.org", "artifact_host": "files.pythonhosted.org", "install_allowed": False,
                "requirements": ["click>=8"], "max_wheel_bytes": 64 * 1024**2,
                "environment": {"implementation_name": "cpython", "implementation_version": "3.12.14",
                    "os_name": "posix", "platform_machine": "x86_64", "platform_release": "6.1",
                    "platform_system": "Linux", "platform_version": "native-test",
                    "platform_python_implementation": "CPython", "python_full_version": "3.12.14",
                    "python_version": "3.12", "sys_platform": "linux"}}
            action = request.model_copy(update={"arguments": arguments,
                "resource": "workspace/current/project-resolution/python/" + digest(arguments),
                "idempotency_key": "resolution-" + "a" * 64})
            allowed = await resolution_policy_allowed(action)
            if allowed is True:
                self.root_actions += 1
            assert allowed is False

    async def execute(self, *, tenant_id: UUID, run_id: UUID, actor: str, name: str,
        arguments: Mapping[str, JsonValue], idempotency_key: str) -> Mapping[str, JsonValue]:
        self.executions += 1
        raise AssertionError("Static authorization and denied actions cannot execute a backend")


@pytest.mark.parametrize("exercise_action", [False, True], ids=["projection_only", "root_action_blocked"])
@pytest.mark.parametrize("deny", [False, True], ids=["static_allowed", "static_denied"])
async def test_static_project_projection_has_owned_policy_scope_and_never_grants_execution(
    exercise_action: bool, deny: bool,
) -> None:
    tenant, actor = uuid4(), uuid4()
    backend, policy = StaticBackend(exercise_action), StaticPolicy(deny)
    harness = HarnessToolGateway(backend, policy_gateway=policy)
    request = HarnessToolCallRequest(run_id=uuid4(), actor="engineer", tool_name="project.verify",
        arguments={}, sandbox="workspace_write", approval_required=False, idempotency_key="static-projection")
    result = await harness.authorize(tenant, request, user_id=actor, role=Role.OPERATOR)
    assert result.status == ("failed" if deny else "succeeded")
    assert result.failure_reason == ("capability denied" if deny else None)
    assert "approval_id" not in result.payload
    assert backend.projections == policy.static_calls == 1
    assert policy.concrete_calls == backend.executions == backend.root_actions == 0
    # An earlier successful static result cannot satisfy the later actual policy check.
    concrete = await harness.invoke(tenant, request, user_id=actor, role=Role.OPERATOR)
    assert concrete.status == "failed" and concrete.failure_reason == "capability denied"
    assert policy.concrete_calls == 1 and backend.executions == backend.root_actions == 0
