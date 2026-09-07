from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import (
    CodexAutoApprovalReviewer,
    DefaultRuntimeCapabilityPolicyGateway,
    build_runtime_capability_stack,
    default_capability_policy,
)
from agent_hub.capabilities.gateway import (
    ApprovalReviewDecision,
    ApprovalReviewOutcome,
    CapabilityGateway,
    CapabilityStatus,
)
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.tools.registry import ToolRegistry
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.runtime.contracts import JsonValue

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
GENERATED_ZIP_SCOPE = (
    "capability-scope:v1:"
    "tenant=11111111-1111-4111-8111-111111111111:"
    "user=22222222-2222-4222-8222-222222222222:"
    "run=33333333-3333-4333-8333-333333333333:"
    "capability=file:"
    "operation=create:"
    "resource=generated/project.generate_zip"
)


def request(capability: str, operation: str, resource: str) -> CapabilityRequest:
    return CapabilityRequest(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        agent_id="researcher",
        capability=capability,
        operation=operation,
        resource=resource,
        idempotency_key=f"{capability}:{operation}:{resource}",
        run_id=RUN_ID,
    )


def capability_request(
    *,
    agent_id: str,
    resource: str = "generated/project.generate_zip",
    run_id: UUID = RUN_ID,
    arguments: dict[str, object] | None = None,
) -> CapabilityRequest:
    return CapabilityRequest(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        agent_id=agent_id,
        capability="file",
        operation="create",
        resource=resource,
        arguments={"filename": f"{agent_id}.zip"} if arguments is None else arguments,
        idempotency_key=f"{run_id}:{agent_id}:{resource}",
        run_id=run_id,
    )


