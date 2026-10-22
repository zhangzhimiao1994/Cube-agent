from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
from agent_hub.capabilities.permission_modes import (
    read_only_capability_allowed,
    run_permission_mode,
)
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule, normalize_resource
from agent_hub.capabilities.python_project_lifecycle import (
    PythonProjectTerminalHook,
    build_python_project_terminal_hook,
)
from agent_hub.capabilities.runtime import CapabilityManifestProvider, RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.harness.tool_gateway import (
    HarnessToolGateway,
    McpToolBackend,
    PluginToolBackend,
    _python_recipe_authorization,
)
from agent_hub.runtime.contracts import TaskContext

ToolApprovalPolicyGetter = Callable[[UUID], Awaitable[bool]]
ToolApprovalModeGetter = Callable[[UUID], Awaitable[str]]


class PluginPolicyRuleSource(Protocol):
    def capability_policy_rules(self, tenant_id: UUID) -> tuple[CapabilityRule, ...]: ...


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityStack:
    runtime_gateway: RuntimeCapabilityGateway
    policy_gateway: DefaultRuntimeCapabilityPolicyGateway
    harness_tool_gateway: HarnessToolGateway
    project_input_preparer: Callable[[TaskContext], Awaitable[TaskContext]] | None = None
    project_cache_cleanup_hook: PythonProjectTerminalHook | None = None


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
        try:
            permission_mode = await run_permission_mode(self._run_repository, request, role=role)
        except Exception:  # noqa: BLE001 - trusted run policy must fail closed.
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason="capability denied")
        if permission_mode == "read_only" and not read_only_capability_allowed(request):
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason="read_only capability denied")
        approval_required = (permission_mode is not None
                             or await self._tool_approval_required(request.tenant_id))
        extra_rules = self._extra_policy_rules(request)
        project_effect = await _project_input_supply_effect(self._run_repository, request, role=role)
        if project_effect is None:
            project_effect = await _project_verification_effect(self._run_repository, request, role=role)
        if project_effect in {PolicyEffect.DENY, PolicyEffect.REQUIRE_APPROVAL}:
            extra_rules = (*extra_rules, CapabilityRule(
                tenant_id=request.tenant_id, role=role, agent_id=request.agent_id,
                capability=request.capability, operation=request.operation,
                resource_prefix=request.resource, effect=project_effect,
            ))
        explicit_plugin_effect = _explicit_policy_effect(extra_rules, request, role)
        policy = default_capability_policy(
            request.tenant_id,
            require_approval_for_tools=approval_required,
            extra_rules=extra_rules,
        )
        policy_decision = policy.evaluate(request, role)
        if policy_decision.effect is PolicyEffect.DENY:
            return CapabilityResult(
                CapabilityStatus.DENIED,
                request.run_id,
                reason="capability denied",
            )
        if not await _run_scope_belongs_to_request_actor(self._run_repository, request):
            return CapabilityResult(
                CapabilityStatus.DENIED,
                request.run_id,
                reason="capability denied",
            )
        if explicit_plugin_effect is PolicyEffect.ALLOW and project_effect is None:
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if await _has_approved_capability_request(self._run_repository, request):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if await _has_approved_capability_scope(self._run_repository, request):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        gateway = CapabilityGateway(
            policy,
            self._approvals,
            self._run_repository,
            approval_reviewer=await self._approval_reviewer_for_request(request.tenant_id, permission_mode),
            plugin_policy_source=self._plugin_policy_rules,
        )
        return await gateway.invoke(request, role=role)

    async def _tool_approval_required(self, tenant_id: UUID) -> bool:
        if self._require_approval_for_tools is None:
            return False
        try:
            return await self._require_approval_for_tools(tenant_id)
        except Exception:  # noqa: BLE001 - approval policy must fail closed.
            return True

    async def _approval_reviewer_for_request(
        self, tenant_id: UUID, permission_mode: str | None = None,
    ) -> ApprovalReviewer | None:
        if permission_mode is not None:
            if (permission_mode == "auto_review"
                and await self._tool_approval_mode_for_tenant(tenant_id) == "auto_review"):
                return _BoundedAutoApprovalReviewer(self._approval_reviewer)
            return None
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


