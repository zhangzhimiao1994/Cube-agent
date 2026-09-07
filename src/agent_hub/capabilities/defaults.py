from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
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
from agent_hub.harness.tool_gateway import HarnessToolGateway, McpToolBackend, PluginToolBackend

ToolApprovalPolicyGetter = Callable[[UUID], Awaitable[bool]]
ToolApprovalModeGetter = Callable[[UUID], Awaitable[str]]


class PluginPolicyRuleSource(Protocol):
    def capability_policy_rules(self, tenant_id: UUID) -> tuple[CapabilityRule, ...]: ...


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
        plugin_policy_rules: PluginPolicyRuleSource | None = None,
    ) -> None:
        self._approvals = approvals
        self._run_repository = run_repository
        self._require_approval_for_tools = require_approval_for_tools
        self._tool_approval_mode = tool_approval_mode
        self._approval_reviewer = approval_reviewer
        self._plugin_policy_rules = plugin_policy_rules

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        role: Role,
    ) -> CapabilityResult:
        approval_required = await self._tool_approval_required(request.tenant_id)
        extra_rules = self._extra_policy_rules(request)
        explicit_plugin_effect = _explicit_policy_effect(extra_rules, request, role)
        if explicit_plugin_effect is PolicyEffect.DENY:
            return CapabilityResult(
                CapabilityStatus.DENIED,
                request.run_id,
                reason="capability denied",
            )
        if await _has_approved_capability_request(self._run_repository, request):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if await _has_approved_capability_scope(self._run_repository, request):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if explicit_plugin_effect is PolicyEffect.ALLOW:
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        gateway = CapabilityGateway(
            default_capability_policy(
                request.tenant_id,
                require_approval_for_tools=approval_required,
                extra_rules=extra_rules,
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

    def _extra_policy_rules(self, request: CapabilityRequest) -> tuple[CapabilityRule, ...]:
        if self._plugin_policy_rules is None:
            return ()
        try:
            rules = self._plugin_policy_rules.capability_policy_rules(request.tenant_id)
        except Exception:  # noqa: BLE001 - plugin policy discovery must fail closed for plugin tools.
            return _deny_current_plugin_request_rules(request)
        if not isinstance(rules, tuple) or not all(isinstance(rule, CapabilityRule) for rule in rules):
            return _deny_current_plugin_request_rules(request)
        return rules


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
    extra_rules: tuple[CapabilityRule, ...] = (),
) -> CapabilityPolicy:
    allowed_roles = (Role.SUPER_ADMIN, Role.ADMIN, Role.OPERATOR)
    generated_effect = (
        PolicyEffect.REQUIRE_APPROVAL if require_approval_for_tools else PolicyEffect.ALLOW
    )
    skill_effect = (
        PolicyEffect.REQUIRE_APPROVAL if require_approval_for_tools else PolicyEffect.ALLOW
    )
    mcp_effect = PolicyEffect.REQUIRE_APPROVAL if require_approval_for_tools else PolicyEffect.ALLOW
    plugin_effect = (
        PolicyEffect.REQUIRE_APPROVAL if require_approval_for_tools else PolicyEffect.ALLOW
    )
    default_rules = tuple(
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
            ("plugin", "use", "plugin", plugin_effect),
        )
    )
    return CapabilityPolicy(default_rules + extra_rules)


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
    plugin_backend: PluginToolBackend | None = None,
) -> RuntimeCapabilityStack:
    runtime_gateway = RuntimeCapabilityGateway(
        skill_store_dir=skill_store_dir,
        tenant_id=tenant_id,
        workspace_root=workspace_root,
        generated_artifact_dir=generated_artifact_dir,
        project_workspace_dir=project_workspace_dir,
        tool_registry=tool_registry,
    )
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
        plugin_policy_rules=_plugin_policy_rule_source(plugin_backend),
    )
    harness_tool_gateway = HarnessToolGateway(
        runtime_gateway,
        policy_gateway=policy_gateway,
        mcp_backend=mcp_backend,
        plugin_backend=plugin_backend,
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


def _deny_current_plugin_request_rules(request: CapabilityRequest) -> tuple[CapabilityRule, ...]:
    if not _is_plugin_policy_request(request):
        return ()
    resource = normalize_resource(request.resource)
    if resource is None:
        return ()
    return tuple(
        CapabilityRule(
            tenant_id=request.tenant_id,
            role=role,
            agent_id=None,
            capability=request.capability,
            operation=request.operation,
            resource_prefix=resource,
            effect=PolicyEffect.DENY,
        )
        for role in (Role.SUPER_ADMIN, Role.ADMIN, Role.OPERATOR)
    )


def _is_plugin_policy_request(request: CapabilityRequest) -> bool:
    resource = normalize_resource(request.resource)
    return (
        request.capability == "plugin"
        or resource == "plugin"
        or (resource is not None and resource.startswith("plugin/"))
    )


def _plugin_policy_rule_source(source: object | None) -> PluginPolicyRuleSource | None:
    if source is None:
        return None
    if not callable(getattr(source, "capability_policy_rules", None)):
        return None
    return cast(PluginPolicyRuleSource, source)


def _explicit_policy_effect(
    rules: tuple[CapabilityRule, ...],
    request: CapabilityRequest,
    role: Role,
) -> PolicyEffect | None:
    normalized_resource = normalize_resource(request.resource)
    if normalized_resource is None:
        return None
    effects = [
        rule.effect
        for rule in rules
        if (
            rule.tenant_id == request.tenant_id
            and (rule.role is None or rule.role is role)
            and (rule.agent_id is None or rule.agent_id == request.agent_id)
            and rule.capability == request.capability
            and rule.operation == request.operation
            and (
                normalized_resource == rule.resource_prefix
                or normalized_resource.startswith(f"{rule.resource_prefix}/")
            )
        )
    ]
    if PolicyEffect.DENY in effects:
        return PolicyEffect.DENY
    if PolicyEffect.REQUIRE_APPROVAL in effects:
        return PolicyEffect.REQUIRE_APPROVAL
    if PolicyEffect.ALLOW in effects:
        return PolicyEffect.ALLOW
    return None
