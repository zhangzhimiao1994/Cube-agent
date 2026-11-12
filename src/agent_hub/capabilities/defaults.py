from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
    exact_capability_approval,
    fingerprint_capability_request,
)
from agent_hub.capabilities.gateway import (
    ApprovalReviewDecision,
    ApprovalReviewer,
    ApprovalReviewOutcome,
    CapabilityGateway,
    CapabilityResult,
    CapabilityStatus,
    _is_go_recipe_request,
    _is_toolchain_supply_request,
)
from agent_hub.capabilities.permission_modes import (
    _concrete_write_policy,
    read_only_capability_allowed,
    run_permission_mode,
    run_permission_policy,
    submission_capability_allowed,
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
from agent_hub.runtime.artifacts import ArtifactRepository
from agent_hub.runtime.contracts import TaskContext

if TYPE_CHECKING:
    from agent_hub.capabilities.project_verification import ProjectVerificationService
    from agent_hub.capabilities.review_context import ConcreteModelReviewGate, _ConcreteReviewCall
    from agent_hub.runs.project_verification_receipts import ProjectVerificationReceiptRepository
    from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority

ToolApprovalPolicyGetter = Callable[[UUID], Awaitable[bool]]
ToolApprovalModeGetter = Callable[[UUID], Awaitable[str]]
ExactHumanApprovalGetter = Callable[[CapabilityRequest], Awaitable[bool]]


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
        exact_human_approval_getter: ExactHumanApprovalGetter | None = None,
    ) -> None:
        self._approvals = approvals
        self._run_repository = run_repository
        self._require_approval_for_tools = require_approval_for_tools
        self._tool_approval_mode = tool_approval_mode
        self._approval_reviewer = approval_reviewer
        self._plugin_policy_rules = plugin_policy_rules
        self._exact_human_approval_getter = exact_human_approval_getter
        from agent_hub.runs.python_recipe_review import PythonRecipeReview

        self._python_recipe_verifier: Callable[[], ProjectVerificationService | None] | None = None
        self.python_recipe_review = PythonRecipeReview(self)

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        role: Role,
    ) -> CapabilityResult:
        if _is_toolchain_supply_request(request) or _is_go_recipe_request(request):
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason='capability denied')
        return await self._invoke(request, role=role)

    async def invoke_concrete(
        self,
        request: CapabilityRequest,
        *,
        role: Role,
        review: _ConcreteReviewCall,
    ) -> CapabilityResult:
        """Only the owned actual Harness invocation supplies this private call."""
        if _is_toolchain_supply_request(request) or _is_go_recipe_request(request):
            return CapabilityResult(
                CapabilityStatus.DENIED, request.run_id, reason="capability denied"
            )

        async def current_policy() -> tuple[CapabilityPolicy, bool]:
            with review.permission_scope(request, role):
                return await self._current_review_policy(request, role)

        review.policy_reader = current_policy
        review.exact_reader = (
            (lambda: _has_exact_human_approval(self._run_repository, request))
            if self._exact_human_approval_getter is None
            else lambda: self._read_exact_human(request)
        )
        await self.python_recipe_review.review(request, role, create=True)
        try:
            policy, inbound = await current_policy()
        except Exception:  # noqa: BLE001 - unavailable current scope never grants execution.
            return CapabilityResult(
                CapabilityStatus.DENIED, request.run_id, reason="capability denied"
            )
        with review.permission_scope(request, role):
            return await CapabilityGateway(
                policy,
                self._approvals,
                self._run_repository,
                plugin_policy_source=self._plugin_policy_rules,
            ).invoke(request, role=role, concrete_review=review, submission_authorized=inbound)

    async def _read_exact_human(self, request: CapabilityRequest) -> bool:
        getter = self._exact_human_approval_getter
        return getter is not None and await getter(request) is True

    async def _current_review_policy(
        self, request: CapabilityRequest, role: Role,
    ) -> tuple[CapabilityPolicy, bool]:
        mode, _ = await run_permission_policy(self._run_repository, request, role=role)
        if mode == "read_only" and not read_only_capability_allowed(request):
            raise ValueError("capability denied")
        if not await _run_scope_belongs_to_request_actor(self._run_repository, request):
            raise ValueError("capability denied")
        required = mode is not None or await self._tool_approval_required(request.tenant_id)
        rules = self._extra_policy_rules(request)
        if mode == "default":
            # The user's manual choice is a ceiling over even explicitly eligible ASK rules.
            rules = tuple(replace(rule, review_disposition="human_only")
                if rule.effect is PolicyEffect.REQUIRE_APPROVAL else rule for rule in rules)
        effect = await _project_metadata_preparation_effect(self._run_repository, request, role=role)
        if effect is None:
            effect = await _project_input_supply_effect(self._run_repository, request, role=role)
        if effect is None:
            effect = await _project_verification_effect(self._run_repository, request, role=role)
        recipe_allowed = (effect is PolicyEffect.REQUIRE_APPROVAL
            and await self.python_recipe_review.review(request, role))
        if recipe_allowed:
            effect = PolicyEffect.ALLOW
        if effect in {PolicyEffect.DENY, PolicyEffect.REQUIRE_APPROVAL}:
            rules = (*rules, CapabilityRule(request.tenant_id, role, request.agent_id,
                request.capability, request.operation, request.resource, effect))
        inbound = recipe_allowed or (effect in {None, PolicyEffect.ALLOW}
            and await submission_capability_allowed(self._run_repository, request, role=role))
        # Suppress only the default blanket ASK for an actually authorized inbound
        # action. Explicit current ASK/DENY rules still aggregate above ALLOW.
        policy = default_capability_policy(request.tenant_id,
            require_approval_for_tools=required and not inbound,
            extra_rules=rules, concrete_model_review=mode == "auto_review")
        return policy, inbound

    async def authorize_static(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        try:
            with _concrete_write_policy(request, role):
                policy, _ = await self._current_review_policy(request, role)
            allowed = policy.evaluate(request, role).route != "deny"
        except Exception:  # noqa: BLE001 - discovery cannot establish execution authority.
            allowed = False
        return CapabilityResult(
            CapabilityStatus.ALLOWED if allowed else CapabilityStatus.DENIED,
            request.run_id,
            reason=None if allowed else "capability denied",
        )

    def _original_python_offline_action_allowed(
        self, request: CapabilityRequest, *, role: Role, permission_mode: object,
    ) -> bool:
        """Pure action policy. The caller independently fences its locked run and settings."""
        if (permission_mode != "auto_review" or self._approval_reviewer is not None or request.tool_origin != "builtin"
                or request.agent_id != "main_agent"
                or (request.capability, request.operation) != ("command", "run")
                or not request.resource.startswith("workspace/current/project-verification/python/")):
            return False
        try:
            _python_recipe_authorization(request, expected=request)
            source = self._plugin_policy_rules
            rules = () if source is None else source.capability_policy_rules(request.tenant_id)
            if not isinstance(rules, tuple) or not all(isinstance(rule, CapabilityRule) for rule in rules):
                return False
            return (default_capability_policy(request.tenant_id).evaluate(request, role).effect is PolicyEffect.ALLOW
                    and _explicit_policy_effect(rules, request, role) not in {PolicyEffect.DENY, PolicyEffect.REQUIRE_APPROVAL})
        except Exception:  # noqa: BLE001 - unavailable policy leaves the existing approval path in force.
            return False

    async def authorize_project_toolchain(
        self, request: CapabilityRequest, *, role: Role,
        authority: ToolchainSupplyAuthority | None = None,
    ) -> CapabilityResult:
        from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority

        if type(authority) is not ToolchainSupplyAuthority or not _is_toolchain_supply_request(request):
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason='capability denied')
        try:
            return await self._invoke(request, role=role, toolchain_authority=authority)
        except Exception:  # noqa: BLE001 - rejected lease fences never become an approval or grant.
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason='capability denied')

    async def authorize_project_go_recipe(
        self, request: CapabilityRequest, *, role: Role,
        authority: ToolchainSupplyAuthority | None = None,
    ) -> CapabilityResult:
        """Private admission only. SDK supply and public invoke cannot grant a Go recipe."""
        from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority

        if type(authority) is not ToolchainSupplyAuthority or not _is_go_recipe_request(request):
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason='capability denied')
        try:
            permission_mode = await run_permission_mode(self._run_repository, request, role=role)
            if permission_mode == "read_only":
                return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason="capability denied")
            rules = (*self._extra_policy_rules(request), CapabilityRule(request.tenant_id, role,
                'main_agent', 'command', 'run', request.resource, PolicyEffect.REQUIRE_APPROVAL))
            gateway = CapabilityGateway(default_capability_policy(request.tenant_id, extra_rules=rules),
                self._approvals, self._run_repository,
                approval_reviewer=await self._approval_reviewer_for_request(request.tenant_id, permission_mode),
                go_recipe_authority=authority)
            return await gateway.invoke(request, role=role)
        except Exception:  # noqa: BLE001 - a failed DB fence never grants admission.
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason='capability denied')

    async def _invoke(
        self, request: CapabilityRequest, *, role: Role,
        toolchain_authority: ToolchainSupplyAuthority | None = None,
    ) -> CapabilityResult:
        try:
            permission_mode, concrete_approval = await run_permission_policy(
                self._run_repository, request, role=role,
            )
        except Exception:  # noqa: BLE001 - trusted run policy must fail closed.
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason="capability denied")
        if permission_mode == "read_only" and not read_only_capability_allowed(request):
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason="read_only capability denied")
        approval_required = (permission_mode is not None
                             or await self._tool_approval_required(request.tenant_id))
        extra_rules = self._extra_policy_rules(request)
        metadata_effect = await _project_metadata_preparation_effect(
            self._run_repository, request, role=role,
        )
        if metadata_effect is PolicyEffect.DENY:
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason="capability denied")
        if metadata_effect is not None:
            concrete_approval = True
        project_effect: PolicyEffect | None = metadata_effect
        if project_effect is None:
            project_effect = await _project_toolchain_supply_effect(self._run_repository, request,
                role=role, authority=toolchain_authority)
        if project_effect is None:
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
        # Separate the internally added recipe ASK from current explicit plugin policy.
        # Explicit ASK/DENY must still prevent this narrow automatic review.
        recipe_plugin_effect = _explicit_policy_effect(self._extra_policy_rules(request), request, role)
        if (project_effect is PolicyEffect.REQUIRE_APPROVAL and recipe_plugin_effect is None
                and await self.python_recipe_review.review(request, role, create=True)):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if explicit_plugin_effect is PolicyEffect.ALLOW and project_effect is None:
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if (permission_mode in {"default", "auto_review"}
            and project_effect in {None, PolicyEffect.ALLOW} and explicit_plugin_effect is None
            and self._approval_reviewer is None
            and await submission_capability_allowed(self._run_repository, request, role=role)):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if toolchain_authority is None and await _has_approved_capability_request(self._run_repository, request):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        conversation_grant = getattr(self._run_repository, "is_conversation_capability_approved", None)
        if (metadata_effect is None and toolchain_authority is None and callable(conversation_grant)
            and await conversation_grant(request, role=role) is True):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if not concrete_approval and await _has_approved_capability_scope(self._run_repository, request):
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        gateway = CapabilityGateway(
            policy,
            self._approvals,
            self._run_repository,
            approval_reviewer=await self._approval_reviewer_for_request(request.tenant_id, permission_mode),
            plugin_policy_source=self._plugin_policy_rules,
            toolchain_authority=toolchain_authority,
        )
        if concrete_approval or permission_mode is not None:
            with exact_capability_approval(request):
                return await gateway.invoke(request, role=role)
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
            **({"tool_origin": request.tool_origin}
               if getattr(repository, "supports_conversation_capability_approval", False) is True else {}),
        )
    )