class _BoundedAutoApprovalReviewer:
    """Additional reviewer policy can restrict, but never widen, the safe allowlist."""

    def __init__(self, additional: ApprovalReviewer | None) -> None:
        self._additional = additional

    async def review(self, request: CapabilityRequest) -> ApprovalReviewDecision:
        if request.tool_origin != "builtin":
            return ApprovalReviewDecision(ApprovalReviewOutcome.REQUIRE_USER_APPROVAL,
                                          "auto_review", "external tool requires human approval")
        baseline = CodexAutoApprovalReviewer().review_sync(request)
        if baseline.outcome is not ApprovalReviewOutcome.ALLOW or self._additional is None:
            return baseline
        return await self._additional.review(request)


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


async def _run_scope_belongs_to_request_actor(
    repository: object,
    request: CapabilityRequest,
) -> bool:
    if not _uses_run_scoped_workspace(request):
        return True
    getter = getattr(repository, "get", None)
    if not callable(getter):
        return False
    try:
        record = getter(request.tenant_id, request.run_id)
        if isinstance(record, Awaitable):
            record = await record
    except (KeyError, LookupError, RuntimeError, TypeError, ValueError):
        return False
    return (
        getattr(record, "tenant_id", None) == request.tenant_id
        and getattr(record, "id", None) == request.run_id
        and getattr(record, "actor_id", None) == request.user_id
    )


def _uses_run_scoped_workspace(request: CapabilityRequest) -> bool:
    normalized_resource = normalize_resource(request.resource)
    run_scoped_resource = normalized_resource is not None and any(
        normalized_resource == prefix or normalized_resource.startswith(f"{prefix}/")
        for prefix in ("workspace/current", "workspace/bundle")
    )
    return run_scoped_resource or (
        normalized_resource == "generated/project.generate_zip"
        and _has_workspace_write_side_effect(request)
    )


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
            ("file", "write", "workspace/current", generated_effect),
            ("file", "create", "workspace", generated_effect),
            ("file", "create", "generated", generated_effect),
            ("command", "run", "workspace/current/project-verification", generated_effect),
            ("context", "read", "context", PolicyEffect.ALLOW),
            ("skill", "use", "skill", skill_effect),
            ("mcp", "invoke", "mcp", mcp_effect),
            ("plugin", "use", "plugin", plugin_effect),
        )
    )
    return CapabilityPolicy(default_rules + extra_rules)


async def _project_verification_effect(
    repository: object, request: CapabilityRequest, *, role: Role | None = None,
) -> PolicyEffect | None:
    resource = normalize_resource(request.resource)
    if not (request.capability == "command" and request.operation == "run"
            and resource is not None and (resource == "workspace/current/project-verification"
                or resource.startswith("workspace/current/project-verification/"))):
        return None
    python_resource = resource.startswith("workspace/current/project-verification/python/")
    if request.resource != "workspace/current/project-verification" and not python_resource:
        return PolicyEffect.DENY
    try:
        from agent_hub.capabilities.scoped_read import authorized_workspace_scope

        getter = getattr(repository, "get", None)
        if not callable(getter):
            return PolicyEffect.DENY
        record = await getter(request.tenant_id, request.run_id)
        project_id, session_id = authorized_workspace_scope(record, tenant_id=request.tenant_id, run_id=request.run_id)
        routing = getattr(record, "routing_decision", None)
        if not isinstance(routing, Mapping):
            return PolicyEffect.DENY
        permissions = routing.get("requested_permissions", ())
        if (routing.get("sandbox_profile") not in {"restricted", "workspace_write"}
            or not isinstance(permissions, (list, tuple)) or "command.run" not in permissions):
            return PolicyEffect.DENY
        from agent_hub.capabilities.generated_project_service import (
            generated_authority,
            generated_selected,
        )
        if generated_selected(record):
            authority = generated_authority(record, request.tenant_id, request.run_id)
            if role is not None and authority.role is not role:
                return PolicyEffect.DENY
            from agent_hub.capabilities.python_generated_service import (
                generated_python_approval_request,
            )
            selected = generated_python_approval_request(record, agent_id=request.agent_id,
                idempotency_key=request.idempotency_key)
            if selected is not None:
                return (PolicyEffect.REQUIRE_APPROVAL if python_resource and selected == request
                        else PolicyEffect.DENY)
            state = routing.get('generated_project_preparation')
            if python_resource or isinstance(state, Mapping) and state.get('stack') == 'python':
                return PolicyEffect.DENY
            return PolicyEffect.ALLOW if 'network.read' in permissions else PolicyEffect.REQUIRE_APPROVAL
        preparation = routing.get("project_input_preparation")
        python_preparation = isinstance(preparation, Mapping) and (
            "python_recipe" in preparation or preparation.get("schema_version") == 2)
        if python_resource:
            return _python_project_verification_effect(request, record, routing, preparation,
                project_id=project_id, session_id=session_id)
        if python_preparation:
            return PolicyEffect.DENY
        return PolicyEffect.ALLOW if "network.read" in permissions else PolicyEffect.REQUIRE_APPROVAL
    except Exception:  # noqa: BLE001 - missing authority cannot enable project execution.
        return PolicyEffect.DENY


