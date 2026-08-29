from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult, JsonValue

type MutableJson = None | bool | int | float | str | list["MutableJson"] | dict[str, "MutableJson"]


class RuntimeToolBackend(Protocol):
    def is_available(self, tenant_id: UUID, name: str) -> bool: ...

    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]: ...


class HarnessCapabilityPolicyGateway(Protocol):
    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult: ...


class HarnessToolGateway:
    """Bridge harness tool envelopes to the approved runtime capability backend."""

    def __init__(
        self,
        backend: RuntimeToolBackend,
        *,
        policy_gateway: HarnessCapabilityPolicyGateway | None = None,
        raise_backend_errors: bool = False,
    ) -> None:
        self._backend = backend
        self._policy_gateway = policy_gateway
        self._raise_backend_errors = raise_backend_errors

    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        if not isinstance(request, HarnessToolCallRequest):
            raise TypeError("request must be HarnessToolCallRequest")
        if request.approval_required and self._policy_gateway is None:
            return self._failure(request, "approval required")
        authorization_failure = await self._authorize(
            tenant_id,
            request,
            user_id=user_id,
            role=role,
        )
        if authorization_failure is not None:
            return authorization_failure
        try:
            if not self._backend.is_available(tenant_id, request.tool_name):
                return self._failure(request, "tool unavailable")
            payload = await self._backend.execute(
                tenant_id=tenant_id,
                run_id=request.run_id,
                actor=request.actor,
                name=request.tool_name,
                arguments=request.arguments,
                idempotency_key=request.idempotency_key,
            )
        except Exception as error:
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            if self._raise_backend_errors:
                raise
            return self._failure(request, "tool execution failed")
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="succeeded",
            payload=payload,
        )

    async def _authorize(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None,
        role: Role | None,
    ) -> HarnessToolCallResult | None:
        if self._policy_gateway is None:
            return None
        if type(user_id) is not UUID or not isinstance(role, Role):
            raise TypeError("user_id and role are required for policy-gated harness tools")
        decision = await self._policy_gateway.invoke(
            _capability_request(tenant_id, user_id, request),
            role=role,
        )
        if decision.status is CapabilityStatus.ALLOWED:
            return None
        payload: Mapping[str, JsonValue] = {}
        if decision.status is CapabilityStatus.WAITING_APPROVAL and decision.approval_id is not None:
            payload = {"approval_id": decision.approval_id}
        return self._failure(
            request,
            decision.reason or _capability_failure_reason(decision.status),
            payload=payload,
        )

    def _failure(
        self,
        request: HarnessToolCallRequest,
        reason: str,
        *,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> HarnessToolCallResult:
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="failed",
            payload={} if payload is None else payload,
            failure_reason=reason,
        )


def _capability_request(
    tenant_id: UUID,
    user_id: UUID,
    request: HarnessToolCallRequest,
) -> CapabilityRequest:
    capability, operation, resource = _capability_parts(request)
    return CapabilityRequest(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=request.actor,
        capability=capability,
        operation=operation,
        resource=resource,
        arguments=dict(_mutable_json_object(request.arguments)),
        idempotency_key=request.idempotency_key,
        run_id=request.run_id,
    )


def _capability_parts(request: HarnessToolCallRequest) -> tuple[str, str, str]:
    if request.tool_name in {"calculator", "calculator_evaluate", "calculator.evaluate"}:
        return "calculator", "evaluate", "calculator"
    if request.tool_name in {"workspace_read", "workspace.read"}:
        path = request.arguments.get("path")
        return "file", "read", path if isinstance(path, str) else "workspace"
    if request.tool_name == "read_context":
        path = request.arguments.get("path")
        if isinstance(path, str):
            return "file", "read", path
        return "context", "read", "context"
    return "skill", "use", f"skill/{request.tool_name}"


def _capability_failure_reason(status: CapabilityStatus) -> str:
    if status is CapabilityStatus.WAITING_APPROVAL:
        return "capability requires approval"
    return "capability denied"


def _mutable_json_object(value: Mapping[str, JsonValue]) -> dict[str, MutableJson]:
    return {key: _mutable_json(item) for key, item in value.items()}


def _mutable_json(value: object) -> MutableJson:
    if value is None or type(value) is bool or type(value) is int or type(value) is float:
        return value
    if type(value) is str:
        return value
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_mutable_json(item) for item in value]
    raise TypeError("value is not JSON-compatible")


__all__ = [
    "HarnessCapabilityPolicyGateway",
    "HarnessToolGateway",
    "RuntimeToolBackend",
]
