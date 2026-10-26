"""Metadata preparation needs a live, exact plan and separate concrete consent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.defaults import (
    CodexAutoApprovalReviewer,
    DefaultRuntimeCapabilityPolicyGateway,
)
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.python_metadata_preparation import (
    METADATA_KEY,
    METADATA_PREFIX,
    metadata_preparation_binding,
)
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.domain.runs import RunStatus
from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlan
from tests.unit.capabilities.test_python_metadata_preparation import setup as state_setup

ROOT = "workspace/current/project-metadata"


async def test_auto_review_without_durable_metadata_recorder_falls_back_to_user(case: Case) -> None:
    from agent_hub.capabilities.gateway import ApprovalReviewDecision, ApprovalReviewOutcome

    class Reviewer:
        async def review(self, request: CapabilityRequest) -> ApprovalReviewDecision:
            return ApprovalReviewDecision(outcome=ApprovalReviewOutcome.ALLOW,
                reviewer='configured', reason='approved concrete catalog download')

    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()),
        case.repo, approval_reviewer=Reviewer())
    result = await gateway.invoke(case.request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert len(case.repo.waits) == 1


class Repository:
    """Substitute persistence only; policy, plan binding and approval service stay real."""

    supports_conversation_capability_approval = True

    def __init__(self, row: SimpleNamespace) -> None:
        self.row = row
        self.approved: set[str] = set()
        self.conversation_grant = False
        self.scope_grant = False
        self.checks: list[str] = []
        self.waits: list[dict[str, object]] = []

    async def get(self, tenant_id: UUID, run_id: UUID) -> SimpleNamespace:
        return self.row

    async def is_capability_approval_approved(
        self, tenant_id: UUID, run_id: UUID, fingerprint: str, *, tool_origin: str,
    ) -> bool:
        self.checks.append("exact")
        return fingerprint in self.approved and tool_origin == "builtin"

    async def is_conversation_capability_approved(
        self, request: CapabilityRequest, *, role: Role,
    ) -> bool:
        self.checks.append("conversation")
        return self.conversation_grant

    async def is_capability_approval_scope_approved(
        self, tenant_id: UUID, run_id: UUID, scope: str,
    ) -> bool:
        self.checks.append("scope")
        return self.scope_grant

    async def begin_capability_approval(
        self, tenant_id: UUID, run_id: UUID, **kwargs: object,
    ) -> None:
        self.waits.append(kwargs)

    async def resolve_capability_approval(
        self, tenant_id: UUID, run_id: UUID, status: RunStatus, *,
        approval_id: str, approval_fingerprint: str,
    ) -> None:
        assert status is RunStatus.RUNNING
        self.approved.add(approval_fingerprint)


class BroadPluginRules:
    def capability_policy_rules(self, tenant_id: UUID) -> tuple[CapabilityRule, ...]:
        return tuple(CapabilityRule(tenant_id, None, None, capability, operation,
            "workspace", PolicyEffect.ALLOW) for capability, operation in (
                ("command", "run"), ("file", "read"), ("file", "write"),
                ("plugin", "use"), ("mcp", "invoke"),
            ))


@dataclass
class Case:
    repo: Repository
    request: CapabilityRequest
    plan: PythonMetadataPlan


@pytest.fixture
def case() -> Case:
    _, row, plan = state_setup()
    repo = Repository(cast(SimpleNamespace, row))
    return Case(repo, metadata_preparation_binding(row, planned=True, live=True),
                cast(PythonMetadataPlan, plan))


def policy_for(
    repo: Repository, *, approvals: ApprovalService | None = None,
    plugin_allow: bool = False, auto_review: bool = False,
) -> DefaultRuntimeCapabilityPolicyGateway:
    async def mode(tenant_id: UUID) -> str:
        return "auto_review"

    return DefaultRuntimeCapabilityPolicyGateway(
        approvals or ApprovalService(InMemoryApprovalStore()), repo,
        plugin_policy_rules=BroadPluginRules() if plugin_allow else None,
        approval_reviewer=CodexAutoApprovalReviewer() if auto_review else None,
        tool_approval_mode=mode if auto_review else None,
    )


@pytest.mark.parametrize("mode", [None, "default", "auto_review"])
@pytest.mark.parametrize("plugin_allow", [False, True])
async def test_live_metadata_plan_requires_concrete_approval(
    case: Case, mode: str | None, plugin_allow: bool,
) -> None:
    if mode is not None:
        case.repo.row.routing_decision["permission_mode"] = mode
    result = await policy_for(case.repo, plugin_allow=plugin_allow, auto_review=True).invoke(
        case.request, role=Role.OPERATOR,
    )
    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert result.approval_id is not None
    assert case.repo.checks == ["exact"]
    assert len(case.repo.waits) == 1
    wait = case.repo.waits[0]
    assert wait["approval_fingerprint"] == fingerprint_capability_request(case.request)
    assert wait["approval_scope"] == capability_approval_scope(case.request)
    assert wait["approval_request"] == case.request
    assert wait["actor_id"] == case.plan.scope.user_id


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 1.0), ("download_count", True),
    ("catalog_count", 2.0), ("catalog_bytes", "200"), ("purpose", "install"),
    ("plan_sha256", "0" * 64), ("catalog_sha256", "0" * 64),
    ("root_config_sha256", "0" * 64), ("source_sha256", "0" * 64),
    ("project_id", "foreign"), ("workspace_session_id", "foreign"),
    ("download_bytes", 99), ("install_allowed", True), ("original_reference", {}),
    ("recipe_sha256", "0" * 64), ("urls", ["https://foreign.invalid"]),
])
async def test_arguments_require_canonical_fingerprint(
    case: Case, field: str, value: object,
) -> None:
    req = case.request.model_copy(update={"arguments": {**case.request.arguments, field: value}})
    case.repo.approved.add(fingerprint_capability_request(req))
    case.repo.conversation_grant = case.repo.scope_grant = True
    result = await policy_for(case.repo, plugin_allow=True).invoke(req, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert not case.repo.checks and not case.repo.waits


@pytest.mark.parametrize("field,value", [
    ("tenant_id", UUID(int=99)), ("run_id", UUID(int=99)), ("user_id", UUID(int=99)),
    ("agent_id", "foreign"), ("capability", "file"), ("operation", "read"),
    ("tool_origin", "plugin"), ("tool_origin", "mcp"),
])
async def test_request_identity_and_builtin_command_are_exact(
    case: Case, field: str, value: object,
) -> None:
    req = case.request.model_copy(update={field: value})
    result = await policy_for(case.repo, plugin_allow=True).invoke(req, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert not case.repo.checks and not case.repo.waits


@pytest.mark.parametrize("resource", [
    ROOT, ROOT + "/", ROOT + "/python", METADATA_PREFIX,
    METADATA_PREFIX + "a" * 63, METADATA_PREFIX + "A" * 64,
    ROOT + "/node/" + "a" * 64, METADATA_PREFIX + "a" * 64 + "/extra",
    "workspace/current/project-metadata/python/../../project-verification",
    "workspace//current/project-metadata/../project-verification",
])
async def test_unknown_and_malformed_metadata_cannot_fall_through(
    case: Case, resource: str,
) -> None:
    req = case.request.model_copy(update={"resource": resource})
    case.repo.conversation_grant = case.repo.scope_grant = True
    result = await policy_for(case.repo, plugin_allow=True).invoke(req, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert not case.repo.checks and not case.repo.waits


@pytest.mark.parametrize("alias", ["trailing", "dot", "slashes", "backslashes", "absolute"])
async def test_equivalent_resource_aliases_are_not_canonical(case: Case, alias: str) -> None:
    resource = case.request.resource
    if alias == "trailing":
        resource += "/"
    elif alias == "dot":
        resource = "./" + resource
    elif alias == "slashes":
        resource = resource.replace("/", "//")
    elif alias == "backslashes":
        resource = resource.replace("/", "\\")
    else:
        resource = "/" + resource
    req = case.request.model_copy(update={"resource": resource})
    result = await policy_for(case.repo, plugin_allow=True).invoke(req, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert not case.repo.checks and not case.repo.waits


@pytest.mark.parametrize("role", [Role.ADMIN, Role.SUPER_ADMIN, Role.VIEWER, "operator"])
async def test_role_must_be_the_current_row_role(case: Case, role: object) -> None:
    result = await policy_for(case.repo, plugin_allow=True).invoke(case.request, role=cast(Role, role))
    assert result.status is CapabilityStatus.DENIED
    assert not case.repo.checks and not case.repo.waits


@pytest.mark.parametrize("field,value", [
    ("actor_id", UUID(int=99)), ("actor_role", Role.ADMIN), ("request", "changed request"),
    ("status", RunStatus.WAITING_APPROVAL), ("status", RunStatus.COMPLETED),
    ("worker_id", None), ("worker_lease_token", None), ("worker_lease_expires_at", None),
    ("worker_lease_expires_at", datetime(2000, 1, 1, tzinfo=UTC)),
])
async def test_changed_row_and_expired_lease_deny_before_approval(
    case: Case, field: str, value: object,
) -> None:
    case.repo.approved.add(fingerprint_capability_request(case.request))
    setattr(case.repo.row, field, value)
    result = await policy_for(case.repo, plugin_allow=True).invoke(case.request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert not case.repo.checks and not case.repo.waits


@pytest.mark.parametrize("field,value", [
    ("requested_permissions", ["workspace.read"]), ("sandbox_profile", "restricted"),
    ("execution_backend", "docker"), ("project_id", "foreign"),
    ("workspace_session_id", "foreign"), ("generated_project_preparation", {}),
    ("generated_project_contract_version", 0), ("python_dependency_preflight_version", True),
    ("python_dependency_preflight_submission_version", 0),
])
async def test_current_permissions_scope_and_precapture_context_are_bound(
    case: Case, field: str, value: object,
) -> None:
    case.repo.row.routing_decision[field] = value
    case.repo.approved.add(fingerprint_capability_request(case.request))
    result = await policy_for(case.repo, plugin_allow=True).invoke(case.request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert not case.repo.checks and not case.repo.waits


@pytest.mark.parametrize("change", ["missing", "unknown", "executing", "hash", "plan"])
async def test_state_must_still_be_the_same_planned_metadata(case: Case, change: str) -> None:
    route = case.repo.row.routing_decision
    state = route[METADATA_KEY]
    if change == "missing":
        del route[METADATA_KEY]
    elif change == "unknown":
        state["phase"] = "unknown"
    elif change == "executing":
        state.update(phase="executing", execution_id="metadata_prepare_" + "a" * 32)
    elif change == "hash":
        state["plan_sha256"] = "0" * 64
    else:
        new = case.plan.model_copy(update={"fetch_timeout_seconds": 31})
        state.update(plan=new.model_dump(mode="json"), plan_sha256=new.sha256)
    case.repo.approved.add(fingerprint_capability_request(case.request))
    result = await policy_for(case.repo, plugin_allow=True).invoke(case.request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert not case.repo.checks and not case.repo.waits


@pytest.mark.parametrize("conversation,scope", [(True, False), (False, True), (True, True)])
async def test_broad_approved_grants_are_never_queried(
    case: Case, conversation: bool, scope: bool,
) -> None:
    case.repo.conversation_grant, case.repo.scope_grant = conversation, scope
    result = await policy_for(case.repo, plugin_allow=True).invoke(case.request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert case.repo.checks == ["exact"]


async def test_same_plan_exact_approval_can_be_reused(case: Case) -> None:
    service = ApprovalService(InMemoryApprovalStore())
    gateway = policy_for(case.repo, approvals=service)
    pending = await gateway.invoke(case.request, role=Role.OPERATOR)
    assert pending.status is CapabilityStatus.WAITING_APPROVAL and pending.approval_id is not None
    await service.approve(pending.approval_id, case.request, actor_id=case.request.user_id)
    case.repo.checks.clear()
    allowed = await gateway.invoke(case.request, role=Role.OPERATOR)
    assert allowed.status is CapabilityStatus.ALLOWED
    assert case.repo.checks == ["exact"] and len(case.repo.waits) == 1


async def test_only_idempotency_key_may_differ_from_live_binding(case: Case) -> None:
    retry = case.request.model_copy(update={"idempotency_key": "metadata-user-retry"})
    result = await policy_for(case.repo).invoke(retry, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert case.repo.waits[0]["approval_fingerprint"] == fingerprint_capability_request(retry)


async def test_exact_context_does_not_reuse_pending_same_scope(case: Case) -> None:
    service = ApprovalService(InMemoryApprovalStore())
    previous = case.request.model_copy(update={"idempotency_key": "older-attempt"})
    old = await service.create(previous)
    result = await policy_for(case.repo, approvals=service).invoke(case.request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert result.approval_id != old.id
    current = await service.get(cast(str, result.approval_id))
    assert current is not None
    assert current.request_fingerprint == fingerprint_capability_request(case.request)


@pytest.mark.parametrize("old_prefix", [
    "workspace/current/project-inputs/python/", "workspace/current/project-verification/python/",
    "workspace/current/project-verification/",
])
async def test_supply_install_and_command_grants_are_not_metadata_consent(
    case: Case, old_prefix: str,
) -> None:
    old = case.request.model_copy(update={"resource": old_prefix + case.plan.sha256})
    case.repo.approved.add(fingerprint_capability_request(old))
    case.repo.conversation_grant = case.repo.scope_grant = True
    result = await policy_for(case.repo).invoke(case.request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert case.repo.checks == ["exact"]


async def test_readonly_denies_metadata_without_requesting_approval(case: Case) -> None:
    case.repo.row.routing_decision.update(permission_mode="read_only", sandbox_profile="read_only",
        requested_permissions=["workspace.read"])
    result = await policy_for(case.repo).invoke(case.request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert result.reason == "read_only capability denied"
    assert not case.repo.checks and not case.repo.waits


async def test_other_resources_keep_existing_policy(case: Case) -> None:
    req = case.request.model_copy(update={"capability": "file", "operation": "read",
        "resource": "workspace/current/src/main.py", "arguments": {}})
    result = await policy_for(case.repo).invoke(req, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.ALLOWED
    assert not case.repo.waits


async def test_missing_or_failed_binding_never_creates_an_approval(case: Case) -> None:
    case.repo.row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    result = await policy_for(case.repo, plugin_allow=True).invoke(case.request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert not case.repo.checks and not case.repo.waits


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.ADMIN, Role.SUPER_ADMIN])
async def test_each_permitted_real_row_role_can_request_metadata_approval(
    case: Case, role: Role,
) -> None:
    case.repo.row.actor_role = role.value
    plan = case.plan.model_copy(update={"scope": case.plan.scope.model_copy(update={
        "actor_role": role.value,
    })})
    case.repo.row.routing_decision[METADATA_KEY].update(
        plan=plan.model_dump(mode="json"), plan_sha256=plan.sha256,
    )
    req = metadata_preparation_binding(case.repo.row, planned=True, live=True)
    result = await policy_for(case.repo).invoke(req, role=role)
    assert result.status is CapabilityStatus.WAITING_APPROVAL


@pytest.mark.parametrize("capability,operation,origin", [
    ("file", "read", "builtin"), ("file", "write", "builtin"),
    ("plugin", "use", "plugin"), ("mcp", "invoke", "mcp"),
])
async def test_metadata_namespace_cannot_use_other_tool_grants(
    case: Case, capability: str, operation: str, origin: str,
) -> None:
    req = case.request.model_copy(update={"capability": capability,
        "operation": operation, "tool_origin": origin})
    result = await policy_for(case.repo, plugin_allow=True).invoke(req, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert not case.repo.checks and not case.repo.waits


@pytest.mark.parametrize("field", ["recipe", "original_reference", "install_allowed"])
async def test_copied_foreign_fields_cannot_disappear_from_fingerprint(
    case: Case, field: str,
) -> None:
    req = case.request.model_copy(update={field: True})
    case.repo.approved.add(fingerprint_capability_request(req))
    result = await policy_for(case.repo, plugin_allow=True).invoke(req, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert not case.repo.checks and not case.repo.waits
