"""Milestones derive from scoped native outcomes, never model completion claims."""

from collections.abc import Mapping
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_hub.runtime.autogen.engineering_delivery import engineering_delivery_ready
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.runtime.engineering_actions import required_engineering_action
from tests.unit.runtime.autogen.test_engineering_completion import context
from tests.unit.runtime.autogen.test_engineering_delivery import (
    RUN,
    TOOLS,
    append_result,
    fixture,
)


def inspection(ctx: TaskContext, *, existing: bool = True) -> Mapping[str, JsonValue]:
    return {"schema_version": 1, "scope": {"run_id": str(RUN),
            "tenant_id": str(ctx.tenant_id), "project_id": "p", "workspace_session_id": "s"},
            "manifests": ({"path": "package.json", "status": "observed" if existing else "absent",
                           "sha256": "f" * 64, "observed_bytes": 10},),
            "operations": {"build": "declared", "test": "declared"}, "issues": ()}


def action_context() -> TaskContext:
    return context().model_copy(update={"run_id": RUN, "routing_decision": {
        "engineering_delivery_contract_version": 1, "engineering_action_version": 1,
        "project_id": "p", "workspace_session_id": "s", "sandbox_profile": "workspace_write",
    }})


def test_existing_source_can_verify_bundle_without_unnecessary_writes() -> None:
    from agent_hub.runtime.autogen.engineering_delivery import engineering_progress

    ctx = action_context()
    rows, artifacts = fixture()
    passed, bundled = artifacts[1].content["result"], artifacts[2].content["result"]
    assert isinstance(passed, Mapping) and isinstance(bundled, Mapping)
    rows.clear()
    artifacts.clear()
    tools = {"engineer": (*TOOLS["engineer"], "project.inspect")}
    progress = engineering_progress(ctx, tool_entries=rows, artifacts=artifacts, allowed_tools=tools)
    assert required_engineering_action(progress) == "project.inspect"
    append_result(rows, artifacts, "project.inspect", inspection(ctx))
    progress = engineering_progress(ctx, tool_entries=rows, artifacts=artifacts, allowed_tools=tools)
    assert progress.existing_project and required_engineering_action(progress) == "project.verify"
    append_result(rows, artifacts, "project.verify", passed)
    progress = engineering_progress(ctx, tool_entries=rows, artifacts=artifacts, allowed_tools=tools,
                                    completion_proposed=True)
    assert progress.verification_current and required_engineering_action(progress) == "workspace.bundle"
    append_result(rows, artifacts, "workspace.bundle", bundled)
    assert engineering_delivery_ready(run_id=RUN, tool_entries=rows, artifacts=artifacts,
                                      allowed_tools=tools, require_write=False)
    assert not engineering_delivery_ready(run_id=RUN, tool_entries=rows, artifacts=artifacts,
                                          allowed_tools=tools)


@pytest.mark.parametrize("defect", ["tenant", "run", "project", "session", "issues", "operations", "manifest"])
def test_existing_project_requires_complete_current_scope_and_observed_declarations(defect: str) -> None:
    from agent_hub.runtime.autogen.engineering_delivery import engineering_progress

    ctx = action_context()
    value = dict(inspection(ctx))
    scope = value["scope"]
    assert isinstance(scope, Mapping)
    if defect in {"tenant", "run", "project", "session"}:
        field = {"tenant": "tenant_id", "run": "run_id", "project": "project_id", "session": "workspace_session_id"}[defect]
        value["scope"] = {**scope, field: str(uuid4())}
    elif defect == "issues":
        value["issues"] = ({"reason": "invalid"},)
    elif defect == "operations":
        value["operations"] = {"build": "unknown", "test": "declared"}
    else:
        value["manifests"] = ({"path": "package.json", "status": "absent"},)
    rows, artifacts = fixture()
    rows.clear()
    artifacts.clear()
    append_result(rows, artifacts, "project.inspect", value)
    progress = engineering_progress(ctx, tool_entries=rows, artifacts=artifacts,
                                    allowed_tools={"engineer": ("project.inspect",)})
    assert not progress.existing_project


def test_running_or_corrupt_outcome_is_uncertain_and_cannot_force_action() -> None:
    from agent_hub.runtime.autogen.engineering_delivery import engineering_progress

    rows, artifacts = fixture()
    rows[-1]["status"] = "running"
    progress = engineering_progress(action_context(), tool_entries=rows, artifacts=artifacts,
                                    allowed_tools=TOOLS, completion_proposed=True)
    assert progress.uncertain and required_engineering_action(progress) is None
    assert required_engineering_action(replace(progress, recovering=True)) is None


def test_latest_inspection_cannot_borrow_old_valid_manifest_digest() -> None:
    from agent_hub.runtime.autogen.engineering_delivery import engineering_progress

    ctx = action_context()
    rows, artifacts = fixture()
    rows.clear()
    artifacts.clear()
    append_result(rows, artifacts, "project.inspect", inspection(ctx))
    bad: dict[str, JsonValue] = {**inspection(ctx), "manifests": ({
        "path": "package.json", "status": "observed", "sha256": "bad", "observed_bytes": 10,
    },)}
    append_result(rows, artifacts, "project.inspect", bad)
    progress = engineering_progress(ctx, tool_entries=rows, artifacts=artifacts,
                                    allowed_tools={"engineer": ("project.inspect",)})
    assert not progress.existing_project
