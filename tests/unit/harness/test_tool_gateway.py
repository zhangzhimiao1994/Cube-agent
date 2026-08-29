from collections.abc import Mapping
from uuid import UUID

from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, JsonValue

TENANT_ID = UUID("44444444-4444-4444-8444-444444444444")
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


async def test_harness_tool_gateway_executes_approved_available_tools() -> None:
    runtime = FakeRuntimeCapabilityGateway()
    gateway = HarnessToolGateway(runtime)

    result = await gateway.invoke(TENANT_ID, request())

    assert result.status == "succeeded"
    assert result.tool_name == "calculator"
    assert result.payload == {"value": "14"}
    assert runtime.calls[-1][0] == "execute"
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
