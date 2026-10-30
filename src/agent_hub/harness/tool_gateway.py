from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, Protocol, cast
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.project_prerequisites import ProjectPrerequisiteObservation
from agent_hub.capabilities.project_verification import ProjectVerificationFailure
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult, JsonValue

if TYPE_CHECKING:
    from agent_hub.capabilities.go_project_inputs import GoProjectInputs
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    from agent_hub.capabilities.review_context import ConcreteModelReviewGate, _ConcreteReviewCall
    from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority
    from agent_hub.skills.sandbox.python_input_supply_protocol import PythonInputSupplyPlan
    from agent_hub.skills.sandbox.toolchain_catalog import ToolchainSupplyPlan

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


class _ConcretePolicyGateway(Protocol):
    async def invoke_concrete(self, request: CapabilityRequest, *, role: Role,
                              review: _ConcreteReviewCall) -> CapabilityResult: ...


class _StaticPolicyGateway(Protocol):
    async def authorize_static(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult: ...


class HarnessToolchainPolicyGateway(Protocol):
    async def authorize_project_toolchain(
        self, request: CapabilityRequest, *, role: Role, authority: ToolchainSupplyAuthority,
    ) -> CapabilityResult: ...


class HarnessGoRecipePolicyGateway(Protocol):
    async def authorize_project_go_recipe(
        self, request: CapabilityRequest, *, role: Role, authority: ToolchainSupplyAuthority,
    ) -> CapabilityResult: ...


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
        review_gate: ConcreteModelReviewGate | None = None,
    ) -> None:
        self._backend = backend
        self._policy_gateway = policy_gateway
        self._mcp_backend = mcp_backend
        self._plugin_backend = plugin_backend
        self._require_actor_identity = require_actor_identity
        self._raise_backend_errors = raise_backend_errors
        self._review_gate = review_gate

    async def authorize(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        """Admit static engineering scopes without executing a tool or caching authority."""
        if not isinstance(request, HarnessToolCallRequest):
            raise TypeError("request must be HarnessToolCallRequest")
        if request.tool_name not in {"workspace.write_text", "project.verify", "workspace.bundle"}:
            return self._failure(request, "tool permission mapping unavailable")
        policy_mapping_failure = _builtin_policy_mapping_failure(request)
        if policy_mapping_failure is not None:
            return self._failure(request, policy_mapping_failure)
        sandbox_failure = _workspace_write_sandbox_failure(request)
        if sandbox_failure is not None:
            return self._failure(request, sandbox_failure)
        if _uses_external_envelope(request):
            return self._failure(request, "tool permission mapping unavailable")
        if request.approval_required and self._policy_gateway is None:
            return self._failure(request, "approval required")
        if type(user_id) is not UUID or not isinstance(role, Role) or self._policy_gateway is None:
            return self._failure(request, "capability identity unavailable")
        try:
            failure = await self._authorize(tenant_id, request, user_id=user_id, role=role, static_only=True)
        except Exception as error:  # noqa: BLE001 - admission never exposes policy internals
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            return self._failure(request, "capability authorization unavailable")
        if failure is not None:
            return failure
        try:
            available = self._backend.is_available(tenant_id, request.tool_name) is True
        except Exception as error:  # noqa: BLE001 - discovery cannot grant execution authority
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            available = False
        if not available:
            return self._failure(request, "tool unavailable")
        return HarnessToolCallResult(
            call_id=request.call_id, tool_name=request.tool_name, status="succeeded",
            payload={"authorized": True},
        )

    async def authorize_project_inputs(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *, user_id: UUID,
        role: Role, plan: PythonInputSupplyPlan,
    ) -> HarnessToolCallResult:
        """Trusted pre-model admission only; intentionally not a model tool."""
        from agent_hub.capabilities.python_input_preparation import input_supply_request

        if (request.tool_name != 'project.prepare_inputs' or request.actor != 'main_agent'
                or request.arguments or request.sandbox != 'workspace_write'
                or _uses_external_envelope(request)):
            return self._failure(request, 'tool permission mapping unavailable')
        if type(user_id) is not UUID or not isinstance(role, Role) or self._policy_gateway is None:
            return self._failure(request, 'capability identity unavailable')
        try:
            admission = input_supply_request(plan, user_id=user_id, idempotency_key=request.idempotency_key)
            if admission.tenant_id != tenant_id or admission.run_id != request.run_id:
                return self._failure(request, 'capability denied')
            result = await self._policy_gateway.invoke(admission, role=role)
        except Exception:  # noqa: BLE001 - no private plan/catalog errors in public admission.
            return self._failure(request, 'capability authorization unavailable')
        if result.status is CapabilityStatus.ALLOWED:
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status='succeeded', payload={'authorized': True})
        payload: Mapping[str, JsonValue] = {}
        if result.status is CapabilityStatus.WAITING_APPROVAL and result.approval_id is not None:
            payload = {'approval_id': result.approval_id}
        return self._failure(request, result.reason or _capability_failure_reason(result.status), payload=payload)

    async def authorize_project_toolchain(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *, user_id: UUID,
        role: Role, plan: ToolchainSupplyPlan, original_reference: ProjectBaselineReference,
        authority: ToolchainSupplyAuthority | None = None,
    ) -> HarnessToolCallResult:
        """Trusted SDK admission only; not wired to a preparer or a public tool."""
        from agent_hub.capabilities.toolchain_preparation import toolchain_supply_request
        from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority

        if not isinstance(request, HarnessToolCallRequest):
            raise TypeError('request must be HarnessToolCallRequest')
        if (request.tool_name != 'project.prepare_toolchain' or request.actor != 'main_agent'
                or request.arguments or request.sandbox != 'workspace_write'
                or _uses_external_envelope(request)):
            return self._failure(request, 'tool permission mapping unavailable')
        if (type(user_id) is not UUID or not isinstance(role, Role)
                or type(authority) is not ToolchainSupplyAuthority
                or not callable(getattr(self._policy_gateway, 'authorize_project_toolchain', None))):
            return self._failure(request, 'capability identity unavailable')
        try:
            admission = toolchain_supply_request(plan, original_reference=original_reference,
                user_id=user_id, idempotency_key=request.idempotency_key)
            if (admission.tenant_id != tenant_id or admission.run_id != request.run_id
                    or admission.arguments['actor_role'] != role.value
                    or (authority.tenant_id, authority.run_id, authority.actor_id, authority.actor_role)
                        != (tenant_id, request.run_id, user_id, role)):
                return self._failure(request, 'capability denied')
            result = await cast(HarnessToolchainPolicyGateway, self._policy_gateway).authorize_project_toolchain(
                admission, role=role, authority=authority)
        except Exception:  # noqa: BLE001 - private plan/capture errors never become public authority.
            return self._failure(request, 'capability authorization unavailable')
        if result.status is CapabilityStatus.ALLOWED:
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status='succeeded', payload={'authorized': True})
        payload: Mapping[str, JsonValue] = {}
        if result.status is CapabilityStatus.WAITING_APPROVAL and result.approval_id is not None:
            payload = {'approval_id': result.approval_id}
        return self._failure(request, result.reason or _capability_failure_reason(result.status), payload=payload)

    async def authorize_project_go_recipe(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *, user_id: UUID,
        role: Role, inputs: GoProjectInputs, authority: ToolchainSupplyAuthority | None = None,
    ) -> HarnessToolCallResult:
        """Private original recipe admission; no public registration or execution wiring."""
        from agent_hub.capabilities.go_project_inputs import GoProjectInputs
        from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority

        if not isinstance(request, HarnessToolCallRequest):
            raise TypeError('request must be HarnessToolCallRequest')
        if (request.tool_name != 'project.prepare_go_recipe' or request.actor != 'main_agent'
                or request.arguments or request.sandbox != 'workspace_write' or _uses_external_envelope(request)):
            return self._failure(request, 'tool permission mapping unavailable')
        if (type(user_id) is not UUID or not isinstance(role, Role)
                or type(authority) is not ToolchainSupplyAuthority or type(inputs) is not GoProjectInputs
                or not callable(getattr(self._policy_gateway, 'authorize_project_go_recipe', None))):
            return self._failure(request, 'capability identity unavailable')
        try:
            inputs = GoProjectInputs.from_payload(inputs.to_payload())
            admission = inputs.approval_request(user_id=user_id, agent_id='main_agent',
                                                idempotency_key=request.idempotency_key)
            if (admission.tenant_id != tenant_id or admission.run_id != request.run_id
                    or inputs.scope.actor_role != role.value
                    or (authority.tenant_id, authority.run_id, authority.actor_id, authority.actor_role)
                        != (tenant_id, request.run_id, user_id, role)):
                return self._failure(request, 'capability denied')
            result = await cast(HarnessGoRecipePolicyGateway, self._policy_gateway).authorize_project_go_recipe(
                admission, role=role, authority=authority)
        except Exception:  # noqa: BLE001 - protected recipe internals never escape admission.
            return self._failure(request, 'capability authorization unavailable')
        if result.status is CapabilityStatus.ALLOWED:
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                         status='succeeded', payload={'authorized': True})
        payload: Mapping[str, JsonValue] = {}
        if result.status is CapabilityStatus.WAITING_APPROVAL and result.approval_id is not None:
            payload = {'approval_id': result.approval_id}
        return self._failure(request, result.reason or _capability_failure_reason(result.status), payload=payload)

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
        review_call = None
        if self._review_gate is not None:
            # Own ALL dispatch fields before the first await. The dataclass's
            # frozen flag alone is not isolation from object.__setattr__.
            request = HarnessToolCallRequest(run_id=UUID(str(request.run_id)), actor=request.actor,
                tool_name=request.tool_name, arguments=request.arguments,
                approval_required=request.approval_required, sandbox=request.sandbox,
                idempotency_key=request.idempotency_key, call_id=request.call_id)
            review_call = self._review_gate.invocation(tool_name=request.tool_name, backend=self._backend)
        if request.tool_name in {'project.prepare_inputs', 'project.prepare_toolchain', 'project.prepare_go_recipe'}:
            return self._failure(request, 'tool permission mapping unavailable')
        policy_mapping_failure = _builtin_policy_mapping_failure(request)
        if policy_mapping_failure is not None:
            return self._failure(request, policy_mapping_failure)
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
        if _requires_external_backend(request) and mcp_backend is None and plugin_backend is None:
            return self._failure(
                request,
                _external_tool_unavailable_reason(
                    tenant_id,
                    request,
                    backends=_external_backends_for_request(self, request),
                ),
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
            tool_origin="mcp" if mcp_backend is not None else "plugin" if plugin_backend is not None else "builtin",
            review_call=review_call,
        )
        if authorization_failure is not None:
            return authorization_failure
        from agent_hub.capabilities.generated_project_service import (
            GeneratedProjectDraftRejected,
            PythonMetadataPreparationApprovalRequired,
        )
        from agent_hub.capabilities.python_generated_service import (
            PythonGeneratedRecipeApprovalRequired,
        )
        try:
            if review_call is not None and not await review_call.before_dispatch():
                return self._failure(request, "capability authorization unavailable")
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
        except GeneratedProjectDraftRejected as error:
            draft = (_generated_draft_payload(error, tenant_id=tenant_id, user_id=user_id, request=request)
                     if type(user_id) is UUID and isinstance(role, Role) else None)
            return self._failure(request, 'generated project draft requires correction' if draft is not None
                                 else 'capability authorization unavailable', payload=draft)
        except (PythonGeneratedRecipeApprovalRequired, PythonMetadataPreparationApprovalRequired) as error:
            if (request.tool_name != 'project.verify' or self._policy_gateway is None
                    or type(user_id) is not UUID or not isinstance(role, Role)):
                return self._failure(request, 'capability authorization unavailable')
            try:
                expected = _capability_request(tenant_id, user_id, request)
                selected = error.approval_request.model_copy(update={'idempotency_key': expected.idempotency_key})
                selected = _project_verification_authorization(selected, expected=expected)
                decision = await self._policy_gateway.invoke(selected, role=role)
            except Exception:  # noqa: BLE001 - only a scope-bound independent approval can follow supply.
                return self._failure(request, 'capability authorization unavailable')
            payload = ({'approval_id': decision.approval_id}
                       if decision.status is CapabilityStatus.WAITING_APPROVAL and decision.approval_id is not None else {})
            return self._failure(request, decision.reason or 'python_project_install_approval_required', payload=payload)
        except RuntimeCapabilityError as error:
            diagnostic = error.execution_diagnostic
            failure_payload: dict[str, JsonValue] | None = None
            if type(diagnostic) is ExecutionDiagnostic:
                failure_payload = {"execution_diagnostic": diagnostic.to_payload()}
                if (request.tool_name == "project.verify"
                    and type(error.project_failure) is ProjectVerificationFailure):
                    failure_payload["verification_failure"] = error.project_failure.to_payload()
                if request.tool_name == "project.verify" and error.python_project_failure is not None:
                    failure_payload["python_verification_failure"] = error.python_project_failure.to_payload()
                    reference = error.python_project_failure.environment_reference
                    failure_payload["python_environment_reference"] = (
                        None if reference is None else reference.model_dump(mode="json")
                    )
            elif (request.tool_name == "project.verify"
                  and type(error.project_prerequisite) is ProjectPrerequisiteObservation
                  and error.project_failure is None):
                failure_payload = {"verification_blocked": error.project_prerequisite.to_payload()}
            return self._failure(
                request, _deterministic_failure_reason(error), payload=failure_payload,
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
        capability_parts: tuple[str, str, str] | None = None,
        static_only: bool = False,
        tool_origin: Literal["builtin", "mcp", "plugin"] = "builtin",
        review_call: _ConcreteReviewCall | None = None,
    ) -> HarnessToolCallResult | None:
        if type(user_id) is not UUID or not isinstance(role, Role):
            if self._policy_gateway is not None or self._require_actor_identity:
                return self._failure(request, "capability identity unavailable")
            return None
        if self._policy_gateway is None:
            return None
        admission = _capability_request(tenant_id, user_id, request,
                                        capability_parts=capability_parts, tool_origin=tool_origin)
        if request.tool_name == "project.verify" and capability_parts is None:
            resolver = getattr(self._backend, "project_verification_static_authorization", None) if static_only else None
            if not callable(resolver):
                resolver = getattr(self._backend, "project_verification_authorization", None)
            if callable(resolver):
                if request.arguments:
                    return self._failure(request, "project.verify takes no arguments")
                # Only trusted backend scope can specialize a grant. An explicit
                # None preserves Node behavior; exceptions NEVER fall back.
                try:
                    selected = await resolver(admission)
                    if selected is not None:
                        admission = _project_verification_authorization(selected, expected=admission)
                except Exception as error:  # noqa: BLE001 - no private recipe error or fallback grant.
                    draft = None if static_only else _generated_draft_payload(
                        error, tenant_id=tenant_id, user_id=user_id, request=request)
                    if draft is not None:
                        return self._failure(request, 'generated project draft requires correction', payload=draft)
                    reason = _project_authorization_failure_reason(error)
                    error.__traceback__ = None
                    error.__context__ = None
                    error.__cause__ = None
                    return self._failure(request, reason)
        if static_only:
            if not callable(getattr(self._policy_gateway, "authorize_static", None)):
                return None  # unresolved concrete consent is deferred, never awaited here.
            decision = await cast(_StaticPolicyGateway, self._policy_gateway).authorize_static(admission, role=role)
        elif review_call is not None:
            if not callable(getattr(self._policy_gateway, "invoke_concrete", None)):
                return self._failure(request, "capability authorization unavailable")
            decision = await cast(_ConcretePolicyGateway, self._policy_gateway).invoke_concrete(
                admission, role=role, review=review_call)
        else:
            decision = await self._policy_gateway.invoke(admission, role=role)
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


