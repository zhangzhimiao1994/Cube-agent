from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult, JsonValue


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


class HarnessToolGateway:
    """Bridge harness tool envelopes to the approved runtime capability backend."""

    def __init__(self, backend: RuntimeToolBackend) -> None:
        self._backend = backend

    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
    ) -> HarnessToolCallResult:
        if not isinstance(request, HarnessToolCallRequest):
            raise TypeError("request must be HarnessToolCallRequest")
        if request.approval_required:
            return self._failure(request, "approval required")
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
        except Exception as error:  # noqa: BLE001 - keep provider/tool details out of events
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            return self._failure(request, "tool execution failed")
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="succeeded",
            payload=payload,
        )

    def _failure(self, request: HarnessToolCallRequest, reason: str) -> HarnessToolCallResult:
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="failed",
            payload={},
            failure_reason=reason,
        )


__all__ = [
    "HarnessToolGateway",
    "RuntimeToolBackend",
]
