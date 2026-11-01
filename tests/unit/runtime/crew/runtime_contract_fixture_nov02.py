"""Narrow offline profile and genuine old-schema fixtures; no runtime grants."""
from __future__ import annotations

from pathlib import Path

from agent_hub.runtime.contracts import JsonValue, RuntimeCheckpoint
from agent_hub.skills.sandbox.python_project_profile import (
    PythonProjectProfileRequest,
    PythonProjectProfileResponse,
)


async def unavailable_python_profile(
    request: PythonProjectProfileRequest, *, socket_path: Path | None = None,
) -> PythonProjectProfileResponse:
    del socket_path
    assert request.invocation.install_allowed is False
    raise OSError("python_project_profile_unavailable")


def legacy_checkpoint_state(checkpoint: RuntimeCheckpoint) -> dict[str, JsonValue]:
    """Explicit pre-v11 shape, not a version label on current feature grants."""
    state = dict(checkpoint.state)
    for name in (
        "context_window_compaction_version", "native_tool_continuation",
        "workspace_productive_boundary", "workspace_context_refresh",
        "workspace_refresh_round_continuation", "workspace_feedback_retention",
        "builtin_tool_input_contract", "incomplete_recovery_output_growth",
        "first_tool_incomplete_recovery_output_growth", "incomplete_continuation_output_growth",
        "initial_incomplete_chain_growth", "required_tool_correction_version",
        "post_tool_finalization_growth_version", "workspace_maintenance_feedback_version",
    ):
        state.pop(name, None)
    state["workspace_delivery_continuation"] = False
    state.pop("workspace_meaningful_progress", None)
    return state
