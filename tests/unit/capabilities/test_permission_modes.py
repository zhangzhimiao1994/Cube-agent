from types import SimpleNamespace
from uuid import uuid4

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
from agent_hub.capabilities.types import PolicyEffect
from tests.unit.capabilities.test_defaults import (
    RUN_ID,
    TENANT_ID,
    USER_ID,
    RecordingApprovalRepository,
    request,
)
from tests.unit.capabilities.test_scoped_read import stored_run


class ModeRepository(RecordingApprovalRepository):
    def __init__(self, mode: str | None, *, actor=USER_ID, profile=None) -> None:
        super().__init__()
        route = {} if mode is None else {"permission_mode": mode}
        route.update(sandbox_profile=profile or ("read_only" if mode == "read_only" else "workspace_write"),
                     requested_permissions=["workspace.read"])
        stored = stored_run(tenant=TENANT_ID, run=RUN_ID, actor_id=actor, **route)
        self.record = SimpleNamespace(tenant_id=stored.tenant_id, id=stored.id, actor_id=stored.actor_id,
                                      actor_role=Role.OPERATOR, routing_decision=stored.routing_decision)

    async def get(self, tenant_id, run_id):
        return self.record


async def auto_mode(_tenant):
    return "auto_review"


async def ask_mode(_tenant):
    return "ask"


async def approval_disabled(_tenant):
    return False


@pytest.mark.parametrize("mode,tenant_mode,expected", [
    ("default", auto_mode, CapabilityStatus.WAITING_APPROVAL),
    ("auto_review", auto_mode, CapabilityStatus.ALLOWED),
    ("auto_review", ask_mode, CapabilityStatus.WAITING_APPROVAL),
])
async def test_run_mode_selects_real_reviewer(mode, tenant_mode, expected):
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
    capability, operation, resource, gateway_kind,
):
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
async def test_explicit_modes_never_cross_run_owner(mode):
    repository = ModeRepository(mode, actor=uuid4())
    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository, tool_approval_mode=auto_mode)
    result = await gateway.invoke(request("file", "create", "generated/document.generate_docx"),
                                  role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED


async def test_read_only_allows_workspace_read():
    repository = ModeRepository("read_only")
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    assert (await gateway.invoke(request("file", "read", "workspace/current"),
                                 role=Role.OPERATOR)).status is CapabilityStatus.ALLOWED


async def test_legacy_ignores_model_permission_mode():
    repository = ModeRepository(None)
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    selected = request("file", "create", "generated/document.generate_docx").model_copy(
        update={"arguments": {"permission_mode": "read_only"}})
    assert (await gateway.invoke(selected, role=Role.OPERATOR)).status is CapabilityStatus.ALLOWED


@pytest.mark.parametrize("mode,profile", [("full_access", "none"), ("read_only", "workspace_write")])
async def test_invalid_persisted_modes_fail_closed(mode, profile):
    repository = ModeRepository(mode, profile=profile)
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    assert (await gateway.invoke(request("file", "read", "workspace/current"),
                                 role=Role.OPERATOR)).status is CapabilityStatus.DENIED


@pytest.mark.parametrize("identity", ["tenant_id", "id", "actor_id"])
async def test_base_gateway_rejects_cross_identity_policy(identity):
    repository = ModeRepository("read_only")
    setattr(repository.record, identity, uuid4())
    selected = request("file", "read", "workspace/current")
    policy = CapabilityPolicy((CapabilityRule(TENANT_ID, Role.OPERATOR, None,
        "file", "read", "workspace", PolicyEffect.ALLOW),))
    gateway = CapabilityGateway(policy, ApprovalService(InMemoryApprovalStore()), repository)
    assert (await gateway.invoke(selected, role=Role.OPERATOR)).status is CapabilityStatus.DENIED


async def test_auto_mode_never_overrides_explicit_deny_or_supply_admission():
    repository = ModeRepository("auto_review")

    class DenyRules:
        def capability_policy_rules(self, tenant_id):
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


async def test_read_only_ignores_preexisting_write_scope():
    from agent_hub.capabilities.approvals import capability_approval_scope

    repository = ModeRepository("read_only")
    selected = request("file", "write", "workspace/current")
    repository.approved_scopes.add(capability_approval_scope(selected))
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    assert (await gateway.invoke(selected, role=Role.OPERATOR)).status is CapabilityStatus.DENIED


@pytest.mark.parametrize("gateway_kind", ["base", "default"])
async def test_read_only_requires_persisted_actor_role(gateway_kind):
    repository = ModeRepository("read_only")
    repository.record.actor_role = Role.ADMIN
    policy = CapabilityPolicy((CapabilityRule(TENANT_ID, Role.OPERATOR, None,
        "file", "read", "workspace", PolicyEffect.ALLOW),))
    approvals = ApprovalService(InMemoryApprovalStore())
    gateway = (CapabilityGateway(policy, approvals, repository) if gateway_kind == "base" else
               DefaultRuntimeCapabilityPolicyGateway(approvals, repository))
    assert (await gateway.invoke(request("file", "read", "workspace/current"),
                                 role=Role.OPERATOR)).status is CapabilityStatus.DENIED


async def test_auto_review_preserves_stricter_injected_reviewer():
    repository = ModeRepository("auto_review")

    class DenyReviewer:
        async def review(self, selected):
            return ApprovalReviewDecision(ApprovalReviewOutcome.DENY, "restricted", "policy denied")

    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository,
        tool_approval_mode=auto_mode, approval_reviewer=DenyReviewer())
    assert (await gateway.invoke(request("file", "create", "generated/document.generate_docx"),
                                 role=Role.OPERATOR)).status is CapabilityStatus.DENIED


@pytest.mark.parametrize("origin", ["plugin", "mcp"])
async def test_harness_read_only_rejects_external_file_read_disguise(origin):
    from agent_hub.harness.tool_gateway import HarnessToolGateway
    from agent_hub.harness.types import HarnessToolCallRequest

    class External:
        executed = False

        def is_available(self, tenant_id, name):
            return name == "external.reader"

        def capability_policy_parts(self, tenant_id, name):
            return "file", "read", "workspace/current"

        def sandbox_profile(self, tenant_id, name):
            return "local_process"

        async def invoke(self, **kwargs):
            self.executed = True
            return {"result": "SIDE EFFECT"}

    class Backend:
        def is_available(self, tenant_id, name):
            return False

    external = External()
    repository = ModeRepository("read_only")
    policy = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    gateway = HarnessToolGateway(Backend(), policy_gateway=policy,
        **({"plugin_backend": external} if origin == "plugin" else {"mcp_backend": external}))
    result = await gateway.invoke(TENANT_ID, HarnessToolCallRequest(
        run_id=RUN_ID, actor="researcher", tool_name="external.reader",
        arguments={"readOnlyHint": True, "permission_mode": "default", "tool_origin": "builtin"},
        approval_required=False, sandbox="local_process" if origin == "plugin" else "mcp_stdio",
        idempotency_key="external-read-disguise"), user_id=USER_ID, role=Role.OPERATOR)
    assert result.status == "failed"
    assert external.executed is False