def test_runtime_capability_stack_passes_tool_registry_to_manifest(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    registry.register(
        "mcp.search",
        object(),
        kind="mcp",
        adapter="mcp_server",
        permission_class="mcp.call",
        sandbox_profile="remote_connector",
        replay_safe=False,
        aliases=("search_web",),
    )

    stack = build_runtime_capability_stack(
        tenant_id=TENANT_ID,
        run_repository=object(),
        skill_store_dir=tmp_path / "skills",
        workspace_root=None,
        tool_registry=registry,
    )

    manifest = stack.runtime_gateway.capability_manifest(TENANT_ID)
    manifest_items = cast(tuple[Mapping[str, JsonValue], ...], manifest["capabilities"])
    capabilities = {item["id"]: item for item in manifest_items}
    assert capabilities["mcp.search"]["adapter"] == "mcp_server"
    assert capabilities["mcp.search"]["available"] is True
    assert capabilities["mcp.search"]["aliases"] == ("search_web",)


class TenantAwareReplaySafePluginSource:
    def __init__(self) -> None:
        self.tenants: list[UUID] = []

    def manifests_for_tenant(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        self.tenants.append(tenant_id)
        return {
            "schema_version": 1,
            "capabilities": (
                {
                    "id": "calendar.create_event",
                    "kind": "plugin",
                    "adapter": "plugin_runtime",
                    "permission_class": "calendar.write",
                    "sandbox_profile": "remote_connector",
                    "available": True,
                    "availability_reason": None,
                    "replay_safe": True,
                    "aliases": ("calendar_create",),
                },
            ),
        }


def test_runtime_capability_stack_uses_tenant_for_plugin_replay_safe(
    tmp_path: Path,
) -> None:
    source = TenantAwareReplaySafePluginSource()
    stack = build_runtime_capability_stack(
        tenant_id=TENANT_ID,
        run_repository=object(),
        skill_store_dir=tmp_path / "skills",
        workspace_root=None,
        tool_registry=source,
    )

    assert stack.runtime_gateway.is_replay_safe("calendar.create_event") is True
    assert stack.runtime_gateway.is_replay_safe("calendar_create") is True
    assert source.tenants == [TENANT_ID, TENANT_ID]


class ScopeApprovedRepository:
    def __init__(self) -> None:
        self.pending_approvals = 0
        self.approved_scopes: set[str] = set()

    async def is_capability_approval_approved(
        self,
        _tenant_id: UUID,
        _run_id: UUID,
        _approval_fingerprint: str,
    ) -> bool:
        return False

    async def is_capability_approval_scope_approved(
        self,
        _tenant_id: UUID,
        _run_id: UUID,
        approval_scope: str,
    ) -> bool:
        return approval_scope in self.approved_scopes

    async def begin_capability_approval(
        self,
        _tenant_id: UUID,
        _run_id: UUID,
        *,
        approval_id: str,
        approval_fingerprint: str,
        approval_scope: str | None = None,
    ) -> object:
        del approval_id, approval_fingerprint, approval_scope
        self.pending_approvals += 1
        return object()


class RecordingApprovalRepository(ScopeApprovedRepository):
    def __init__(self) -> None:
        super().__init__()
        self.approvals: list[dict[str, str | None]] = []
        self.review_records: list[dict[str, str | None]] = []

    async def begin_capability_approval(
        self,
        _tenant_id: UUID,
        _run_id: UUID,
        *,
        approval_id: str,
        approval_fingerprint: str,
        approval_scope: str | None = None,
    ) -> object:
        self.pending_approvals += 1
        self.approvals.append(
            {
                "approval_id": approval_id,
                "approval_fingerprint": approval_fingerprint,
                "approval_scope": approval_scope,
            }
        )
        return object()

    async def record_capability_approval_review(
        self,
        _tenant_id: UUID,
        _run_id: UUID,
        *,
        approval_id: str,
        approval_fingerprint: str,
        reviewer: str,
        reason: str,
        status: str,
    ) -> object:
        self.review_records.append(
            {
                "approval_id": approval_id,
                "approval_fingerprint": approval_fingerprint,
                "reviewer": reviewer,
                "reason": reason,
                "status": status,
            }
        )
        return object()


class StaticApprovalReviewer:
    def __init__(self, decision: ApprovalReviewDecision) -> None:
        self.decision = decision
        self.requests: list[CapabilityRequest] = []

    async def review(self, request: CapabilityRequest) -> ApprovalReviewDecision:
        self.requests.append(request)
        return self.decision


class FailingApprovalReviewer:
    async def review(self, request: CapabilityRequest) -> ApprovalReviewDecision:
        del request
        raise RuntimeError("raw reviewer failure with secret token")


async def approval_required(_tenant_id: UUID) -> bool:
    return True


async def ask_approval_mode(_tenant_id: UUID) -> str:
    return "ask"


def test_default_capability_policy_allows_safe_runtime_tools_for_operators() -> None:
    policy = default_capability_policy(TENANT_ID)

    calculator = policy.evaluate(request("calculator", "evaluate", "calculator"), Role.OPERATOR)
    read = policy.evaluate(request("file", "read", "workspace/docs/a.md"), Role.OPERATOR)
    create = policy.evaluate(
        request("file", "create", "generated/document.generate_docx"), Role.OPERATOR
    )
    project = policy.evaluate(
        request("file", "create", "generated/project.generate_zip"), Role.OPERATOR
    )
    skill = policy.evaluate(request("skill", "use", "skill/docx"), Role.OPERATOR)
    mcp = policy.evaluate(request("mcp", "invoke", "mcp/search/web_search"), Role.OPERATOR)
    plugin = policy.evaluate(request("plugin", "use", "plugin/calendar/create_event"), Role.OPERATOR)

    assert calculator.effect is PolicyEffect.ALLOW
    assert read.effect is PolicyEffect.ALLOW
    assert create.effect is PolicyEffect.ALLOW
    assert project.effect is PolicyEffect.ALLOW
    assert skill.effect is PolicyEffect.ALLOW
    assert mcp.effect is PolicyEffect.ALLOW
    assert plugin.effect is PolicyEffect.ALLOW


def test_default_capability_policy_denies_viewer_runtime_tools() -> None:
    policy = default_capability_policy(TENANT_ID)

    decision = policy.evaluate(request("calculator", "evaluate", "calculator"), Role.VIEWER)

    assert decision.effect is PolicyEffect.DENY


async def test_default_runtime_capability_stack_authorizes_request_tenant(
    tmp_path: Path,
) -> None:
    stack = build_runtime_capability_stack(
        tenant_id=TENANT_ID,
        run_repository=object(),
        skill_store_dir=tmp_path,
        workspace_root=tmp_path,
    )
    request_tenant = UUID("44444444-4444-4444-8444-444444444444")

    result = await stack.harness_tool_gateway.invoke(
        request_tenant,
        HarnessToolCallRequest(
            run_id=RUN_ID,
            actor="researcher",
            tool_name="calculator",
            arguments={"expression": "2+3"},
            approval_required=False,
            sandbox="none",
            idempotency_key="tenant-default-policy",
        ),
        user_id=uuid4(),
        role=Role.OPERATOR,
    )

    assert result.status == "succeeded"
    assert result.payload == {"value": "5"}


async def test_default_runtime_capability_stack_wires_generated_artifact_store(
    tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    stack = build_runtime_capability_stack(
        tenant_id=TENANT_ID,
        run_repository=object(),
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path / "workspace",
        generated_artifact_dir=generated_dir,
    )

    result = await stack.harness_tool_gateway.invoke(
        TENANT_ID,
        HarnessToolCallRequest(
            run_id=RUN_ID,
            actor="document_writer",
            tool_name="document.generate_docx",
            arguments={"title": "Delivery Plan"},
            approval_required=False,
            sandbox="restricted",
            idempotency_key="docx-stack",
        ),
        user_id=uuid4(),
        role=Role.OPERATOR,
    )

    assert result.status == "succeeded"
    file_payload = result.payload["file"]
    assert isinstance(file_payload, Mapping)
    assert file_payload["filename"] == "delivery-plan.docx"
    assert "storage_key" not in file_payload
    metadata = result.payload["metadata"]
    assert isinstance(metadata, Mapping)
    assert isinstance(metadata["storage_key"], str)
    assert (generated_dir / metadata["storage_key"]).is_file()


async def test_default_runtime_stack_auto_reviews_replay_safe_generated_artifact(
    tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    stack = build_runtime_capability_stack(
        tenant_id=TENANT_ID,
        run_repository=RecordingApprovalRepository(),
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path / "workspace",
        generated_artifact_dir=generated_dir,
        require_approval_for_tools=approval_required,
    )

    result = await stack.harness_tool_gateway.invoke(
        TENANT_ID,
        HarnessToolCallRequest(
            run_id=RUN_ID,
            actor="engineer",
            tool_name="project.generate_zip",
            arguments={"title": "Hello World", "files": {"main.py": "print('hello')\n"}},
            approval_required=True,
            sandbox="workspace_write",
            idempotency_key="project-zip-auto-review",
        ),
        user_id=USER_ID,
        role=Role.OPERATOR,
    )

    assert result.status == "succeeded"
    file_payload = result.payload["file"]
    assert isinstance(file_payload, Mapping)
    assert file_payload["filename"] == "hello-world.zip"


async def test_default_runtime_stack_can_use_ask_mode_for_generated_artifact_approval(
    tmp_path: Path,
) -> None:
    repository = RecordingApprovalRepository()
    stack = build_runtime_capability_stack(
        tenant_id=TENANT_ID,
        run_repository=repository,
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path / "workspace",
        generated_artifact_dir=tmp_path / "generated",
        require_approval_for_tools=approval_required,
        tool_approval_mode=ask_approval_mode,
    )

    result = await stack.harness_tool_gateway.invoke(
        TENANT_ID,
        HarnessToolCallRequest(
            run_id=RUN_ID,
            actor="engineer",
            tool_name="project.generate_zip",
            arguments={"title": "Hello World", "files": {"main.py": "print('hello')\n"}},
            approval_required=True,
            sandbox="workspace_write",
            idempotency_key="project-zip-ask-mode",
        ),
        user_id=USER_ID,
        role=Role.OPERATOR,
    )

    assert result.status == "failed"
    assert result.failure_reason == "capability requires approval"
    assert repository.pending_approvals == 1


async def test_auto_review_does_not_allow_project_zip_workspace_side_effect(
    tmp_path: Path,
) -> None:
    repository = RecordingApprovalRepository()
    workspace_dir = tmp_path / "project-workspaces"
    stack = build_runtime_capability_stack(
        tenant_id=TENANT_ID,
        run_repository=repository,
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path / "workspace",
        generated_artifact_dir=tmp_path / "generated",
        project_workspace_dir=workspace_dir,
        require_approval_for_tools=approval_required,
    )

    result = await stack.harness_tool_gateway.invoke(
        TENANT_ID,
        HarnessToolCallRequest(
            run_id=RUN_ID,
            actor="engineer",
            tool_name="project.generate_zip",
            arguments={
                "title": "Hello World",
                "files": {"main.py": "print('hello')\n"},
                "project_id": "project-main",
                "workspace_session_id": "session-main",
            },
            approval_required=True,
            sandbox="workspace_write",
            idempotency_key="project-zip-workspace-side-effect",
        ),
        user_id=USER_ID,
        role=Role.OPERATOR,
    )

    assert result.status == "failed"
    assert result.failure_reason == "capability requires approval"
    assert repository.pending_approvals == 1
    assert not workspace_dir.exists()


async def test_generated_file_scope_approval_reuses_run_level_tool_consent() -> None:
    repository = ScopeApprovedRepository()
    first_request = capability_request(agent_id="engineer")
    second_request = capability_request(agent_id="reviewer", arguments={"filename": "final.zip"})
    repository.approved_scopes.add(GENERATED_ZIP_SCOPE)
    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()),
        repository,
        require_approval_for_tools=approval_required,
    )

    result = await gateway.invoke(second_request, role=Role.OPERATOR)

    assert result.status is CapabilityStatus.ALLOWED
    assert repository.pending_approvals == 0
    assert first_request.run_id == second_request.run_id


async def test_generated_file_scope_approval_does_not_authorize_other_tools_or_runs() -> None:
    repository = ScopeApprovedRepository()
    repository.approved_scopes.add(GENERATED_ZIP_SCOPE)
    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()),
        repository,
        require_approval_for_tools=approval_required,
    )

    other_run = await gateway.invoke(
        capability_request(
            agent_id="reviewer",
            run_id=UUID("44444444-4444-4444-8444-444444444444"),
        ),
        role=Role.OPERATOR,
    )
    skill = await gateway.invoke(
        CapabilityRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            agent_id="reviewer",
            capability="skill",
            operation="use",
            resource="skill/project-packager",
            arguments={"name": "project-packager"},
            idempotency_key="skill:project-packager",
            run_id=RUN_ID,
        ),
        role=Role.OPERATOR,
    )

    assert other_run.status is CapabilityStatus.WAITING_APPROVAL
    assert skill.status is CapabilityStatus.WAITING_APPROVAL


