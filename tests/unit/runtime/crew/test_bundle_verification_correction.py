"""Only confirmed missing-verification bundle failures can reach the model."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
from agent_hub.models.types import ModelRequest, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.crew.adapter import RuntimeExecutionError, _RunState
from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.capabilities.test_scoped_read import RUN, TENANT
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
    _workspace_delivery_write_response,
)
from tests.unit.runtime.crew.test_engineering_delivery_execution import (
    FINAL,
    ActualCapabilityHarness,
    engineering_runtime,
    verify_response,
)


class ObservedHarness(ActualCapabilityHarness):
    def __init__(self, root: Path, *, boundary: str = "envelope") -> None:
        super().__init__(root, Executor())
        self.boundary = boundary

    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        if request.tool_name == "workspace.bundle" and self.boundary == "typed":
            self.calls.append(request)
            payload = await self.backend.execute(
                tenant_id=tenant_id, run_id=request.run_id, actor=request.actor,
                name=request.tool_name, arguments=request.arguments,
                idempotency_key=request.idempotency_key,
            )
            return HarnessToolCallResult(
                call_id=request.call_id, tool_name=request.tool_name, status="succeeded", payload=payload,
            )
        result = await super().invoke(tenant_id, request, user_id=user_id, role=role)
        if request.tool_name != "workspace.bundle" or result.status != "failed":
            return result
        if self.boundary == "wrong_call":
            return replace(result, call_id="another-call")
        if self.boundary == "wrong_tool":
            return replace(result, tool_name="project.verify")
        if self.boundary == "truncated":
            return replace(result, truncated=True)
        if self.boundary == "uncertain":
            return replace(result, payload={"status": "uncertain"})
        if self.boundary == "generic":
            return replace(result, failure_reason="tool execution failed")
        return result


def context() -> TaskContext:
    return _workspace_delivery_sequence_context().model_copy(update={"tenant_id": TENANT, "run_id": RUN})


@pytest.mark.parametrize("boundary", ("envelope", "typed"))
async def test_missing_verification_can_be_corrected_without_retrying_bundle(
    tmp_path: Path, boundary: str,
) -> None:
    repository = InMemoryArtifactRepository()
    harness = ObservedHarness(tmp_path, boundary=boundary)
    model = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("index.js", "module.exports = 42;", "write"),
        _workspace_delivery_bundle_response(), verify_response(),
        _workspace_delivery_bundle_response(), FINAL,
    ))
    runner = engineering_runtime(model, harness, repository)
    task = context()
    events = [event async for event in runner.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text", "workspace.bundle", "project.verify", "workspace.bundle",
    ]
    failures = [event for event in events if event.kind is EventKind.TOOL_FAILED]
    assert len(failures) == 1 and failures[0].artifact is None
    assert failures[0].payload["status"] == "failed"
    feedback = "\n".join(message.content for message in model.requests[2].messages
                         if isinstance(message.content, str))
    assert "project_verification_required" in feedback and "project.verify" in feedback
    checkpoint = await runner.save_checkpoint()
    cached = WorkspaceDeliverySequenceGateway(())
    resumed = engineering_runtime(cached, harness, repository)
    calls = tuple(harness.calls)
    await resumed.restore_checkpoint(checkpoint)
    events = [event async for event in resumed.run(task.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not cached.requests and tuple(harness.calls) == calls


@pytest.mark.parametrize("boundary", (
    "wrong_call", "wrong_tool", "truncated", "uncertain", "generic", "exception",
))
async def test_untrusted_or_generic_failures_never_trigger_correction(
    tmp_path: Path, boundary: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = ObservedHarness(tmp_path, boundary=boundary)
    if boundary == "exception":
        async def broken(*args: object, **kwargs: object) -> None:
            raise RuntimeError("project verification required before delivery")

        monkeypatch.setattr(harness.backend, "_execute_workspace_bundle", broken)
    model = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("index.js", "module.exports = 42;", "write"),
        _workspace_delivery_bundle_response(), verify_response(),
    ))
    runner = engineering_runtime(model, harness, InMemoryArtifactRepository())
    with pytest.raises(RuntimeExecutionError, match="capability .*execution failed"):
        async for _ in runner.run(context()):
            pass
    assert len(model.requests) == 2
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", "workspace.bundle"]


async def test_legacy_missing_verification_still_terminates(tmp_path: Path) -> None:
    harness = ObservedHarness(tmp_path)
    model = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("index.js", "module.exports = 42;", "write"),
        _workspace_delivery_bundle_response(), verify_response(),
    ))
    runner = engineering_runtime(model, harness, InMemoryArtifactRepository())
    runner._plan = runner._plan.model_copy(update={"steps": (
        runner._plan.steps[0].model_copy(update={"engineering_delivery_contract_version": 0}),
    )})
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for _ in runner.run(context()):
            pass
    assert len(model.requests) == 2


async def test_repeating_unverified_bundle_stops_instead_of_blind_retry(tmp_path: Path) -> None:
    harness = ObservedHarness(tmp_path)
    model = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("index.js", "module.exports = 42;", "write"),
        _workspace_delivery_bundle_response(), _workspace_delivery_bundle_response(), verify_response(),
    ))
    runner = engineering_runtime(model, harness, InMemoryArtifactRepository())
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for _ in runner.run(context()):
            pass
    assert len(model.requests) == 3
    assert len(harness.calls) == 3


async def pause_at_bundle_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[RuntimeCheckpoint, ObservedHarness, InMemoryArtifactRepository, TaskContext]:
    harness = ObservedHarness(tmp_path)
    repository = InMemoryArtifactRepository()
    model = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("index.js", "module.exports = 42;", "write"),
        _workspace_delivery_bundle_response(),
    ))
    runner = engineering_runtime(model, harness, repository)
    saved: list[RuntimeCheckpoint] = []
    publish = runner._publish_checkpoint

    def stop(state: _RunState, checkpoint: RuntimeCheckpoint) -> None:
        publish(state, checkpoint)
        rows = checkpoint.state["tools"]
        assert isinstance(rows, Mapping)
        if any(isinstance(row, Mapping) and row.get("name") == "workspace.bundle"
               and row.get("status") == "failed" for row in rows.values()):
            saved.append(checkpoint)
            raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_publish_checkpoint", stop)
    task = context()
    with pytest.raises(asyncio.CancelledError):
        async for _ in runner.run(task):
            pass
    assert saved
    return saved[0], harness, repository, task


async def test_failed_boundary_restore_rebuilds_feedback_without_replaying_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint, harness, repository, task = await pause_at_bundle_failure(tmp_path, monkeypatch)
    model = WorkspaceDeliverySequenceGateway((verify_response(), _workspace_delivery_bundle_response(), FINAL))
    resumed = engineering_runtime(model, harness, repository)
    await resumed.restore_checkpoint(checkpoint)
    events = [event async for event in resumed.run(task.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text", "workspace.bundle", "project.verify", "workspace.bundle",
    ]
    assert "project_verification_required" in "\n".join(
        m.content for m in model.requests[0].messages if isinstance(m.content, str)
    )


@pytest.mark.parametrize("status", ("running", "uncertain", "UNKNOWN", "prepared"))
async def test_correction_marker_never_overrides_unconfirmed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    checkpoint, harness, repository, task = await pause_at_bundle_failure(tmp_path, monkeypatch)
    payload = checkpoint.to_payload()
    state = payload["state"]
    assert isinstance(state, dict) and isinstance(state["tools"], dict)
    for row in state["tools"].values():
        if row.get("name") == "workspace.bundle":
            row["status"] = status
    changed = RuntimeCheckpoint(
        id=checkpoint.id, runtime_type=checkpoint.runtime_type, runtime_version=checkpoint.runtime_version,
        run_id=checkpoint.run_id, tenant_id=checkpoint.tenant_id, mode=checkpoint.mode, state=state,
    )
    model = WorkspaceDeliverySequenceGateway(())
    resumed = engineering_runtime(model, harness, repository)
    calls = tuple(harness.calls)
    with pytest.raises(RuntimeExecutionError, match="checkpoint.*incompatible"):
        await resumed.restore_checkpoint(changed)
        async for _ in resumed.run(task.model_copy(update={"checkpoint": changed})):
            pass
    assert not model.requests and tuple(harness.calls) == calls


async def test_native_batch_marks_unexecuted_calls_and_restores_lineage(tmp_path: Path) -> None:
    class NativeGateway(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            response = completion.response
            if response.tool_calls:
                response = replace(response, native_assistant=NativeAssistantMessage(
                    "primary", None, tuple(NativeToolCall(
                        call.id, call.name.replace(".", "_"), json.dumps(dict(call.arguments)),
                    ) for call in response.tool_calls),
                ))
            return replace(completion, response=response)

    early = _workspace_delivery_bundle_response()
    batched = replace(early, tool_calls=(
        replace(early.tool_calls[0], id="premature-bundle"), ToolCall("unexecuted", "project.verify", {}),
    ))
    model = NativeGateway((
        _workspace_delivery_write_response("index.js", "module.exports = 42;", "write"),
        batched, verify_response(), _workspace_delivery_bundle_response(), FINAL,
    ))
    harness = ObservedHarness(tmp_path)
    repository = InMemoryArtifactRepository()
    runner = engineering_runtime(model, harness, repository)
    native_store = InMemoryProviderContinuationRepository()
    runner._provider_continuation_repository = native_store
    task = context()
    events = [event async for event in runner.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    feedback = [m for m in model.requests[2].messages if m.tool_call_id == "unexecuted"]
    assert len(feedback) == 1
    assert isinstance(feedback[0].content, str)
    assert json.loads(feedback[0].content.split("=", 1)[1])[0]["result"] == {"status": "not_executed"}
    assert [call.tool_name for call in harness.calls].count("project.verify") == 1
    checkpoint = await runner.save_checkpoint()
    cached = NativeGateway(())
    resumed = engineering_runtime(cached, harness, repository)
    resumed._provider_continuation_repository = native_store
    await resumed.restore_checkpoint(checkpoint)
    events = [event async for event in resumed.run(task.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED and not cached.requests
