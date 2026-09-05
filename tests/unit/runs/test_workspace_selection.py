from __future__ import annotations

import pytest

from agent_hub.runs.workspace import workspace_selection


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
    assert selection.requested_permissions == ("workspace.read", "network.read", "command.run")


def test_workspace_selection_defaults_to_metadata_only_without_sandbox() -> None:
    selection = workspace_selection(project_id=None, session_id="conv-test")

    assert selection.sandbox_profile == "none"
    assert selection.requested_permissions == ()