async def test_pending_generated_file_scope_reuses_one_approval_id_before_user_decides() -> None:
    repository = ScopeApprovedRepository()
    policy = CapabilityPolicy(
        (
            CapabilityRule(
                tenant_id=TENANT_ID,
                role=Role.OPERATOR,
                agent_id=None,
                capability="file",
                operation="create",
                resource_prefix="generated",
                effect=PolicyEffect.REQUIRE_APPROVAL,
            ),
        )
    )
    gateway = CapabilityGateway(policy, ApprovalService(InMemoryApprovalStore()), repository)

    first = await gateway.invoke(capability_request(agent_id="engineer"), role=Role.OPERATOR)
    second = await gateway.invoke(
        capability_request(agent_id="reviewer", arguments={"filename": "review.zip"}),
        role=Role.OPERATOR,
    )

    assert first.status is CapabilityStatus.WAITING_APPROVAL
    assert second.status is CapabilityStatus.WAITING_APPROVAL
    assert second.approval_id == first.approval_id


async def test_auto_review_can_allow_replay_safe_generated_artifact_without_pending_approval() -> None:
    repository = RecordingApprovalRepository()
    reviewer = StaticApprovalReviewer(
        ApprovalReviewDecision(
            outcome=ApprovalReviewOutcome.ALLOW,
            reviewer="auto_review",
            reason="reviewed bounded generated artifact",
        )
    )
    gateway = CapabilityGateway(
        CapabilityPolicy(
            (
                CapabilityRule(
                    tenant_id=TENANT_ID,
                    role=Role.OPERATOR,
                    agent_id=None,
                    capability="file",
                    operation="create",
                    resource_prefix="generated",
                    effect=PolicyEffect.REQUIRE_APPROVAL,
                ),
            )
        ),
        ApprovalService(InMemoryApprovalStore()),
        repository,
        approval_reviewer=reviewer,
    )

    result = await gateway.invoke(capability_request(agent_id="engineer"), role=Role.OPERATOR)

    assert result.status is CapabilityStatus.ALLOWED
    assert result.reason == "approved by auto_review"
    assert result.review is not None
    assert result.review.outcome is ApprovalReviewOutcome.ALLOW
    assert repository.pending_approvals == 0
    assert repository.review_records == [
        {
            "approval_id": repository.review_records[0]["approval_id"],
            "approval_fingerprint": repository.review_records[0]["approval_fingerprint"],
            "reviewer": "auto_review",
            "reason": "reviewed bounded generated artifact",
            "status": "approved",
        }
    ]
    assert len(reviewer.requests) == 1


