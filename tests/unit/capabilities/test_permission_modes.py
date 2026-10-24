from collections.abc import Awaitable, Callable, Mapping
from types import SimpleNamespace
from typing import NotRequired, TypedDict, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import (
    ApprovalReviewDecision,
    ApprovalReviewOutcome,
    CapabilityGateway,
    CapabilityStatus,
)
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.domain.runs import RunStatus
from agent_hub.harness.tool_gateway import RuntimeToolBackend
from agent_hub.runtime.contracts import JsonValue
from tests.unit.capabilities.test_defaults import (
    RUN_ID,
    TENANT_ID,
    USER_ID,
    RecordingApprovalRepository,
    request,
)
from tests.unit.capabilities.test_scoped_read import stored_run


class ModeRouting(TypedDict):
    permission_mode: NotRequired[str]
    sandbox_profile: str
    requested_permissions: list[str]


class ModeRepository(RecordingApprovalRepository):
    def __init__(self, mode: str | None, *, actor: UUID = USER_ID, profile: str | None = None) -> None:
        super().__init__()
        route: ModeRouting = {
            "sandbox_profile": profile or ("read_only" if mode == "read_only" else "workspace_write"),
            "requested_permissions": ["workspace.read"],
        }
        if mode is not None:
            route["permission_mode"] = mode
        stored = stored_run(tenant=TENANT_ID, run=RUN_ID, actor_id=actor, **route)
        self.record = SimpleNamespace(tenant_id=stored.tenant_id, id=stored.id, actor_id=stored.actor_id,
                                      actor_role=Role.OPERATOR, status=RunStatus.RUNNING,
                                      routing_decision=stored.routing_decision)

    async def get(self, tenant_id: UUID, run_id: UUID) -> SimpleNamespace:
        return self.record


async def auto_mode(_tenant: UUID) -> str:
    return "auto_review"


async def ask_mode(_tenant: UUID) -> str:
    return "ask"


async def approval_disabled(_tenant: UUID) -> bool:
    return False


@pytest.mark.parametrize("mode,tenant_mode,expected", [
    ("default", auto_mode, CapabilityStatus.WAITING_APPROVAL),
    ("auto_review", auto_mode, CapabilityStatus.ALLOWED),
    ("auto_review", ask_mode, CapabilityStatus.WAITING_APPROVAL),
])
async def test_run_mode_selects_real_reviewer(
    mode: str, tenant_mode: Callable[[UUID], Awaitable[str]], expected: CapabilityStatus,
) -> None:
    repository = ModeRepository(mode)
    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository,
        require_approval_for_tools=approval_disabled, tool_approval_mode=tenant_mode,
    )
    result = await gateway.invoke(request("file", "create", "generated/document.generate_docx"),
                                  role=Role.OPERATOR)
    assert result.status is expected
    assert bool(repository.review_records) is (expected is CapabilityStatus.ALLOWED)


@pytest.mark.parametrize("capability,operation,resource", [
    ("file", "write", "workspace/current"),
    ("file", "create", "generated/document.generate_docx"),
    ("command", "run", "workspace/current/project-verification"),
    ("skill", "use", "skill/demo"),
    ("mcp", "invoke", "mcp/reader"),
    ("plugin", "use", "plugin/reader"),
])
@pytest.mark.parametrize("gateway_kind", ["default", "base"])
async def test_read_only_denies_side_effects_before_policy_and_grants(
    capability: str, operation: str, resource: str, gateway_kind: str,
) -> None:
    repository = ModeRepository("read_only")
    selected = request(capability, operation, resource).model_copy(
        update={"arguments": {"permission_mode": "auto_review", "readOnlyHint": True}})
    policy = CapabilityPolicy((CapabilityRule(TENANT_ID, Role.OPERATOR, None,
        capability, operation, resource, PolicyEffect.ALLOW),))
    approvals = ApprovalService(InMemoryApprovalStore())
    gateway = (CapabilityGateway(policy, approvals, repository) if gateway_kind == "base" else
               DefaultRuntimeCapabilityPolicyGateway(approvals, repository,
                   tool_approval_mode=auto_mode))
    result = await gateway.invoke(selected, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED
    assert repository.pending_approvals == 0
    assert repository.review_records == []


@pytest.mark.parametrize("mode", ["default", "auto_review", "read_only"])
async def test_explicit_modes_never_cross_run_owner(mode: str) -> None:
    repository = ModeRepository(mode, actor=uuid4())
    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository, tool_approval_mode=auto_mode)
    result = await gateway.invoke(request("file", "create", "generated/document.generate_docx"),
                                  role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED


async def test_read_only_allows_workspace_read() -> None:
    repository = ModeRepository("read_only")
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    assert (await gateway.invoke(request("file", "read", "workspace/current"),
                                 role=Role.OPERATOR)).status is CapabilityStatus.ALLOWED


async def test_legacy_ignores_model_permission_mode() -> None:
    repository = ModeRepository(None)
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    selected = request("file", "create", "generated/document.generate_docx").model_copy(
        update={"arguments": {"permission_mode": "read_only"}})
    assert (await gateway.invoke(selected, role=Role.OPERATOR)).status is CapabilityStatus.ALLOWED


@pytest.mark.parametrize("mode,profile", [("full_access", "none"), ("read_only", "workspace_write")])
async def test_invalid_persisted_modes_fail_closed(mode: str, profile: str) -> None:
    repository = ModeRepository(mode, profile=profile)
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    assert (await gateway.invoke(request("file", "read", "workspace/current"),
                                 role=Role.OPERATOR)).status is CapabilityStatus.DENIED


@pytest.mark.parametrize("identity", ["tenant_id", "id", "actor_id"])
async def test_base_gateway_rejects_cross_identity_policy(identity: str) -> None:
    repository = ModeRepository("read_only")
    setattr(repository.record, identity, uuid4())
    selected = request("file", "read", "workspace/current")
    policy = CapabilityPolicy((CapabilityRule(TENANT_ID, Role.OPERATOR, None,
        "file", "read", "workspace", PolicyEffect.ALLOW),))
    gateway = CapabilityGateway(policy, ApprovalService(InMemoryApprovalStore()), repository)
    assert (await gateway.invoke(selected, role=Role.OPERATOR)).status is CapabilityStatus.DENIED


async def test_auto_mode_never_overrides_explicit_deny_or_supply_admission() -> None:
    repository = ModeRepository("auto_review")

    class DenyRules:
        def capability_policy_rules(self, tenant_id: UUID) -> tuple[CapabilityRule, ...]:
            return (CapabilityRule(tenant_id, Role.OPERATOR, None, "file", "create", "generated",
                                   PolicyEffect.DENY),)

    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository,
        tool_approval_mode=auto_mode, plugin_policy_rules=DenyRules())
    for selected in (
        request("file", "create", "generated/document.generate_docx"),
        request("command", "run", "workspace/current/project-inputs/toolchain/" + "a" * 64),
        request("command", "run", "workspace/current/project-verification/go/" + "a" * 64),
    ):
        assert (await gateway.invoke(selected, role=Role.OPERATOR)).status is CapabilityStatus.DENIED
    assert repository.review_records == []