def _generated_draft_payload(
    error: Exception, *, tenant_id: UUID, user_id: UUID, request: HarnessToolCallRequest,
) -> Mapping[str, JsonValue] | None:
    import hashlib

    from agent_hub.capabilities.generated_project_draft import (
        GeneratedDraftRejection,
        validated_generated_draft_payload,
    )
    from agent_hub.capabilities.generated_project_service import GeneratedProjectDraftRejected

    observation = getattr(error, 'rejection', None)
    if (type(error) is not GeneratedProjectDraftRejected or type(observation) is not GeneratedDraftRejection
            or request.tool_name != 'project.verify' or request.arguments or request.sandbox != 'workspace_write'):
        return None
    try:
        rejection = validated_generated_draft_payload(observation.to_payload())
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return None
    if (rejection is None or rejection['tenant_id'] != str(tenant_id) or rejection['run_id'] != str(request.run_id)
            or rejection['user_id'] != str(user_id) or rejection['proposer_actor'] != request.actor):
        return None
    return {'generated_draft_rejection': rejection, 'call_id': request.call_id,
        'arguments_sha256': hashlib.sha256(b'{}').hexdigest()}


def _project_authorization_failure_reason(error: Exception) -> str:
    from typing import get_args

    from agent_hub.capabilities.generated_project_service import GeneratedProjectError
    from agent_hub.capabilities.project_verification import ProjectVerificationError
    from agent_hub.skills.sandbox.python_dependency_protocol import PythonDependencyClosureError
    from agent_hub.skills.sandbox.python_input_supply_protocol import SupplyError
    from agent_hub.skills.sandbox.python_metadata_preparation_client import PUBLIC_FAILURE_CODES
    from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerError

    # A syntactically safe prefix is not a finite code: it can still carry a
    # project-controlled token. Keep diagnostics independent of exception text.
    allowed = {*get_args(PythonProjectBrokerError), *get_args(SupplyError),
        *get_args(PythonDependencyClosureError.__value__),
        *PUBLIC_FAILURE_CODES,
        'python_project_systemd_linux_unsupported', 'python_project_recipe_invalid',
        'python_project_recipe_unsupported', 'python_project_lock_invalid',
        'python_project_declaration_limit', 'python_project_response_invalid',
        'python_project_install_approval_required', 'input_supply_approval_required',
        'generated_python_project_unavailable', 'generated_acceptance_scope_changed',
        'generated_acceptance_state_invalid', 'generated_acceptance_persistence_unavailable',
        'generated_acceptance_unknown_no_recapture', 'generated_acceptance_unavailable_or_changed',
        'generated_acceptance_manifest_required', 'generated_acceptance_scaffold_incomplete',
        'generated_acceptance_python_declaration_invalid', 'generated_acceptance_node_declaration_invalid',
        'generated_acceptance_test_execution_invalid'}
    code = str(error)
    if isinstance(error, (GeneratedProjectError, ProjectVerificationError)) and code in allowed:
        return code
    return 'capability authorization unavailable'


