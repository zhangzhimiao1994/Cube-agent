from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    fingerprint_capability_request,
)
from agent_hub.capabilities.gateway import CapabilityGateway, CapabilityResult, CapabilityStatus
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.harness.tool_gateway import HarnessToolGateway

ToolApprovalPolicyGetter = Callable[[UUID], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityStack:
    runtime_gateway: RuntimeCapabilityGateway
    policy_gateway: DefaultRuntimeCapabilityPolicyGateway
    harness_tool_gateway: HarnessToolGateway


class DefaultRuntimeCapabilityPolicyGateway:
    def __init__(
        self,
        approvals: ApprovalService,
        run_repository: object,
        *,
        require_approval_for_tools: ToolApprovalPolicyGetter | None = None,
    ) -> None:
        self._approvals = approvals
        self._run_repository = run_repository
        self._require_approval_for_tools = require_approval_for_tools

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        role: Role,
    ) -> CapabilityResult:
        if await _has_approved_capability_request(self._run_repository, request):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        gateway = CapabilityGateway(
            default_capability_policy(
                request.tenant_id,
                require_approval_for_tools=await self._tool_approval_required(request.tenant_id),
            ),
            self._approvals,
            self._run_repository,
        )
        return await gateway.invoke(request, role=role)

    async def _tool_approval_required(self, tenant_id: UUID) -> bool:
        if self._require_approval_for_tools is None:
            return False
        try:
            return await self._require_approval_for_tools(tenant_id)
        except Exception:  # noqa: BLE001 - approval policy must fail closed.
            return True


async def _has_approved_capability_request(repository: object, request: CapabilityRequest) -> bool:
    checker = getattr(repository, "is_capability_approval_approved", None)
    if not callable(checker):
        return False
    return bool(
        await checker(
            request.tenant_id,
            request.run_id,
            fingerprint_capability_request(request),
        )
    )


def default_capability_policy(
    tenant_id: UUID,
    *,
    require_approval_for_tools: bool = False,
) -> CapabilityPolicy:
    allowed_roles = (Role.SUPER_ADMIN, Role.ADMIN, Role.OPERATOR)
    generated_effect = (
        PolicyEffect.REQUIRE_APPROVAL if require_approval_for_tools else PolicyEffect.ALLOW
    )
    skill_effect = (
        PolicyEffect.REQUIRE_APPROVAL if require_approval_for_tools else PolicyEffect.ALLOW
    )
    return CapabilityPolicy(
        tuple(
            CapabilityRule(
                tenant_id=tenant_id,
                role=role,
                agent_id=None,
                capability=capability,
                operation=operation,
                resource_prefix=resource_prefix,
                effect=effect,
            )
            for role in allowed_roles
            for capability, operation, resource_prefix, effect in (
                ("calculator", "evaluate", "calculator", PolicyEffect.ALLOW),
                ("file", "read", "workspace", PolicyEffect.ALLOW),
                ("file", "create", "generated", generated_effect),
                ("context", "read", "context", PolicyEffect.ALLOW),
                ("skill", "use", "skill", skill_effect),
            )
        )
    )


def build_runtime_capability_stack(
    *,
    tenant_id: UUID,
    run_repository: object,
    skill_store_dir: Path,
    workspace_root: Path | None,
    generated_artifact_dir: Path | None = None,
    require_approval_for_tools: ToolApprovalPolicyGetter | None = None,
) -> RuntimeCapabilityStack:
    runtime_gateway = RuntimeCapabilityGateway(
        skill_store_dir=skill_store_dir,
        workspace_root=workspace_root,
        generated_artifact_dir=generated_artifact_dir,
    )
    del tenant_id
    policy_gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()),
        run_repository,
        require_approval_for_tools=require_approval_for_tools,
    )
    harness_tool_gateway = HarnessToolGateway(
        runtime_gateway,
        policy_gateway=policy_gateway,
        raise_backend_errors=True,
    )
    return RuntimeCapabilityStack(
        runtime_gateway=runtime_gateway,
        policy_gateway=policy_gateway,
        harness_tool_gateway=harness_tool_gateway,
    )


__all__ = [
    "DefaultRuntimeCapabilityPolicyGateway",
    "RuntimeCapabilityStack",
    "build_runtime_capability_stack",
    "default_capability_policy",
]
