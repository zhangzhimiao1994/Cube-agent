"""Inspection declarations influence prompts, never execution or delivery proof."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.runtime.contracts import Artifact, JsonValue
from agent_hub.runtime.engineering_actions import EngineeringProgress
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST
from tests.unit.runtime.autogen.test_engineering_actions import action_context
from tests.unit.runtime.autogen.test_engineering_delivery import append_result


def inspection() -> dict[str, JsonValue]:
    context = action_context().model_copy(update={"tenant_id": UUID(int=1)})
    return {
        "schema_version": 1,
        "scope": {
            "tenant_id": str(context.tenant_id),
            "run_id": str(context.run_id),
            "project_id": "p",
            "workspace_session_id": "s",
        },
        "manifests": tuple(
            {
                "path": name,
                "status": "observed",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "observed_bytes": len(raw),
            }
            for name, raw in (("pyproject.toml", MANIFEST), ("requirements.lock", LOCK))
        ),
        "python_recipe": {"kind": "python_offline_wheel_v1", "status": "declared"},
        "operations": {"build": "declared", "test": "declared"},
        "issues": (),
    }


def progress(mode: str, result: Mapping[str, JsonValue]) -> EngineeringProgress:
    context = action_context().model_copy(update={"tenant_id": UUID(int=1)})
    if mode == "autogen":
        from agent_hub.runtime.autogen.engineering_delivery import engineering_progress

        rows: list[dict[str, JsonValue]] = []
        artifacts: list[Artifact] = []
        append_result(rows, artifacts, "project.inspect", result)
        return engineering_progress(
            context,
            tool_entries=rows,
            artifacts=artifacts,
            allowed_tools={"engineer": ("project.inspect",)},
        )
    from agent_hub.runtime.crew.engineering_delivery import engineering_action_progress

    source = Artifact(
        id=uuid4(),
        type="model_response",
        producer="engineer",
        content={
            "tool_calls": ({"id": "inspect", "name": "project.inspect", "arguments": {}},),
        },
    )
    digest = hashlib.sha256(b"{}").hexdigest()
    artifact = Artifact(
        id=uuid4(),
        type="tool_result",
        producer="engineer",
        source_ids=(str(source.id),),
        content={
            "agent_id": "engineer",
            "tool_name": "project.inspect",
            "arguments_sha256": digest,
            "result": result,
        },
    )
    return engineering_action_progress(
        context=context,
        step_id="build",
        actor="engineer",
        states={
            "inspect": {
                "step_id": "build",
                "attempt": 0,
                "round": 0,
                "tool_index": 0,
                "name": "project.inspect",
                "status": "succeeded",
                "arguments_sha256": digest,
                "artifact_id": str(artifact.id),
                "sha256": artifact.content_sha256,
            }
        },
        artifacts={"inspect": artifact},
        attempt=0,
        round_index=1,
    )


@pytest.mark.parametrize("mode", ["crew", "autogen"])
def test_declared_python_inspection_is_existing_without_delivery_credit(mode: str) -> None:
    value = progress(mode, inspection())
    assert value.inspected and value.existing_project
    assert not value.verification_current and not value.bundle_current and not value.has_writes


@pytest.mark.parametrize("mode", ["crew", "autogen"])
def test_node_inspection_keeps_its_existing_branch_without_python_authority(mode: str) -> None:
    result = inspection()
    result["manifests"] = (
        {"path": "package.json", "status": "observed", "sha256": "a" * 64, "observed_bytes": 10},
    )
    result["python_recipe"] = {"kind": "unsupported", "status": "unavailable"}
    value = progress(mode, result)
    assert value.inspected and value.existing_project
    assert not value.verification_current and not value.bundle_current


@pytest.mark.parametrize("mode", ["crew", "autogen"])
@pytest.mark.parametrize(
    "defect",
    [
        "recipe",
        "kind",
        "status",
        "authority",
        "manifest",
        "lock",
        "hash",
        "bool-size",
        "duplicate",
        "issues",
        "operations",
    ],
)
def test_incomplete_python_advisory_does_not_claim_an_existing_project(
    mode: str, defect: str
) -> None:
    result = inspection()
    if defect == "recipe":
        result.pop("python_recipe")
    elif defect in {"kind", "status", "authority"}:
        recipe = cast(Mapping[str, JsonValue], result["python_recipe"])
        field = "install_allowed" if defect == "authority" else defect
        value: JsonValue = (
            True
            if defect == "authority"
            else ("python_project" if defect == "kind" else "unavailable")
        )
        result["python_recipe"] = {**recipe, field: value}
    elif defect == "issues":
        result["issues"] = ({"reason": "python_recipe_inputs_invalid"},)
    elif defect == "operations":
        result["operations"] = {"build": "unknown", "test": "declared"}
    else:
        manifests = list(cast(tuple[Mapping[str, JsonValue], ...], result["manifests"]))
        if defect in {"manifest", "lock"}:
            manifests.pop(0 if defect == "manifest" else 1)
        elif defect == "duplicate":
            manifests.append(manifests[0])
        else:
            manifests[0] = {
                **manifests[0],
                "sha256" if defect == "hash" else "observed_bytes": "bad"
                if defect == "hash"
                else True,
            }
        result["manifests"] = tuple(manifests)
    observed = progress(mode, result)
    assert (
        not observed.existing_project
        and not observed.verification_current
        and not observed.bundle_current
    )
