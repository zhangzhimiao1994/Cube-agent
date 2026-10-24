from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.conversation_approvals import conversation_approval_binding
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import RunStatus
from agent_hub.runs.workspace import workspace_selection


def example() -> tuple[SimpleNamespace, CapabilityRequest]:
    record = SimpleNamespace(
        id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role=Role.OPERATOR,
        status=RunStatus.RUNNING,
        routing_decision={**workspace_selection(
            project_id="project", session_id="main", permission_mode="default",
        ).routing_payload(), "conversation_id": "conversation-owned"},
    )
    request = CapabilityRequest(
        tenant_id=record.tenant_id, user_id=record.actor_id, run_id=record.id,
        agent_id="worker", capability="command", operation="run",
        resource="workspace/current/project-verification",
        arguments={"command": ["npm", "test"], "cwd": "."},
        idempotency_key="first", tool_origin="builtin",
    )
    return record, request


def test_same_action_across_runs_has_same_private_binding_without_raw_arguments() -> None:
    record, request = example()
    binding = conversation_approval_binding(record, request)
    assert binding is not None
    record.id = uuid4()
    assert conversation_approval_binding(
        record, request.model_copy(update={"run_id": record.id, "idempotency_key": "next"}),
    ) == binding
    assert set(binding) == {"schema_version", "context_sha256", "action_sha256"}
    assert "npm" not in str(binding)


@pytest.mark.parametrize("change", (
    "tenant", "actor", "role", "conversation", "project", "workspace", "permissions", "mode",
    "arguments", "resource", "agent", "origin",
))
def test_conversation_grant_cannot_widen_action_or_identity(change: str) -> None:
    record, request = example()
    original = conversation_approval_binding(record, request)
    assert original is not None
    if change in {"tenant", "actor"}:
        setattr(record, {"tenant": "tenant_id", "actor": "actor_id"}[change], uuid4())
    elif change == "role":
        record.actor_role = Role.ADMIN
    elif change in {"conversation", "project", "workspace", "permissions", "mode"}:
        key = {"conversation": "conversation_id", "project": "project_id",
               "workspace": "workspace_session_id", "permissions": "requested_permissions",
               "mode": "permission_mode"}[change]
        record.routing_decision[key] = ["workspace.read"] if change == "permissions" else "changed"
    else:
        field = {"arguments": "arguments", "resource": "resource", "agent": "agent_id",
                 "origin": "tool_origin"}[change]
        request = request.model_copy(update={field: {"command": ["npm", "publish"]}
                                             if change == "arguments" else "changed"})
    assert conversation_approval_binding(record, request) != original


@pytest.mark.parametrize("field", ("conversation_id", "project_id", "workspace_session_id", "permission_mode"))
def test_legacy_or_missing_scope_cannot_create_conversation_authority(field: str) -> None:
    record, request = example()
    record.routing_decision.pop(field)
    assert conversation_approval_binding(record, request) is None
