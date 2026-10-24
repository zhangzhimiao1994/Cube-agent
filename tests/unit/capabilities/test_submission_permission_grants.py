from __future__ import annotations

from types import SimpleNamespace
from typing import Literal
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.workspace import workspace_selection


class Repository:
    def __init__(self) -> None:
        self.record = SimpleNamespace(
            id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role=Role.OPERATOR,
            status=RunStatus.RUNNING, mode=TaskMode.DIRECT,
            routing_decision={**workspace_selection(
                project_id="project", session_id="main", permission_mode="default",
            ).routing_payload(), "conversation_id": "conversation-owned"},
        )
        self.pending: list[dict[str, object]] = []

    async def get(self, tenant_id: UUID, run_id: UUID) -> SimpleNamespace:
        return self.record

    async def begin_capability_approval(self, tenant_id: UUID, run_id: UUID, **kwargs: object) -> None:
        self.pending.append(kwargs)


def request(repository: Repository, capability: str = "file", operation: str = "write",
            resource: str = "workspace/current", *, arguments: dict[str, object] | None = None,
            origin: Literal["builtin", "mcp", "plugin"] = "builtin") -> CapabilityRequest:
    return CapabilityRequest(
        tenant_id=repository.record.tenant_id, user_id=repository.record.actor_id,
        run_id=repository.record.id, agent_id="worker", capability=capability, operation=operation,
        resource=resource, arguments=arguments or {"path": "src/main.py", "content": "print(1)"},
        idempotency_key="action-one", tool_origin=origin,
    )


@pytest.mark.parametrize("capability,operation,resource", (
    ("file", "write", "workspace/current"), ("file", "create", "workspace/bundle"),
    ("file", "create", "generated/document.generate_docx"),
))
async def test_submission_authorizes_builtin_actions_within_selected_permissions(
    capability: str, operation: str, resource: str,
) -> None:
    repository = Repository()
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    result = await gateway.invoke(request(repository, capability, operation, resource), role=Role.OPERATOR)
    assert result.status is CapabilityStatus.ALLOWED
    assert repository.pending == []


@pytest.mark.parametrize("change", ("actor", "role", "tenant", "run", "status", "permissions"))
async def test_submission_authority_does_not_cross_current_identity_or_permissions(change: str) -> None:
    repository = Repository()
    selected = request(repository)
    if change in {"actor", "tenant", "run"}:
        setattr(repository.record, {"actor": "actor_id", "tenant": "tenant_id", "run": "id"}[change], uuid4())
    elif change == "role":
        repository.record.actor_role = Role.ADMIN
    elif change == "status":
        repository.record.status = RunStatus.CANCELLED
    else:
        repository.record.routing_decision["requested_permissions"] = ["workspace.read"]
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    assert (await gateway.invoke(selected, role=Role.OPERATOR)).status is not CapabilityStatus.ALLOWED


async def test_explicit_policy_deny_still_overrides_submission_authority() -> None:
    repository = Repository()

    class Deny:
        def capability_policy_rules(self, tenant_id: UUID) -> tuple[CapabilityRule, ...]:
            return (CapabilityRule(tenant_id, Role.OPERATOR, None, "file", "write",
                                   "workspace/current", PolicyEffect.DENY),)

    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository, plugin_policy_rules=Deny(),
    )
    assert (await gateway.invoke(request(repository), role=Role.OPERATOR)).status is CapabilityStatus.DENIED


@pytest.mark.parametrize("origin", ("mcp", "plugin"))
async def test_external_origin_cannot_claim_builtin_submission_authority(origin: Literal["mcp", "plugin"]) -> None:
    repository = Repository()
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    result = await gateway.invoke(request(repository, origin=origin), role=Role.OPERATOR)
    assert result.status is not CapabilityStatus.ALLOWED


@pytest.mark.parametrize("network_selected", (False, True))
async def test_project_verification_only_uses_submission_when_all_required_permissions_selected(network_selected: bool) -> None:
    repository = Repository()
    if network_selected:
        repository.record.routing_decision["requested_permissions"].append("network.read")
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    result = await gateway.invoke(request(repository, "command", "run", "workspace/current/project-verification",
        arguments={"command": ["npm", "test"]}), role=Role.OPERATOR)
    assert result.status is (CapabilityStatus.ALLOWED if network_selected else CapabilityStatus.WAITING_APPROVAL)


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID))
async def test_selected_submission_permissions_apply_to_all_modes_including_exact_consent(mode: TaskMode) -> None:
    repository = Repository()
    repository.record.mode = mode
    repository.record.routing_decision["engineering_concrete_approval_version"] = 1
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    assert (await gateway.invoke(request(repository), role=Role.OPERATOR)).status is CapabilityStatus.ALLOWED
