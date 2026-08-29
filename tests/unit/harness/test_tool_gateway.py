from collections.abc import Mapping
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, JsonValue

TENANT_ID = UUID("44444444-4444-4444-8444-444444444444")
USER_ID = UUID("66666666-6666-4666-8666-666666666666")
RUN_ID = UUID("55555555-5555-4555-8555-555555555555")


class FakeRuntimeCapabilityGateway:
    def __init__(self, *, available: bool = True, fail: bool = False) -> None:
        self.available = available
        self.fail = fail
        self.calls: list[tuple[str, Mapping[str, object], str]] = []

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        self.calls.append(("available", {"tenant_id": str(tenant_id), "name": name}, ""))
        return self.available

    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        self.calls.append(
            (
                "execute",
                {
                    "tenant_id": str(tenant_id),
                    "run_id": str(run_id),
                    "actor": actor,
                    "name": name,
                    "arguments": arguments,
                },
                idempotency_key,
            )
        )
        if self.fail:
            raise RuntimeError("raw provider filesystem failure")
        return {"value": "14"}


class FakePolicyGateway:
    def __init__(
        self,
        status: CapabilityStatus,
        *,
        reason: str | None = None,
        approval_id: str | None = None,
    ) -> None:
        self.status = status
        self.reason = reason
        self.approval_id = approval_id
        self.requests: list[tuple[CapabilityRequest, Role]] = []

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        self.requests.append((request, role))
        return CapabilityResult(
            self.status,
            request.run_id,
            approval_id=self.approval_id,
            reason=self.reason,
        )


def request(*, approval_required: bool = False) -> HarnessToolCallRequest:
    return HarnessToolCallRequest(
        run_id=RUN_ID,
        actor="main_agent",
        tool_name="calculator",
        arguments={"expression": "2+3*4"},
        approval_required=approval_required,
        sandbox="restricted",
        idempotency_key="calc_1",
    )


def search_request() -> HarnessToolCallRequest:
    return HarnessToolCallRequest(
        run_id=RUN_ID,
        actor="main_agent",
        tool_name="read_context",
        arguments={"tags": ("alpha", "beta")},
        approval_required=False,
        sandbox="restricted",
        idempotency_key="context_1",
    )


def read_context_file_request() -> HarnessToolCallRequest:
    return HarnessToolCallRequest(
        run_id=RUN_ID,
        actor="main_agent",
        tool_name="read_context",
        arguments={"path": "workspace/docs/a.md"},
        approval_required=False,
        sandbox="restricted",
        idempotency_key="context_file_1",
    )


def workspace_dot_file_request() -> HarnessToolCallRequest:
    return HarnessToolCallRequest(
        run_id=RUN_ID,
        actor="main_agent",
        tool_name="workspace.read",
        arguments={"path": "workspace/docs/a.md"},
        approval_required=False,
        sandbox="read_only",
        idempotency_key="workspace_dot_1",
    )


async def test_harness_tool_gateway_executes_approved_available_tools() -> None:
    runtime = FakeRuntimeCapabilityGateway()
    gateway = HarnessToolGateway(runtime)

    result = await gateway.invoke(TENANT_ID, request())

    assert result.status == "succeeded"
    assert result.tool_name == "calculator"
    assert result.payload == {"value": "14"}
    assert runtime.calls[-1][0] == "execute"
    assert runtime.calls[-1][2] == "calc_1"


def test_harness_capability_policy_gateway_is_publicly_exported() -> None:
    import agent_hub.harness as public_harness

    assert public_harness.HarnessCapabilityPolicyGateway is not None


async def test_harness_tool_gateway_policy_runs_even_when_envelope_marks_approval_required() -> None:
    runtime = FakeRuntimeCapabilityGateway()
    policy = FakePolicyGateway(
        CapabilityStatus.WAITING_APPROVAL,
        reason="capability requires approval",
        approval_id="approval_2",
    )
    gateway = HarnessToolGateway(runtime, policy_gateway=policy)

    result = await gateway.invoke(
        TENANT_ID,
        request(approval_required=True),
        user_id=USER_ID,
        role=Role.OPERATOR,
    )

    assert result.status == "failed"
    assert result.failure_reason == "capability requires approval"
    assert result.payload == {"approval_id": "approval_2"}
    assert len(policy.requests) == 1
    assert runtime.calls == []


async def test_harness_tool_gateway_converts_frozen_json_arguments_for_policy() -> None:
    runtime = FakeRuntimeCapabilityGateway()
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(runtime, policy_gateway=policy)

    result = await gateway.invoke(TENANT_ID, search_request(), user_id=USER_ID, role=Role.OPERATOR)

    assert result.status == "succeeded"
    capability_request = policy.requests[0][0]
    assert capability_request.arguments == {"tags": ["alpha", "beta"]}
    assert isinstance(capability_request.arguments["tags"], list)