def _project_verification_authorization(selected: object, *, expected: CapabilityRequest) -> CapabilityRequest:
    if type(selected) is CapabilityRequest and selected.resource.startswith('workspace/current/project-metadata/'):
        return _metadata_preparation_authorization(selected, expected=expected)
    if type(selected) is not CapabilityRequest or not selected.resource.startswith('workspace/current/project-inputs/'):
        return _python_recipe_authorization(selected, expected=expected)
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference

    selected = CapabilityRequest.model_validate(selected.model_dump(), strict=True)
    match = re.fullmatch(r'workspace/current/project-inputs/python/([a-f0-9]{64})', selected.resource)
    args = selected.arguments
    if (any(getattr(selected, name) != getattr(expected, name)
            for name in ('tenant_id', 'user_id', 'run_id', 'agent_id', 'idempotency_key'))
            or (selected.capability, selected.operation) != ('command', 'run') or match is None
            or set(args) != {'schema_version', 'plan_sha256', 'original_reference', 'project_id',
                             'workspace_session_id', 'catalog_sha256', 'declaration_sha256',
                             'manifest_sha256', 'lock_sha256'}
            or type(args['schema_version']) is not int or args['schema_version'] != 1
            or args['plan_sha256'] != match.group(1)
            or any(type(args[name]) is not str or re.fullmatch(r'[a-f0-9]{64}', cast(str, args[name])) is None
                   for name in ('catalog_sha256', 'declaration_sha256', 'manifest_sha256', 'lock_sha256'))
            or any(type(args[name]) is not str or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', cast(str, args[name])) is None
                   for name in ('project_id', 'workspace_session_id'))):
        raise ValueError('project verification authorization unavailable')
    ProjectBaselineReference.from_payload(args['original_reference'])
    return selected


