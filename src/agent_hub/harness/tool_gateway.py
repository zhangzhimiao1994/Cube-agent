from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult, JsonValue

type MutableJson = None | bool | int | float | str | list["MutableJson"] | dict[str, "MutableJson"]

_LOGGER = logging.getLogger(__name__)


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


class McpToolBackend(Protocol):
    def is_available(self, tenant_id: UUID, name: str) -> bool: ...

    async def invoke(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]: ...


class PluginToolBackend(Protocol):
    def is_available(self, tenant_id: UUID, name: str) -> bool: ...

    async def invoke(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]: ...


type CapabilityPolicyParts = tuple[str, str, str]


class HarnessCapabilityPolicyGateway(Protocol):
    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult: ...


class HarnessToolGateway:
    """Bridge harness tool envelopes to the approved runtime capability backend."""

    def __init__(
        self,
        backend: RuntimeToolBackend,
        *,
        policy_gateway: HarnessCapabilityPolicyGateway | None = None,
        mcp_backend: McpToolBackend | None = None,
        plugin_backend: PluginToolBackend | None = None,
        require_actor_identity: bool = False,
        raise_backend_errors: bool = False,
    ) -> None:
        self._backend = backend
        self._policy_gateway = policy_gateway
        self._mcp_backend = mcp_backend
        self._plugin_backend = plugin_backend
        self._require_actor_identity = require_actor_identity
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
        sandbox_failure = _workspace_write_sandbox_failure(request)
        if sandbox_failure is not None:
            return self._failure(request, sandbox_failure)
        if request.approval_required and self._policy_gateway is None:
            return self._failure(request, "approval required")
        uses_external_envelope = _uses_external_envelope(request)
        if uses_external_envelope:
            preparation_failure = await self._prepare_external_backends(
                tenant_id,
                backends=_external_backends_for_request(self, request),
            )
            if preparation_failure is not None:
                return self._failure(request, preparation_failure)
        mcp_backend = (
            self._available_mcp_backend(tenant_id, request.tool_name)
            if _may_route_to_mcp_backend(request)
            else None
        )
        plugin_backend = (
            self._available_plugin_backend(tenant_id, request.tool_name)
            if mcp_backend is None and _may_route_to_plugin_backend(request)
            else None
        )
        sandbox_mismatch = _external_sandbox_mismatch(
            tenant_id,
            request,
            mcp_backend=mcp_backend,
            plugin_backend=plugin_backend,
        )
        if sandbox_mismatch is not None:
            return self._failure(request, sandbox_mismatch)
        if (mcp_backend is not None or plugin_backend is not None) and self._policy_gateway is None:
            return self._failure(request, "capability identity unavailable")
        authorization_failure = await self._authorize(
            tenant_id,
            request,
            user_id=user_id,
            role=role,
            capability_parts=_external_capability_parts(
                tenant_id,
                request,
                mcp_backend=mcp_backend,
                plugin_backend=plugin_backend,
            ),
        )
        if authorization_failure is not None:
            return authorization_failure
        try:
            if mcp_backend is not None:
                if type(user_id) is not UUID:
                    return self._failure(request, "capability identity unavailable")
                payload = await mcp_backend.invoke(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=request.run_id,
                    actor=request.actor,
                    name=request.tool_name,
                    arguments=request.arguments,
                    idempotency_key=request.idempotency_key,
                )
            elif plugin_backend is not None:
                if type(user_id) is not UUID:
                    return self._failure(request, "capability identity unavailable")
                payload = await plugin_backend.invoke(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=request.run_id,
                    actor=request.actor,
                    name=request.tool_name,
                    arguments=request.arguments,
                    idempotency_key=request.idempotency_key,
                )
            else:
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
        except RuntimeCapabilityError as error:
            return self._failure(request, _deterministic_failure_reason(error))
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
        capability_parts: tuple[str, str, str] | None = None,
    ) -> HarnessToolCallResult | None:
        if type(user_id) is not UUID or not isinstance(role, Role):
            if self._policy_gateway is not None or self._require_actor_identity:
                return self._failure(request, "capability identity unavailable")
            return None
        if self._policy_gateway is None:
            return None
        decision = await self._policy_gateway.invoke(
            _capability_request(tenant_id, user_id, request, capability_parts=capability_parts),
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

    def _available_mcp_backend(self, tenant_id: UUID, tool_name: str) -> McpToolBackend | None:
        if self._mcp_backend is None:
            return None
        try:
            available = self._mcp_backend.is_available(tenant_id, tool_name)
        except Exception:  # noqa: BLE001 - MCP routing discovery must fail closed.
            return None
        if not available:
            return None
        return self._mcp_backend

    def _available_plugin_backend(self, tenant_id: UUID, tool_name: str) -> PluginToolBackend | None:
        if self._plugin_backend is None:
            return None
        try:
            available = self._plugin_backend.is_available(tenant_id, tool_name)
        except Exception:  # noqa: BLE001 - plugin routing discovery must fail closed.
            return None
        if not available:
            return None
        return self._plugin_backend

    async def _prepare_external_backends(
        self,
        tenant_id: UUID,
        *,
        backends: tuple[object | None, ...],
    ) -> str | None:
        for backend in backends:
            if backend is None:
                continue
            ensure_tenant_loaded = getattr(backend, "ensure_tenant_loaded", None)
            if not callable(ensure_tenant_loaded):
                continue
            try:
                await ensure_tenant_loaded(tenant_id)
            except Exception as error:  # noqa: BLE001 - optional backend preparation fails closed.
                _LOGGER.warning(
                    "harness_external_backend_prepare_failed backend=%s tenant_id=%s error_type=%s",
                    type(backend).__name__,
                    tenant_id,
                    type(error).__name__,
                )
                return "external tool tenant preparation failed"
        return None


def _capability_request(
    tenant_id: UUID,
    user_id: UUID,
    request: HarnessToolCallRequest,
    *,
    capability_parts: tuple[str, str, str] | None = None,
) -> CapabilityRequest:
    capability, operation, resource = capability_parts or _capability_parts(request)
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


def _mcp_capability_parts(request: HarnessToolCallRequest) -> CapabilityPolicyParts:
    return "mcp", "invoke", f"mcp/{request.tool_name.replace('.', '/')}"


def _plugin_capability_parts(request: HarnessToolCallRequest) -> CapabilityPolicyParts:
    return "plugin", "use", f"plugin/{request.tool_name.replace('.', '/')}"


def _external_capability_parts(
    tenant_id: UUID,
    request: HarnessToolCallRequest,
    *,
    mcp_backend: McpToolBackend | None,
    plugin_backend: PluginToolBackend | None,
) -> CapabilityPolicyParts | None:
    if mcp_backend is not None:
        return _mcp_capability_parts(request)
    if plugin_backend is not None:
        declared = _plugin_declared_capability_parts(plugin_backend, tenant_id, request.tool_name)
        if declared is not None:
            return declared
        return _plugin_capability_parts(request)
    return None


def _external_sandbox_mismatch(
    tenant_id: UUID,
    request: HarnessToolCallRequest,
    *,
    mcp_backend: McpToolBackend | None,
    plugin_backend: PluginToolBackend | None,
) -> str | None:
    backend: object | None = mcp_backend if mcp_backend is not None else plugin_backend
    if backend is None:
        return None
    declared = _declared_sandbox_profile(backend, tenant_id, request.tool_name)
    if declared is None:
        return None
    if declared == request.sandbox:
        return None
    return "tool sandbox does not match declared sandbox profile"


def _declared_sandbox_profile(backend: object, tenant_id: UUID, tool_name: str) -> str | None:
    sandbox_profile = getattr(backend, "sandbox_profile", None)
    if not callable(sandbox_profile):
        return None
    try:
        value = sandbox_profile(tenant_id, tool_name)
    except Exception:  # noqa: BLE001 - sandbox declaration lookup must fail closed.
        return "unavailable"
    if not isinstance(value, str) or not value.strip():
        return "unavailable"
    return value


def _plugin_declared_capability_parts(
    plugin_backend: PluginToolBackend,
    tenant_id: UUID,
    tool_name: str,
) -> CapabilityPolicyParts | None:
    capability_policy_parts = getattr(plugin_backend, "capability_policy_parts", None)
    if not callable(capability_policy_parts):
        return None
    try:
        parts = capability_policy_parts(tenant_id, tool_name)
    except Exception:  # noqa: BLE001 - plugin policy discovery must fail closed to generic plugin policy.
        return None
    if (
        not isinstance(parts, tuple)
        or len(parts) != 3
        or not all(isinstance(part, str) and part.strip() == part for part in parts)
    ):
        return None
    capability, operation, resource = parts
    if _SAFE_POLICY_TOKEN.fullmatch(capability) is None:
        return None
    if _SAFE_POLICY_TOKEN.fullmatch(operation) is None:
        return None
    if not resource:
        return None
    return capability, operation, resource


_SAFE_POLICY_TOKEN = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")

_EXTERNAL_SANDBOX_PROFILES = frozenset(
    {
        "http_read",
        "in_process",
        "local_process",
        "mcp_remote",
        "mcp_stdio",
        "remote_connector",
    }
)

_MCP_SANDBOX_PROFILES = frozenset({"mcp_remote", "mcp_stdio"})


def _uses_external_envelope(request: HarnessToolCallRequest) -> bool:
    return request.sandbox in _EXTERNAL_SANDBOX_PROFILES


def _may_route_to_mcp_backend(request: HarnessToolCallRequest) -> bool:
    return request.sandbox in _MCP_SANDBOX_PROFILES or not _uses_external_envelope(request)


def _may_route_to_plugin_backend(request: HarnessToolCallRequest) -> bool:
    return request.sandbox not in _MCP_SANDBOX_PROFILES


def _external_backends_for_request(
    gateway: HarnessToolGateway,
    request: HarnessToolCallRequest,
) -> tuple[object | None, ...]:
    if request.sandbox in _MCP_SANDBOX_PROFILES:
        return (gateway._mcp_backend,)
    return (gateway._plugin_backend,)


def _workspace_write_sandbox_failure(request: HarnessToolCallRequest) -> str | None:
    if not _has_project_workspace_write_side_effect(request):
        return None
    if request.sandbox == "workspace_write":
        return None
    return "workspace write requires workspace_write sandbox"


def _has_project_workspace_write_side_effect(request: HarnessToolCallRequest) -> bool:
    return (
        request.tool_name == "project.generate_zip"
        and _nonblank_argument(request, "project_id")
        and _nonblank_argument(request, "workspace_session_id")
    )


def _nonblank_argument(request: HarnessToolCallRequest, name: str) -> bool:
    value = request.arguments.get(name)
    return isinstance(value, str) and bool(value.strip())


def _capability_parts(request: HarnessToolCallRequest) -> CapabilityPolicyParts:
    if request.tool_name in {"calculator", "calculator_evaluate", "calculator.evaluate"}:
        return "calculator", "evaluate", "calculator"
    if request.tool_name in {"document.generate_docx", "presentation.generate_pptx", "project.generate_zip"}:
        return "file", "create", f"generated/{request.tool_name}"
    if request.tool_name in {"workspace_read", "workspace.read"}:
        path = request.arguments.get("path")
        return "file", "read", _workspace_policy_resource(path)
    if request.tool_name == "read_context":
        path = request.arguments.get("path")
        if isinstance(path, str):
            return "file", "read", _workspace_policy_resource(path)
        return "context", "read", "context"
    return "skill", "use", f"skill/{request.tool_name}"


def _workspace_policy_resource(path: object) -> str:
    if not isinstance(path, str) or not path:
        return "workspace"
    normalized = path.replace("\\", "/").lstrip("/")
    if normalized == "workspace" or normalized.startswith("workspace/"):
        return normalized
    return f"workspace/{normalized}"


def _capability_failure_reason(status: CapabilityStatus) -> str:
    if status is CapabilityStatus.WAITING_APPROVAL:
        return "capability requires approval"
    return "capability denied"


def _deterministic_failure_reason(error: RuntimeCapabilityError) -> str:
    reason = str(error).strip()
    if not reason or len(reason) > 512 or any(ord(character) < 32 or ord(character) == 127 for character in reason):
        return "tool input is invalid"
    return reason


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
    "McpToolBackend",
    "PluginToolBackend",
    "RuntimeToolBackend",
]