async def _has_exact_human_approval(repository: object, request: CapabilityRequest) -> bool:
    """Read existing manual rows only; old model upserts are not human overrides.

    The old boolean getter intentionally includes auto_review records, so it is
    unsuitable for the fresh path. This narrow read uses the same approval table.
    Alternate repositories must explicitly inject their trusted manual reader.
    """
    from sqlalchemy import select

    from agent_hub.db.models import RunApprovalRow
    from agent_hub.runs.repository import RunRepository

    if not isinstance(repository, RunRepository):
        return False
    async with repository._session_factory() as session:
        rows = (await session.scalars(select(RunApprovalRow).where(
            RunApprovalRow.tenant_id == request.tenant_id, RunApprovalRow.run_id == request.run_id,
            RunApprovalRow.action == "capability_tool", RunApprovalRow.status == "approved",
        ))).all()
        fingerprint = fingerprint_capability_request(request)
        return any(row.payload.get("approval_fingerprint") == fingerprint
            and 'automatic_review_id' not in row.payload
            and row.payload.get("decision_scope") != "conversation"
            and row.payload.get("approval_origin", request.tool_origin) == request.tool_origin
            for row in rows)


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
    concrete_model_review: bool = False,
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
            review_disposition=("model_eligible" if concrete_model_review
                and effect is PolicyEffect.REQUIRE_APPROVAL
                and (capability, operation, resource_prefix) == ("file", "write", "workspace/current")
                else None),
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