def _metadata_preparation_authorization(selected: CapabilityRequest, *, expected: CapabilityRequest) -> CapabilityRequest:
    selected = CapabilityRequest.model_validate(dict(vars(selected)) | (selected.model_extra or {}), strict=True)
    args = selected.arguments
    match = re.fullmatch(r'workspace/current/project-metadata/python/([a-f0-9]{64})', selected.resource)
    if (match is None or selected.tool_origin != 'builtin'
            or any(getattr(selected, name) != getattr(expected, name)
                for name in ('tenant_id', 'user_id', 'run_id', 'agent_id', 'idempotency_key'))
            or (selected.capability, selected.operation) != ('command', 'run')
            or set(args) != {'schema_version', 'purpose', 'plan_sha256', 'project_id', 'workspace_session_id',
                'catalog_sha256', 'root_config_sha256', 'source_sha256', 'catalog_count', 'catalog_bytes',
                'download_count', 'download_bytes'}
            or type(args.get('schema_version')) is not int or args['schema_version'] != 1
            or args['purpose'] != 'python_dependency_metadata' or args['plan_sha256'] != match.group(1)
            or any(type(args[name]) is not str or re.fullmatch(r'[a-f0-9]{64}', cast(str, args[name])) is None
                for name in ('catalog_sha256', 'root_config_sha256', 'source_sha256'))
            or any(type(args[name]) is not str or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', cast(str, args[name])) is None
                for name in ('project_id', 'workspace_session_id'))
            or any(type(args[name]) is not int or not 0 <= cast(int, args[name]) < 2**63
                for name in ('catalog_count', 'catalog_bytes', 'download_count', 'download_bytes'))
            or not 0 <= cast(int, args['download_count']) <= cast(int, args['catalog_count']) <= 1000
            or not 0 <= cast(int, args['download_bytes']) <= cast(int, args['catalog_bytes'])):
        raise ValueError('project metadata preparation authorization unavailable')
    return selected


