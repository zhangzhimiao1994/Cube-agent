from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.gateway import CapabilityGateway, CapabilityResult
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.harness.tool_gateway import HarnessToolGateway


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
    ) -> None:
        self._approvals = approvals
        self._run_repository = run_repository

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        role: Role,
    ) -> CapabilityResult:
        gateway = CapabilityGateway(
            default_capability_policy(request.tenant_id),
            self._approvals,
            self._run_repository,
        )
        return await gateway.invoke(request, role=role)


def default_capability_policy(tenant_id: UUID) -> CapabilityPolicy:
    allowed_roles = (Role.SUPER_ADMIN, Role.ADMIN, Role.OPERATOR)
    return CapabilityPolicy(
        tuple(
            CapabilityRule(
                tenant_id=tenant_id,
                role=role,
                agent_id=None,
                capability=capability,
                operation=operation,
                resource_prefix=resource_prefix,
                effect=PolicyEffect.ALLOW,
            )
            for role in allowed_roles
            for capability, operation, resource_prefix in (
                ("calculator", "evaluate", "calculator"),
                ("file", "read", "workspace"),
                ("file", "create", "generated"),
                ("context", "read", "context"),
                ("skill", "use", "skill"),
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
