from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.gateway import (
    ApprovalReviewDecision,
    ApprovalReviewer,
    ApprovalReviewOutcome,
    CapabilityGateway,
    CapabilityResult,
    CapabilityStatus,
)
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule, normalize_resource
from agent_hub.capabilities.runtime import CapabilityManifestProvider, RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.harness.tool_gateway import HarnessToolGateway, McpToolBackend

ToolApprovalPolicyGetter = Callable[[UUID], Awaitable[bool]]
ToolApprovalModeGetter = Callable[[UUID], Awaitable[str]]


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
        tool_approval_mode: ToolApprovalModeGetter | None = None,
        approval_reviewer: ApprovalReviewer | None = None,
    ) -> None:
        self._approvals = approvals
        self._run_repository = run_repository
        self._require_approval_for_tools = require_approval_for_tools
        self._tool_approval_mode = tool_approval_mode
        self._approval_reviewer = approval_reviewer

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        role: Role,
    ) -> CapabilityResult:
        if await _has_approved_capability_request(self._run_repository, request):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if await _has_approved_capability_scope(self._run_repository, request):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        approval_required = await self._tool_approval_required(request.tenant_id)
        gateway = CapabilityGateway(
            default_capability_policy(
                request.tenant_id,
                require_approval_for_tools=approval_required,
            ),
            self._approvals,
            self._run_repository,
            approval_reviewer=await self._approval_reviewer_for_request(request.tenant_id),
        )
        return await gateway.invoke(request, role=role)

    async def _tool_approval_required(self, tenant_id: UUID) -> bool:
        if self._require_approval_for_tools is None:
            return False
        try:
            return await self._require_approval_for_tools(tenant_id)
        except Exception:  # noqa: BLE001 - approval policy must fail closed.
            return True

    async def _approval_reviewer_for_request(self, tenant_id: UUID) -> ApprovalReviewer | None:
        if self._approval_reviewer is not None:
            return self._approval_reviewer
        if await self._tool_approval_mode_for_tenant(tenant_id) == "auto_review":
            return CodexAutoApprovalReviewer()
        return None

    async def _tool_approval_mode_for_tenant(self, tenant_id: UUID) -> str:
        if self._tool_approval_mode is None:
            return "ask"
        try:
            mode = await self._tool_approval_mode(tenant_id)
        except Exception:  # noqa: BLE001 - approval review mode must fail closed.
            return "ask"
        if mode in {"ask", "auto_review"}:
            return mode
        return "ask"


class CodexAutoApprovalReviewer:
    """Deterministic auto-reviewer for replay-safe built-in generated artifacts."""

    reviewer = "auto_review"
    _SAFE_GENERATED_RESOURCES = frozenset(
        {
            "generated/document.generate_docx",
            "generated/presentation.generate_pptx",
            "generated/project.generate_zip",
        }
    )

    async def review(self, request: CapabilityRequest) -> ApprovalReviewDecision:
        return self.review_sync(request)

    def review_sync(self, request: CapabilityRequest) -> ApprovalReviewDecision:
        normalized_resource = normalize_resource(request.resource)
        if (
            request.capability == "file"
            and request.operation == "create"
            and normalized_resource in self._SAFE_GENERATED_RESOURCES
            and not _has_workspace_write_side_effect(request)
        ):
            return ApprovalReviewDecision(
                outcome=ApprovalReviewOutcome.ALLOW,
                reviewer=self.reviewer,
                reason="approved replay-safe generated artifact",
            )
        return ApprovalReviewDecision(
            outcome=ApprovalReviewOutcome.REQUIRE_USER_APPROVAL,
            reviewer=self.reviewer,
            reason="requires human approval",
        )


def _has_workspace_write_side_effect(request: CapabilityRequest) -> bool:
    return (
        normalize_resource(request.resource) == "generated/project.generate_zip"
        and _nonblank_argument(request, "project_id")
        and _nonblank_argument(request, "workspace_session_id")
    )


def _nonblank_argument(request: CapabilityRequest, name: str) -> bool:
    value = request.arguments.get(name)
    return isinstance(value, str) and bool(value.strip())


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


async def _has_approved_capability_scope(repository: object, request: CapabilityRequest) -> bool:
    approval_scope = capability_approval_scope(request)
    if approval_scope is None:
        return False
    checker = getattr(repository, "is_capability_approval_scope_approved", None)
    if not callable(checker):
        return False
    return bool(await checker(request.tenant_id, request.run_id, approval_scope))


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
    mcp_effect = PolicyEffect.REQUIRE_APPROVAL if require_approval_for_tools else PolicyEffect.ALLOW
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
                ("mcp", "invoke", "mcp", mcp_effect),
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
    project_workspace_dir: Path | None = None,
    require_approval_for_tools: ToolApprovalPolicyGetter | None = None,
    tool_approval_mode: ToolApprovalModeGetter | None = None,
    approval_reviewer: ApprovalReviewer | None = None,
    tool_registry: CapabilityManifestProvider | None = None,
    mcp_backend: McpToolBackend | None = None,
) -> RuntimeCapabilityStack:
    runtime_gateway = RuntimeCapabilityGateway(
        skill_store_dir=skill_store_dir,
        workspace_root=workspace_root,
        generated_artifact_dir=generated_artifact_dir,
        project_workspace_dir=project_workspace_dir,
        tool_registry=tool_registry,
    )
    del tenant_id
    default_reviewer = (
        CodexAutoApprovalReviewer()
        if approval_reviewer is None and tool_approval_mode is None
        else approval_reviewer
    )
    policy_gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()),
        run_repository,
        require_approval_for_tools=require_approval_for_tools,
        tool_approval_mode=tool_approval_mode,
        approval_reviewer=default_reviewer,
    )
    harness_tool_gateway = HarnessToolGateway(
        runtime_gateway,
        policy_gateway=policy_gateway,
        mcp_backend=mcp_backend,
        raise_backend_errors=True,
    )
    return RuntimeCapabilityStack(
        runtime_gateway=runtime_gateway,
        policy_gateway=policy_gateway,
        harness_tool_gateway=harness_tool_gateway,
    )


__all__ = [
    "CodexAutoApprovalReviewer",
    "DefaultRuntimeCapabilityPolicyGateway",
    "RuntimeCapabilityStack",
    "build_runtime_capability_stack",
    "default_capability_policy",
]