def _python_recipe_authorization(selected: object, *, expected: CapabilityRequest) -> CapabilityRequest:
    if type(selected) is not CapabilityRequest:
        raise ValueError("project verification authorization unavailable")
    selected = CapabilityRequest.model_validate(selected.model_dump(), strict=True)
    fields = ("tenant_id", "user_id", "run_id", "agent_id", "idempotency_key")
    match = re.fullmatch(r"workspace/current/project-verification/python/([a-f0-9]{64})", selected.resource)
    arguments = selected.arguments
    if (any(getattr(selected, field) != getattr(expected, field) for field in fields)
        or (selected.capability, selected.operation) != ("command", "run") or match is None
        or set(arguments) != {"schema_version", "recipe_sha256", "environment_input_sha256",
                              "project_id", "workspace_session_id"}
        or type(arguments["schema_version"]) is not int or arguments["schema_version"] != 1
        or arguments["recipe_sha256"] != match.group(1)
        or type(arguments["environment_input_sha256"]) is not str
        or re.fullmatch(r"[a-f0-9]{64}", arguments["environment_input_sha256"]) is None
        or any(type(value) is not str
               or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value) is None
               for value in (arguments["project_id"], arguments["workspace_session_id"]))):
        raise ValueError("project verification authorization unavailable")
    return selected


