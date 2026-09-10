from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRecord
from agent_hub.runs.service import _submitted
from agent_hub.runs.workspace import workspace_selection

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")


def test_workspace_selection_normalizes_project_and_session_segments() -> None:
    selection = workspace_selection(
        project_id="Mofang Agent",
        project_label="魔方 Agent",
        session_id="Conv Main 01",
        sandbox_profile="workspace_write",
        requested_permissions=("workspace.read", "workspace.write"),
    )

    assert selection.project_id == "mofang-agent"
    assert selection.project_label == "魔方 Agent"
    assert selection.session_id == "conv-main-01"
    assert selection.session_path == "projects/mofang-agent/sessions/conv-main-01"
    assert selection.artifacts_path == "projects/mofang-agent/sessions/conv-main-01/artifacts"
    assert selection.requested_permissions == ("workspace.read", "workspace.write")


def test_workspace_selection_rejects_path_escape_segments() -> None:
    with pytest.raises(ValueError, match="project_id"):
        workspace_selection(project_id="../other", session_id="conv-test")


def test_workspace_selection_rejects_permissions_outside_sandbox_profile() -> None:
    with pytest.raises(ValueError, match="workspace.write"):
        workspace_selection(
            project_id="demo",
            session_id="conv-test",
            sandbox_profile="read_only",
            requested_permissions=("workspace.write",),
        )


def test_workspace_selection_defaults_permissions_from_sandbox_profile() -> None:
    selection = workspace_selection(project_id=None, session_id="conv-test", sandbox_profile="restricted")

    assert selection.project_id == "default"
    assert selection.requested_permissions == ("workspace.read", "command.run")


def test_workspace_selection_defaults_to_workspace_write_without_network() -> None:
    selection = workspace_selection(project_id=None, session_id="conv-test")

    assert selection.sandbox_profile == "workspace_write"
    assert selection.requested_permissions == ("workspace.read", "workspace.write", "command.run")


def test_workspace_selection_requires_explicit_network_permission() -> None:
    selection = workspace_selection(
        project_id=None,
        session_id="conv-test",
        sandbox_profile="workspace_write",
        requested_permissions=("workspace.read", "workspace.write", "network.read"),
    )

    assert selection.requested_permissions == ("workspace.read", "workspace.write", "network.read")


def test_submitted_projection_reconciles_persisted_permissions_with_sandbox_profile() -> None:
    submitted = _submitted(
        RunRecord(
            id=uuid4(),
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            request="inspect workspace",
            mode=TaskMode.DISPATCH,
            status=RunStatus.QUEUED,
            version=1,
            created_at=datetime.now(UTC),
            routing_decision={
                "sandbox_profile": "read_only",
                "requested_permissions": ["workspace.write", "workspace.read"],
            },
        )
    )

    assert submitted.sandbox_profile == "read_only"
    assert submitted.requested_permissions == ("workspace.read",)


def test_submitted_projection_drops_unknown_persisted_permissions_without_profile() -> None:
    submitted = _submitted(
        RunRecord(
            id=uuid4(),
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            request="inspect workspace",
            mode=TaskMode.DISPATCH,
            status=RunStatus.QUEUED,
            version=1,
            created_at=datetime.now(UTC),
            routing_decision={
                "requested_permissions": ["workspace.read", "credential.dump"],
            },
        )
    )

    assert submitted.sandbox_profile is None
    assert submitted.requested_permissions == ("workspace.read",)