async def test_read_only_ignores_preexisting_write_scope() -> None:
    from agent_hub.capabilities.approvals import capability_approval_scope

    repository = ModeRepository("read_only")
    selected = request("file", "write", "workspace/current")
    scope = capability_approval_scope(selected)
    assert scope is not None
    repository.approved_scopes.add(scope)
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    assert (await gateway.invoke(selected, role=Role.OPERATOR)).status is CapabilityStatus.DENIED


@pytest.mark.parametrize("gateway_kind", ["base", "default"])
async def test_read_only_requires_persisted_actor_role(gateway_kind: str) -> None:
    repository = ModeRepository("read_only")
    repository.record.actor_role = Role.ADMIN
    policy = CapabilityPolicy((CapabilityRule(TENANT_ID, Role.OPERATOR, None,
        "file", "read", "workspace", PolicyEffect.ALLOW),))
    approvals = ApprovalService(InMemoryApprovalStore())
    gateway = (CapabilityGateway(policy, approvals, repository) if gateway_kind == "base" else
               DefaultRuntimeCapabilityPolicyGateway(approvals, repository))
    assert (await gateway.invoke(request("file", "read", "workspace/current"),
                                 role=Role.OPERATOR)).status is CapabilityStatus.DENIED


async def test_auto_review_preserves_stricter_injected_reviewer() -> None:
    repository = ModeRepository("auto_review")

    class DenyReviewer:
        async def review(self, selected: CapabilityRequest) -> ApprovalReviewDecision:
            return ApprovalReviewDecision(ApprovalReviewOutcome.DENY, "restricted", "policy denied")

    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository,
        tool_approval_mode=auto_mode, approval_reviewer=DenyReviewer())
    assert (await gateway.invoke(request("file", "create", "generated/document.generate_docx"),
                                 role=Role.OPERATOR)).status is CapabilityStatus.DENIED


@pytest.mark.parametrize("origin", ["plugin", "mcp"])
async def test_harness_read_only_rejects_external_file_read_disguise(origin: str) -> None:
    from agent_hub.harness.tool_gateway import HarnessToolGateway
    from agent_hub.harness.types import HarnessToolCallRequest

    class External:
        executed = False

        def is_available(self, tenant_id: UUID, name: str) -> bool:
            return name == "external.reader"

        def capability_policy_parts(self, tenant_id: UUID, name: str) -> tuple[str, str, str]:
            return "file", "read", "workspace/current"

        def sandbox_profile(self, tenant_id: UUID, name: str) -> str:
            return "local_process"

        async def invoke(self, **kwargs: object) -> Mapping[str, JsonValue]:
            self.executed = True
            return {"result": "SIDE EFFECT"}

    class Backend:
        def is_available(self, tenant_id: UUID, name: str) -> bool:
            return False

    external = External()
    repository = ModeRepository("read_only")
    policy = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    gateway = HarnessToolGateway(cast(RuntimeToolBackend, Backend()), policy_gateway=policy,
        plugin_backend=external if origin == "plugin" else None,
        mcp_backend=external if origin != "plugin" else None)
    result = await gateway.invoke(TENANT_ID, HarnessToolCallRequest(
        run_id=RUN_ID, actor="researcher", tool_name="external.reader",
        arguments={"readOnlyHint": True, "permission_mode": "default", "tool_origin": "builtin"},
        approval_required=False, sandbox="local_process" if origin == "plugin" else "mcp_stdio",
        idempotency_key="external-read-disguise"), user_id=USER_ID, role=Role.OPERATOR)
    assert result.status == "failed"
    assert external.executed is False