async def test_auto_review_defers_unknown_required_capability_to_user_approval() -> None:
    repository = RecordingApprovalRepository()
    reviewer = StaticApprovalReviewer(
        ApprovalReviewDecision(
            outcome=ApprovalReviewOutcome.REQUIRE_USER_APPROVAL,
            reviewer="auto_review",
            reason="not in deterministic allowlist",
        )
    )
    gateway = CapabilityGateway(
        CapabilityPolicy(
            (
                CapabilityRule(
                    tenant_id=TENANT_ID,
                    role=Role.OPERATOR,
                    agent_id=None,
                    capability="skill",
                    operation="use",
                    resource_prefix="skill",
                    effect=PolicyEffect.REQUIRE_APPROVAL,
                ),
            )
        ),
        ApprovalService(InMemoryApprovalStore()),
        repository,
        approval_reviewer=reviewer,
    )

    result = await gateway.invoke(
        CapabilityRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            agent_id="reviewer",
            capability="skill",
            operation="use",
            resource="skill/project-packager",
            arguments={"name": "project-packager"},
            idempotency_key="skill:project-packager",
            run_id=RUN_ID,
        ),
        role=Role.OPERATOR,
    )

    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert result.reason == "capability requires approval"
    assert result.review is not None
    assert result.review.outcome is ApprovalReviewOutcome.REQUIRE_USER_APPROVAL
    assert repository.pending_approvals == 1
    approval_fingerprint = repository.approvals[0]["approval_fingerprint"]
    assert approval_fingerprint is not None
    assert approval_fingerprint not in repr(result.review)