async def test_harness_tool_gateway_maps_read_context_path_to_file_read_policy() -> None:
    runtime = FakeRuntimeCapabilityGateway()
    policy = FakePolicyGateway(CapabilityStatus.DENIED, reason="capability denied")
    gateway = HarnessToolGateway(runtime, policy_gateway=policy)

    result = await gateway.invoke(
        TENANT_ID,
        read_context_file_request(),
        user_id=USER_ID,
        role=Role.OPERATOR,
    )

    assert result.status == "failed"
    capability_request = policy.requests[0][0]
    assert capability_request.capability == "file"
    assert capability_request.operation == "read"
    assert capability_request.resource == "workspace/docs/a.md"
    assert runtime.calls == []


async def test_harness_tool_gateway_maps_workspace_dot_name_to_file_read_policy() -> None:
    runtime = FakeRuntimeCapabilityGateway()
    policy = FakePolicyGateway(CapabilityStatus.DENIED, reason="capability denied")
    gateway = HarnessToolGateway(runtime, policy_gateway=policy)

    result = await gateway.invoke(
        TENANT_ID,
        workspace_dot_file_request(),
        user_id=USER_ID,
        role=Role.OPERATOR,
    )

    assert result.status == "failed"
    capability_request = policy.requests[0][0]
    assert capability_request.capability == "file"
    assert capability_request.operation == "read"
    assert capability_request.resource == "workspace/docs/a.md"
    assert runtime.calls == []


async def test_harness_tool_gateway_denies_before_runtime_execute() -> None:
    runtime = FakeRuntimeCapabilityGateway()
    policy = FakePolicyGateway(CapabilityStatus.DENIED, reason="capability denied")
    gateway = HarnessToolGateway(runtime, policy_gateway=policy)

    result = await gateway.invoke(TENANT_ID, request(), user_id=USER_ID, role=Role.OPERATOR)

    assert result.status == "failed"
    assert result.failure_reason == "capability denied"
    assert runtime.calls == []
    capability_request, role = policy.requests[0]
    assert role is Role.OPERATOR
    assert capability_request.tenant_id == TENANT_ID
    assert capability_request.user_id == USER_ID
    assert capability_request.run_id == RUN_ID
    assert capability_request.agent_id == "main_agent"
    assert capability_request.capability == "calculator"
    assert capability_request.operation == "evaluate"
    assert capability_request.resource == "calculator"
    assert capability_request.arguments == {"expression": "2+3*4"}
    assert capability_request.idempotency_key == "calc_1"


async def test_harness_tool_gateway_waiting_approval_does_not_execute_backend() -> None:
    runtime = FakeRuntimeCapabilityGateway()
    policy = FakePolicyGateway(
        CapabilityStatus.WAITING_APPROVAL,
        reason="capability requires approval",
        approval_id="approval_1",
    )
    gateway = HarnessToolGateway(runtime, policy_gateway=policy)

    result = await gateway.invoke(
        TENANT_ID,
        request(approval_required=False),
        user_id=USER_ID,
        role=Role.OPERATOR,
    )

    assert result.status == "failed"
    assert result.failure_reason == "capability requires approval"
    assert result.payload == {"approval_id": "approval_1"}
    assert runtime.calls == []


async def test_harness_tool_gateway_allowed_invokes_backend_once_with_same_idempotency() -> None:
    runtime = FakeRuntimeCapabilityGateway()
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(runtime, policy_gateway=policy)

    result = await gateway.invoke(TENANT_ID, request(), user_id=USER_ID, role=Role.OPERATOR)

    assert result.status == "succeeded"
    assert policy.requests[0][0].idempotency_key == "calc_1"
    assert [call[0] for call in runtime.calls] == ["available", "execute"]
    assert runtime.calls[-1][2] == "calc_1"


async def test_harness_tool_gateway_does_not_execute_when_approval_is_pending() -> None:
    runtime = FakeRuntimeCapabilityGateway()
    gateway = HarnessToolGateway(runtime)

    result = await gateway.invoke(TENANT_ID, request(approval_required=True))

    assert result.status == "failed"
    assert result.failure_reason == "approval required"
    assert [call[0] for call in runtime.calls] == []


async def test_harness_tool_gateway_reports_unavailable_tools_without_execution() -> None:
    runtime = FakeRuntimeCapabilityGateway(available=False)
    gateway = HarnessToolGateway(runtime)

    result = await gateway.invoke(TENANT_ID, request())

    assert result.status == "failed"
    assert result.failure_reason == "tool unavailable"
    assert [call[0] for call in runtime.calls] == ["available"]


async def test_harness_tool_gateway_redacts_backend_failures() -> None:
    runtime = FakeRuntimeCapabilityGateway(fail=True)
    gateway = HarnessToolGateway(runtime)

    result = await gateway.invoke(TENANT_ID, request())

    assert result.status == "failed"
    assert result.failure_reason == "tool execution failed"
    assert "raw provider" not in repr(result)


async def test_harness_tool_gateway_can_preserve_backend_uncertainty_for_runtime() -> None:
    runtime = FakeRuntimeCapabilityGateway(fail=True)
    gateway = HarnessToolGateway(runtime, raise_backend_errors=True)

    try:
        await gateway.invoke(TENANT_ID, request())
    except RuntimeError as error:
        assert "raw provider" in str(error)
    else:
        raise AssertionError("backend uncertainty should be re-raised for runtime ledgers")