async def _project_resolution_preparation_effect(
    repository: object, request: CapabilityRequest, *, role: Role,
) -> PolicyEffect:
    """Concrete fixed-registry/publication actions; never generic command admission."""
    from datetime import timedelta

    from agent_hub.capabilities.execution_authority import (
        _trusted_review_context,
        get_verification_authority,
    )
    from agent_hub.capabilities.generated_project_service import generated_authority
    from agent_hub.capabilities.python_metadata_preparation import metadata_scope
    from agent_hub.capabilities.python_resolution_preparation import PREFIX
    from agent_hub.skills.sandbox.python_registry_protocol import (
        digest,
        require_sha,
        requirement,
        target_environment,
    )

    try:
        checked = CapabilityRequest.model_validate(request.model_dump(warnings='error'), strict=True)
        if (checked.tool_origin != 'builtin' or checked.capability != 'command' or checked.operation != 'run'
                or checked.resource != normalize_resource(checked.resource)):
            return PolicyEffect.DENY
        getter = getattr(repository, 'get', None)
        if not callable(getter):
            return PolicyEffect.DENY
        record = await getter(checked.tenant_id, checked.run_id)
        authority = get_verification_authority(tenant_id=checked.tenant_id, run_id=checked.run_id,
            user_id=checked.user_id, role=role, current_record=record)
        context, worker = _trusted_review_context(tenant_id=checked.tenant_id, run_id=checked.run_id,
            user_id=checked.user_id, role=role, current_record=record)
        if "protected_original" in checked.arguments:
            from agent_hub.capabilities.python_resolution_authorization import (
                original_resolution_scope,
                resolution_original_deadline,
                validate_resolution_authorization,
            )
            from agent_hub.runs.original_python_resolution import _approval_window
            from agent_hub.skills.sandbox.python_resolution_protocol import (
                ProtectedOriginalResolutionSource,
            )

            args = validate_resolution_authorization(checked).arguments
            source = ProtectedOriginalResolutionSource.model_validate(args["protected_original"], strict=True)
            scope = original_resolution_scope(record, actor=checked.agent_id, source=source, live=True)
            ceiling = record.created_at + timedelta(seconds=_approval_window(record))
            retained = resolution_original_deadline({**scope.model_dump(mode='json'),
                'root_sha256': args['root_sha256'], 'catalog_sha256': args['catalog_sha256'],
                'protected_original': source.model_dump(mode='json')})
            deadline = ceiling if retained is None else retained
            if (authority.actor != scope.actor or record.worker_id != worker
                    or record.worker_lease_token != authority.execution_token
                    or args["permissions_sha256"] != scope.permissions_sha256
                    or not record.created_at < deadline <= ceiling or deadline <= datetime.now(UTC)
                    or args["original_deadline"] != deadline.isoformat()):
                return PolicyEffect.DENY
            return PolicyEffect.REQUIRE_APPROVAL
        scope = metadata_scope(record, actor=checked.agent_id, live=True)
        live = generated_authority(record, checked.tenant_id, checked.run_id)
        permissions = record.routing_decision.get('requested_permissions', ())
        if (authority.actor != checked.agent_id or record.actor_role is not role
                or not {'workspace.read', 'workspace.write', 'command.run'} <= set(permissions)):
            return PolicyEffect.DENY
        args = checked.arguments
        public = {'purpose', 'key', 'source_sha256', 'root_sha256', 'catalog_sha256', 'permissions_sha256',
            'original_deadline', 'registry', 'artifact_host', 'install_allowed',
            'requirements', 'environment', 'max_wheel_bytes'}
        purpose = args.get('purpose')
        if purpose == 'python_dependency_publication':
            public |= {'plan_sha256', 'artifacts'}
        elif purpose != 'python_dependency_resolution':
            return PolicyEffect.DENY
        if set(args) != public or checked.resource != PREFIX + digest(args):
            return PolicyEffect.DENY
        for name in ('key', 'source_sha256', 'root_sha256', 'catalog_sha256', 'permissions_sha256'):
            require_sha(cast(str, args[name]))
        from agent_hub.capabilities.python_resolution_authorization import (
            resolution_original_deadline,
        )

        original_deadline = resolution_original_deadline({**scope.model_dump(mode='json'),
            'root_sha256': args['root_sha256'], 'catalog_sha256': args['catalog_sha256']})
        if original_deadline is None:
            original_deadline = record.created_at + timedelta(
                seconds=min(context.timeout_seconds, live.timeout_seconds))
        if (args['permissions_sha256'] != scope.permissions_sha256 or record.worker_id != worker
                or record.worker_lease_token != authority.execution_token
                or args['registry'] != 'https://pypi.org' or args['artifact_host'] != 'files.pythonhosted.org'
                or args['install_allowed'] is not False or type(args['max_wheel_bytes']) is not int
                or args['max_wheel_bytes'] != 64 * 1024**2
                or original_deadline.tzinfo is None
                or not record.created_at < original_deadline <= record.created_at + timedelta(seconds=live.timeout_seconds)
                or original_deadline <= datetime.now(UTC)
                or args['original_deadline'] != original_deadline.isoformat()):
            return PolicyEffect.DENY
        requirements = args['requirements']
        if type(requirements) is not list or not 0 < len(requirements) <= 128:
            return PolicyEffect.DENY
        for item in requirements:
            requirement(item)
        target_environment(cast(dict[str, str], args['environment']))
        if purpose == 'python_dependency_publication':
            require_sha(cast(str, args['plan_sha256']))
            artifacts = args['artifacts']
            if type(artifacts) is not list or not 0 < len(artifacts) <= 128:
                return PolicyEffect.DENY
            total = 0
            for entry in artifacts:
                if (type(entry) is not list or len(entry) != 3 or type(entry[0]) is not str
                        or not entry[0].endswith('-py3-none-any.whl') or '/' in entry[0] or '\\' in entry[0]
                        or type(entry[2]) is not int or entry[2] <= 0):
                    return PolicyEffect.DENY
                require_sha(entry[1])
                total += entry[2]
            if total > 64 * 1024**2:
                return PolicyEffect.DENY
        # Root revalidates private source/plan evidence before IO. Public data
        # only identifies this exact human approval, never a new execution grant.
        return PolicyEffect.REQUIRE_APPROVAL
    except Exception:  # noqa: BLE001 - no malformed/private/stale request can widen authority.
        return PolicyEffect.DENY