async def test_auto_review_can_deny_required_capability_without_pending_approval() -> None:
    repository = RecordingApprovalRepository()
    reviewer = StaticApprovalReviewer(
        ApprovalReviewDecision(
            outcome=ApprovalReviewOutcome.DENY,
            reviewer="auto_review",
            reason="deterministic reviewer denied request",
        )
    )
    gateway = CapabilityGateway(
        CapabilityPolicy(
            (
                CapabilityRule(
                    tenant_id=TENANT_ID,
                    role=Role.OPERATOR,
                    agent_id=None,
                    capability="skill",
                    operation="use",
                    resource_prefix="skill",
                    effect=PolicyEffect.REQUIRE_APPROVAL,
                ),
            )
        ),
        ApprovalService(InMemoryApprovalStore()),
        repository,
        approval_reviewer=reviewer,
    )

    result = await gateway.invoke(
        CapabilityRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            agent_id="reviewer",
            capability="skill",
            operation="use",
            resource="skill/project-packager",
            arguments={"name": "project-packager"},
            idempotency_key="skill:deny",
            run_id=RUN_ID,
        ),
        role=Role.OPERATOR,
    )

    assert result.status is CapabilityStatus.DENIED
    assert result.reason == "deterministic reviewer denied request"
    assert repository.pending_approvals == 0
    assert repository.review_records == [
        {
            "approval_id": repository.review_records[0]["approval_id"],
            "approval_fingerprint": repository.review_records[0]["approval_fingerprint"],
            "reviewer": "auto_review",
            "reason": "deterministic reviewer denied request",
            "status": "denied",
        }
    ]