async def _project_input_supply_effect(
    repository: object, request: CapabilityRequest, *, role: Role,
) -> PolicyEffect | None:
    resource = normalize_resource(request.resource)
    if resource is None or not (resource == 'workspace/current/project-inputs'
                               or resource.startswith('workspace/current/project-inputs/')):
        return None
    if request.resource != resource:
        return PolicyEffect.DENY
    try:
        from agent_hub.capabilities.python_input_preparation import input_supply_binding
        from agent_hub.domain.runs import RunStatus

        getter = getattr(repository, 'get', None)
        if not callable(getter):
            return PolicyEffect.DENY
        record = await getter(request.tenant_id, request.run_id)
        from agent_hub.capabilities.generated_project_service import generated_selected
        if generated_selected(record):
            from agent_hub.capabilities.python_generated_service import (
                generated_python_approval_request,
            )
            expected = generated_python_approval_request(record, agent_id=request.agent_id,
                idempotency_key=request.idempotency_key)
            if expected is None or not expected.resource.startswith('workspace/current/project-inputs/python/'):
                return PolicyEffect.DENY
        else:
            expected = input_supply_binding(record, planned=True)
        expiry = getattr(record, 'worker_lease_expires_at', None)
        actual_role = getattr(record, 'actor_role', None)
        if (not isinstance(actual_role, str) or Role(actual_role) is not role
                or getattr(record, 'status', None) is not RunStatus.RUNNING
                or not getattr(record, 'worker_id', None)
                or getattr(record, 'worker_lease_token', None) is None
                or not isinstance(expiry, datetime) or expiry <= datetime.now(UTC)
                or request.model_copy(update={'idempotency_key': expected.idempotency_key}) != expected):
            return PolicyEffect.DENY
        return PolicyEffect.REQUIRE_APPROVAL
    except Exception:  # noqa: BLE001 - malformed/missing DB authority never becomes a supply grant.
        return PolicyEffect.DENY


def _python_project_verification_effect(
    request: CapabilityRequest, record: object, routing: Mapping[str, object], preparation: object,
    *, project_id: str, session_id: str,
) -> PolicyEffect:
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    from agent_hub.capabilities.project_contracts import ManagedProjectInput

    _python_recipe_authorization(request, expected=request)
    if (getattr(record, "actor_id", None) != request.user_id
        or routing.get("execution_backend", "systemd") != "systemd"
        or type(routing.get("project_input_contract_version")) is not int
        or routing.get("project_input_contract_version") != 1
        or not isinstance(preparation, Mapping)
        or type(preparation.get("schema_version")) is not int
        or preparation.get("schema_version") != 2
        or preparation.get("phase") not in {"captured", "executing", "observed"}):
        return PolicyEffect.DENY
    ManagedProjectInput.from_payload(routing.get("project_input"))
    ProjectBaselineReference.from_payload(preparation.get("reference"))
    marker = preparation.get("python_recipe")
    if (not isinstance(marker, Mapping)
        or set(marker) != {"schema_version", "recipe_sha256", "environment_input_sha256"}
        or type(marker.get("schema_version")) is not int or marker["schema_version"] != 1
        or request.arguments != {**dict(marker), "project_id": project_id,
                                 "workspace_session_id": session_id}):
        return PolicyEffect.DENY
    # Installation is an exact run/user/recipe approval even when network.read
    # or broad generated-tool policy is allowed. Reviewer approvals are audited.
    return PolicyEffect.REQUIRE_APPROVAL


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
        run_repository=run_repository,
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
        project_input_preparer=runtime_gateway.original_project_preparer(harness_tool_gateway),
        project_cache_cleanup_hook=build_python_project_terminal_hook(
            run_repository, getattr(runtime_gateway, "_project_verification", None),
        ),
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