async def _project_metadata_preparation_effect(
    repository: object, request: CapabilityRequest, *, role: Role,
) -> PolicyEffect | None:
    resolution_prefix = "workspace/current/project-resolution"
    lexical = "/".join(part for part in request.resource.replace("\\", "/").split("/") if part not in {"", "."})
    resource = normalize_resource(request.resource)
    if any(value is not None and (value == resolution_prefix or value.startswith(resolution_prefix + "/"))
           for value in (resource, lexical)):
        return await _project_resolution_preparation_effect(repository, request, role=role)
    prefix = "workspace/current/project-metadata"
    resource = normalize_resource(request.resource)
    # Keep the lexical namespace too: traversal out of metadata must not reach
    # an older project effect or a broad plugin grant after normalization.
    lexical_resource = "/".join(part for part in request.resource.replace("\\", "/").split("/")
                                if part not in {"", "."})
    if not any(value is not None and (value == prefix or value.startswith(prefix + "/"))
               for value in (resource, lexical_resource)):
        return None
    try:
        from agent_hub.capabilities.python_metadata_preparation import (
            METADATA_PREFIX,
            metadata_preparation_binding,
        )

        if (type(request) is not CapabilityRequest or type(role) is not Role
                or resource is None or request.resource != resource
                or not resource.startswith(METADATA_PREFIX)
                or request.tool_origin != "builtin"
                or request.capability != "command" or request.operation != "run"):
            return PolicyEffect.DENY
        checked = CapabilityRequest.model_validate(vars(request), strict=True)
        getter = getattr(repository, "get", None)
        if not callable(getter):
            return PolicyEffect.DENY
        record = getter(checked.tenant_id, checked.run_id)
        if isinstance(record, Awaitable):
            record = await record
        expected = metadata_preparation_binding(record, planned=True, live=True)
        actual_role = getattr(record, "actor_role", None)
        selected = checked.model_copy(update={"idempotency_key": expected.idempotency_key})
        if (not isinstance(actual_role, str) or Role(actual_role) is not role
                or fingerprint_capability_request(selected) != fingerprint_capability_request(expected)):
            return PolicyEffect.DENY
        return PolicyEffect.REQUIRE_APPROVAL
    except Exception:  # noqa: BLE001 - uncertain live state never grants metadata preparation.
        return PolicyEffect.DENY