async def test_auto_review_failure_fails_closed_without_raw_error_details() -> None:
    repository = RecordingApprovalRepository()
    gateway = CapabilityGateway(
        CapabilityPolicy(
            (
                CapabilityRule(
                    tenant_id=TENANT_ID,
                    role=Role.OPERATOR,
                    agent_id=None,
                    capability="skill",
                    operation="use",
                    resource_prefix="skill",
                    effect=PolicyEffect.REQUIRE_APPROVAL,
                ),
            )
        ),
        ApprovalService(InMemoryApprovalStore()),
        repository,
        approval_reviewer=FailingApprovalReviewer(),
    )

    result = await gateway.invoke(
        CapabilityRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            agent_id="reviewer",
            capability="skill",
            operation="use",
            resource="skill/project-packager",
            arguments={"name": "project-packager"},
            idempotency_key="skill:review-failure",
            run_id=RUN_ID,
        ),
        role=Role.OPERATOR,
    )

    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert result.review is not None
    assert result.review.reason == "approval reviewer unavailable"
    assert "secret token" not in repr(result)
    assert repository.pending_approvals == 1


def test_approval_review_decision_rejects_unbounded_or_unprintable_text() -> None:
    with pytest.raises(ValueError, match="reviewer"):
        ApprovalReviewDecision(
            outcome=ApprovalReviewOutcome.DENY,
            reviewer="auto_review\nsecret",
            reason="blocked",
        )
    with pytest.raises(ValueError, match="reason"):
        ApprovalReviewDecision(
            outcome=ApprovalReviewOutcome.DENY,
            reviewer="auto_review",
            reason="x" * 257,
        )


def test_codex_auto_review_allows_only_bounded_builtin_generated_artifacts() -> None:
    reviewer = CodexAutoApprovalReviewer()

    generated_zip = reviewer.review_sync(capability_request(agent_id="engineer"))
    workspace_zip = reviewer.review_sync(
        capability_request(
            agent_id="engineer",
            arguments={
                "filename": "engineer.zip",
                "project_id": "project-main",
                "workspace_session_id": "session-main",
            },
        )
    )
    skill = reviewer.review_sync(
        CapabilityRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            agent_id="reviewer",
            capability="skill",
            operation="use",
            resource="skill/project-packager",
            arguments={"name": "project-packager"},
            idempotency_key="skill:project-packager",
            run_id=RUN_ID,
        )
    )

    assert generated_zip.outcome is ApprovalReviewOutcome.ALLOW
    assert generated_zip.reviewer == "auto_review"
    assert workspace_zip.outcome is ApprovalReviewOutcome.REQUIRE_USER_APPROVAL
    assert skill.outcome is ApprovalReviewOutcome.REQUIRE_USER_APPROVAL