def _capability_request(
    tenant_id: UUID,
    user_id: UUID,
    request: HarnessToolCallRequest,
    *,
    capability_parts: tuple[str, str, str] | None = None,
    tool_origin: Literal["builtin", "mcp", "plugin"] = "builtin",
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
        tool_origin=tool_origin,
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
_SAFE_AVAILABILITY_REASON = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")

_STATIC_BUILTIN_POLICY_PARTS: Mapping[str, CapabilityPolicyParts] = {
    "project.verify": ("command", "run", "workspace/current/project-verification"),
    "project.inspect": ("file", "read", "workspace/current"),
    "calculator": ("calculator", "evaluate", "calculator"),
    "calculator_evaluate": ("calculator", "evaluate", "calculator"),
    "calculator.evaluate": ("calculator", "evaluate", "calculator"),
    "document.generate_docx": ("file", "create", "generated/document.generate_docx"),
    "http.read": ("network", "read", "network/http"),
    "presentation.generate_pptx": (
        "file",
        "create",
        "generated/presentation.generate_pptx",
    ),
    "project.preflight_architecture": (
        "file",
        "create",
        "generated/project.preflight_architecture",
    ),
    "project.generate_zip": ("file", "create", "generated/project.generate_zip"),
    "workspace.write_text": ("file", "write", "workspace/current"),
    "workspace.list": ("file", "read", "workspace/current"),
    "workspace.bundle": ("file", "create", "workspace/bundle"),
}
_DYNAMIC_BUILTIN_TOOLS = frozenset({"read_context", "workspace_read", "workspace.read"})
_KNOWN_BUILTIN_TOOLS = frozenset(_STATIC_BUILTIN_POLICY_PARTS) | _DYNAMIC_BUILTIN_TOOLS
_RESERVED_BUILTIN_PREFIXES = ("calculator.", "document.", "http.", "presentation.", "project.", "workspace.")
_PRIVILEGED_BUILTIN_SANDBOXES = frozenset(
    {"generated_artifact_store", "project_workspace_store", "workspace_write"}
)

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
    return (
        request.tool_name not in _KNOWN_BUILTIN_TOOLS
        and request.sandbox in _EXTERNAL_SANDBOX_PROFILES
    )


def _requires_external_backend(request: HarnessToolCallRequest) -> bool:
    return _uses_external_envelope(request)


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


def _external_tool_unavailable_reason(
    tenant_id: UUID,
    request: HarnessToolCallRequest,
    *,
    backends: tuple[object | None, ...],
) -> str:
    reason = _declared_availability_failure_reason(backends, tenant_id, request.tool_name)
    if request.sandbox in _MCP_SANDBOX_PROFILES:
        if reason is None:
            return "MCP tool unavailable"
        return f"MCP tool unavailable: {reason}"
    if reason is None:
        return "external tool unavailable"
    return f"Plugin tool unavailable: {reason}"


def _declared_availability_failure_reason(
    backends: tuple[object | None, ...],
    tenant_id: UUID,
    tool_name: str,
) -> str | None:
    for backend in backends:
        if backend is None:
            continue
        availability_failure_reason = getattr(backend, "availability_failure_reason", None)
        if not callable(availability_failure_reason):
            continue
        try:
            value = availability_failure_reason(tenant_id, tool_name)
        except Exception as error:  # noqa: BLE001 - external availability diagnostics must fail closed.
            _LOGGER.debug(
                "harness_external_availability_reason_failed backend=%s tenant_id=%s error_type=%s",
                type(backend).__name__,
                tenant_id,
                type(error).__name__,
            )
            continue
        if isinstance(value, str):
            reason = value.strip()
            if _SAFE_AVAILABILITY_REASON.fullmatch(reason) is not None:
                return reason
    return None


def _workspace_write_sandbox_failure(request: HarnessToolCallRequest) -> str | None:
    if not _has_project_workspace_write_side_effect(request):
        return None
    if request.sandbox == "workspace_write":
        return None
    return "workspace write requires workspace_write sandbox"


def _has_project_workspace_write_side_effect(request: HarnessToolCallRequest) -> bool:
    return request.tool_name in {"workspace.write_text", "workspace.bundle"} or (
        request.tool_name == "project.generate_zip"
        and _nonblank_argument(request, "project_id")
        and _nonblank_argument(request, "workspace_session_id")
    )


def _nonblank_argument(request: HarnessToolCallRequest, name: str) -> bool:
    value = request.arguments.get(name)
    return isinstance(value, str) and bool(value.strip())


def _builtin_policy_mapping_failure(request: HarnessToolCallRequest) -> str | None:
    if request.tool_name in _KNOWN_BUILTIN_TOOLS or _uses_external_envelope(request):
        return None
    if request.sandbox in _PRIVILEGED_BUILTIN_SANDBOXES:
        return "tool permission mapping unavailable"
    if request.tool_name.startswith(_RESERVED_BUILTIN_PREFIXES):
        return "tool permission mapping unavailable"
    return None


def _capability_parts(request: HarnessToolCallRequest) -> CapabilityPolicyParts:
    static_parts = _STATIC_BUILTIN_POLICY_PARTS.get(request.tool_name)
    if static_parts is not None:
        return static_parts
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