async def _project_toolchain_supply_effect(
    repository: object, request: CapabilityRequest, *, role: Role,
    authority: ToolchainSupplyAuthority | None,
) -> PolicyEffect | None:
    if not _is_toolchain_supply_request(request):
        return None
    if authority is None or request.resource != normalize_resource(request.resource):
        return PolicyEffect.DENY
    try:
        from agent_hub.capabilities.toolchain_preparation import toolchain_supply_binding
        from agent_hub.domain.runs import RunStatus

        getter = getattr(repository, 'get', None)
        if not callable(getter):
            return PolicyEffect.DENY
        record = await getter(request.tenant_id, request.run_id)
        authority.require_current(record, request.tenant_id, request.run_id)
        expected = toolchain_supply_binding(record, planned=True)
        expiry = getattr(record, 'worker_lease_expires_at', None)
        actual_role = getattr(record, 'actor_role', None)
        # Canonical JSON fingerprints preserve bool/int distinctions in the
        # protected capture and exact arguments; only the retry key may differ.
        selected = request.model_copy(update={'idempotency_key': expected.idempotency_key})
        if (not isinstance(actual_role, str) or Role(actual_role) is not role
                or getattr(record, 'status', None) is not RunStatus.RUNNING
                or not getattr(record, 'worker_id', None)
                or getattr(record, 'worker_lease_token', None) is None
                or not isinstance(expiry, datetime) or expiry <= datetime.now(UTC)
                or fingerprint_capability_request(selected) != fingerprint_capability_request(expected)):
            return PolicyEffect.DENY
        return PolicyEffect.REQUIRE_APPROVAL
    except Exception:  # noqa: BLE001 - stale or malformed DB state cannot authorize SDK supply.
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
    artifact_repository: ArtifactRepository | None = None,
    receipt_repository: ProjectVerificationReceiptRepository | None = None,
    review_gate: ConcreteModelReviewGate | None = None,
    review_gate_factory: Callable[[RuntimeCapabilityGateway], ConcreteModelReviewGate] | None = None,
    exact_human_approval_getter: ExactHumanApprovalGetter | None = None,
) -> RuntimeCapabilityStack:
    if review_gate is not None and review_gate_factory is not None:
        raise ValueError("configure one concrete review gate")
    runtime_gateway = RuntimeCapabilityGateway(
        skill_store_dir=skill_store_dir,
        tenant_id=tenant_id,
        workspace_root=workspace_root,
        generated_artifact_dir=generated_artifact_dir,
        project_workspace_dir=project_workspace_dir,
        run_repository=run_repository,
        tool_registry=tool_registry,
        artifact_repository=artifact_repository,
        receipt_repository=receipt_repository,
    )
    if artifact_repository is not None and runtime_gateway._project_verification is not None:
        runtime_gateway._project_verification.generated.configure_resolution_artifacts(artifact_repository)
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
        exact_human_approval_getter=exact_human_approval_getter,
    )
    policy_gateway._python_recipe_verifier = lambda: runtime_gateway._project_verification
    if runtime_gateway._project_verification is not None:
        runtime_gateway._project_verification._python.recipe_review = policy_gateway.python_recipe_review
    harness_tool_gateway = HarnessToolGateway(
        runtime_gateway,
        policy_gateway=policy_gateway,
        mcp_backend=mcp_backend,
        plugin_backend=plugin_backend,
        raise_backend_errors=True,
        review_gate=review_gate_factory(runtime_gateway) if review_gate_factory is not None else review_gate,
    )
    cleanup_hook = build_python_project_terminal_hook(
        run_repository, getattr(runtime_gateway, "_project_verification", None),
    )
    preparer = runtime_gateway.original_project_preparer(harness_tool_gateway)
    if preparer is not None and cleanup_hook is not None:
        preparer.capacity_maintenance = cleanup_hook.maintain_capacity
    return RuntimeCapabilityStack(
        runtime_gateway=runtime_gateway,
        policy_gateway=policy_gateway,
        harness_tool_gateway=harness_tool_gateway,
        project_input_preparer=preparer,
        project_cache_cleanup_hook=cleanup_hook,
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
