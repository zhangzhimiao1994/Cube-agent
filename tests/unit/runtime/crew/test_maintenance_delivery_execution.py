"""Ordinary maintenance must not finish on text or a stale bundle."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.types import ModelResponse, TokenUsage
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, TaskContext
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    WorkspaceDeliverySequenceHarness,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
    _workspace_delivery_write_response,
)

FINAL = ModelResponse(text="Repair complete.", usage=TokenUsage(1, 1, 2))
TASK = (
    "Repair the existing project and deliver a ZIP.\n"
    "Existing workspace maintenance contract:\n"
    "Modify the necessary production files and deliver a fresh final bundle."
)


class ManifestHarness(WorkspaceDeliverySequenceHarness):
    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        result = await super().invoke(tenant_id, request, user_id=user_id, role=role)
        if request.tool_name == "workspace.bundle":
            return replace(result, payload={
                **result.payload,
                "workspace_files": tuple({"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                                          "size_bytes": len(text.encode())}
                                         for path, text in sorted(self.files.items())),
            })
        return result


def runtime(
    gateway: WorkspaceDeliverySequenceGateway, harness: ManifestHarness,
    repository: InMemoryArtifactRepository | None = None,
    *, version: int | None = 1,
) -> CrewDispatchRuntime:
    runner = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    runner._plan = runner._plan.model_copy(update={
        "steps": (runner._plan.steps[0].model_copy(update={
            "task": TASK, "workspace_maintenance_contract_version": 1 if version == 1 else 0,
        }),),
    })
    return runner


def context(version: int | None = 1) -> TaskContext:
    task = _workspace_delivery_sequence_context()
    route = dict(task.routing_decision)
    for key in ("project_scale", "project_delivery", "artifact_strategy"):
        route.pop(key, None)
    route["workspace_project_explicit"] = True
    if version is not None:
        route["workspace_maintenance_contract_version"] = version
    return task.model_copy(update={
        "request": "Repair the existing project and deliver a ZIP.", "routing_decision": route,
    })


async def test_summary_only_must_continue_to_actual_write_and_bundle() -> None:
    gateway = WorkspaceDeliverySequenceGateway((
        FINAL, _workspace_delivery_write_response("src/business.ts", "export const fixed = true;", "repair"),
        _workspace_delivery_bundle_response(), FINAL,
    ))
    harness = ManifestHarness()
    events = [event async for event in runtime(gateway, harness).run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", "workspace.bundle"]
    assert len(gateway.requests) == 4


async def test_bundle_before_modification_must_be_replaced() -> None:
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_bundle_response(),
        _workspace_delivery_write_response("src/business.ts", "export const fixed = true;", "repair"),
        FINAL, _workspace_delivery_bundle_response(), FINAL,
    ))
    harness = ManifestHarness()
    events = [event async for event in runtime(gateway, harness).run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == [
        "workspace.bundle", "workspace.write_text", "workspace.bundle",
    ]
    assert len(gateway.requests) == 5


@pytest.mark.parametrize("version", (None, 0))
async def test_legacy_route_does_not_enable_new_marker_semantics(version: int | None) -> None:
    gateway, harness = WorkspaceDeliverySequenceGateway((FINAL,)), ManifestHarness()
    events = [event async for event in runtime(gateway, harness, version=version).run(context(version))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 1 and not harness.calls


async def test_completed_maintenance_restore_does_not_repeat_paid_or_tool_calls() -> None:
    repository, harness = InMemoryArtifactRepository(), ManifestHarness()
    first = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("src/business.ts", "export const fixed = true;", "repair"),
        _workspace_delivery_bundle_response(), FINAL,
    ))
    runner, task = runtime(first, harness, repository), context()
    events = [event async for event in runner.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runner.save_checkpoint()
    calls, files = tuple(harness.calls), dict(harness.files)
    cached = WorkspaceDeliverySequenceGateway(())
    resumed = runtime(cached, harness, repository)
    await resumed.restore_checkpoint(checkpoint)
    events = [event async for event in resumed.run(task.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not cached.requests and tuple(harness.calls) == calls and harness.files == files
    saved = await resumed.save_checkpoint()
    assert saved.state["models"] == checkpoint.state["models"]
    assert saved.state["usage"] == checkpoint.state["usage"]


@pytest.mark.parametrize("version", (None, 0, True, "1", 2))
async def test_untrusted_or_legacy_route_cannot_activate_a_new_step(version: object) -> None:
    task = context()
    route = dict(task.routing_decision)
    if version is None:
        route.pop("workspace_maintenance_contract_version")
    else:
        route["workspace_maintenance_contract_version"] = version  # type: ignore[assignment]
    gateway, harness = WorkspaceDeliverySequenceGateway(()), ManifestHarness()
    with pytest.raises(RuntimeExecutionError, match="maintenance.*version"):
        async for _ in runtime(gateway, harness).run(task.model_copy(update={"routing_decision": route})):
            pass
    assert not gateway.requests and not harness.calls


async def test_repeated_summary_claim_is_bounded_without_tool_or_completion_credit() -> None:
    gateway, harness = WorkspaceDeliverySequenceGateway((FINAL, FINAL)), ManifestHarness()
    events = []
    with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
        async for event in runtime(gateway, harness).run(context()):
            events.append(event)
    assert len(gateway.requests) == 2 and not harness.calls
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
