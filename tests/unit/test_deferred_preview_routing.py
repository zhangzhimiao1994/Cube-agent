from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from agent_hub.harness.project_scale_runner import _web_preview_requested
from agent_hub.runs.service import _project_delivery_assessment


@pytest.fixture(scope="module")
def acceptance_script() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts/real_user_four_scale_acceptance.py"
    spec = importlib.util.spec_from_file_location("deferred_preview_acceptance", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
@pytest.mark.parametrize("route", ("auto", "direct", "dispatch", "hybrid", "multi_agent"))
@pytest.mark.parametrize("defer_preview", (True, False, None), ids=("deferred", "full", "default-full"))
def test_deferred_preview_preserves_delivery_routing_without_preview_gate(
    acceptance_script: ModuleType, scale: str, route: str, defer_preview: bool | None,
) -> None:
    options = {} if defer_preview is None else {"defer_preview": defer_preview}
    plan = acceptance_script.build_real_user_scale_plan(
        scale=scale, route_intent=route,
        project_id="offline-project", project_label="offline",
        conversation_id="offline-conversation", workspace_session_id="offline-session",
        **options,
    )
    assert plan.case_count == 1
    request = plan.requests[0]
    assert request.case_id == f"{scale}:{route}"
    assert request.body["mode"] == ("dispatch" if route == "multi_agent" else route)
    assert request.body["project_id"] == "offline-project"
    assert request.body["conversation_id"] == "offline-conversation"
    assert request.body["workspace_session_id"] == "offline-session"
    assert "runtime_timeout_seconds" not in request.body

    message = request.body["message"]
    assert isinstance(message, str)
    assessment = _project_delivery_assessment(message)
    assert assessment is not None
    assert assessment["project_scale"] == scale
    assert assessment["project_delivery"] == "workspace"
    assert assessment["artifact_strategy"] == "workspace_bundle"
    assert assessment["runtime_timeout_source"] == "project_scale_soft_budget"
    assert assessment["runtime_timeout_absolute_seconds"] == 3600
    preview_required = defer_preview is not True
    assert assessment["website_preview_required"] is preview_required
    assert _web_preview_requested(message) is preview_required
