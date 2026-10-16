"""Offline delivery progress, fresh observations and causal checkpoint replay."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import socket
from collections.abc import Mapping
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import (
    CrewRunStream,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
)
from agent_hub.runtime.progress import artifact_progress_key
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    WorkspaceDeliverySequenceHarness,
    _workspace_delivery_bundle_response,
    _workspace_delivery_partial_checkpoint,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
    _workspace_delivery_write_response,
)

TOOLS = ("workspace.write_text", "workspace.read", "workspace.list", "workspace.bundle")
FINAL = ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2))


def test_unexecuted_provisional_tool_artifact_cannot_grant_progress() -> None:
    placeholder = Artifact(id=uuid4(), type="tool_result", producer="implementer_step",
                           content={"result": None})
    assert artifact_progress_key(placeholder) is None


@pytest.mark.parametrize("name", ("workspace.read", "workspace.list"))
def test_claimed_progress_and_new_artifact_identity_do_not_change_observation_key(name: str) -> None:
    result: dict[str, JsonValue] = (
        {"path": "src/file.ts", "text": "same", "truncated": False}
        if name == "workspace.read" else {"workspace_files": ({"path": "src/file.ts", "sha256": "a" * 64},)}
    )
    first = Artifact(id=uuid4(), type="tool_result", producer="implementer",
                     content={"tool_name": name, "arguments_sha256": "a" * 64, "result": result})
    second = Artifact(id=uuid4(), type="tool_result", producer="implementer",
                      content={"tool_name": name, "arguments_sha256": "b" * 64,
                               "result": {**result, "progress": True, "new_information": True}})
    assert artifact_progress_key(first) is not None
    assert artifact_progress_key(first) == artifact_progress_key(second)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch, _function_scoped_runner: asyncio.Runner) -> None:
    _function_scoped_runner.get_loop()

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("meaningful progress fixtures cannot use network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", denied)


def observe(name: str, path: str = "src/business.ts") -> ModelResponse:
    return ModelResponse(
        text=None,
        tool_calls=(ToolCall(id="fresh-observation", name=name,
                             arguments={"path": path} if name == "workspace.read" else {}),),
        usage=TokenUsage(1, 1, 2),
    )


def write(content: str = "export const ready = true;\n") -> ModelResponse:
    return _workspace_delivery_write_response("src/business.ts", content, "write-business")


class Observations(WorkspaceDeliverySequenceHarness):
    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        if request.tool_name not in {"workspace.read", "workspace_read", "workspace.list"}:
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        assert user_id is not None and role is Role.OPERATOR
        self.calls.append(request)
        if request.tool_name in {"workspace.read", "workspace_read"}:
            path = str(request.arguments["path"])
            result: Mapping[str, JsonValue] = {
                "path": path, "text": self.files.get(path, "export const dependency = true;\n"),
                "truncated": False,
            }
        else:
            result = {"workspace_files": tuple({"path": p, "content_bytes": len(v),
                       "content_sha256": hashlib.sha256(v.encode()).hexdigest()}
                      for p, v in sorted(self.files.items()))}
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                     status="succeeded", payload=result)


@pytest.mark.parametrize("name", ("workspace.read", "workspace.list"))
async def test_unchanged_fresh_observations_get_one_correction_then_stop(
    monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    gateway = WorkspaceDeliverySequenceGateway((write(), *(observe(name) for _ in range(12)),
                                               _workspace_delivery_bundle_response(), FINAL))
    harness = Observations()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=TOOLS)
    credits: list[float] = []
    original = adapter._tool_progress_step_deadline

    def credit(*args: object, **kwargs: object) -> float:
        deadline = original(*args, **kwargs)  # type: ignore[arg-type]
        credits.append(deadline)
        return deadline

    monkeypatch.setattr(adapter, "_tool_progress_step_deadline", credit)
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
        async for event in runtime.run(_workspace_delivery_sequence_context()):
            events.append(event)
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", name, name, name]
    assert len({call.idempotency_key for call in harness.calls[1:]}) == 3
    assert len(credits) == 2, "new causal IDs must not renew a step's deadline"
    correction = "WORKSPACE_OBSERVATION_STALL:"
    assert sum(isinstance(m.content, str) and correction in m.content
               for m in gateway.requests[-1].messages) == 1
    assert not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events)
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["timeout_progress_units"] == 2
    assert checkpoint.state["usage"] == {"tokens": 8, "cost_usd": "0"}


async def test_first_distinct_inspections_still_extend_round_budget() -> None:
    gateway = WorkspaceDeliverySequenceGateway((write(),
        *(observe("workspace.read", f"src/dependency-{i}.ts") for i in range(10)),
        _workspace_delivery_bundle_response(), FINAL))
    harness = Observations()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=TOOLS)
    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(harness.calls) == 12 and len(gateway.requests) == 13
    assert sum(c.tool_name == "workspace.read" for c in harness.calls) == 10


async def test_real_changed_write_and_observation_recover_after_correction() -> None:
    gateway = WorkspaceDeliverySequenceGateway((write("first"), observe("workspace.read"),
        observe("workspace.read"), write("changed"), observe("workspace.read"),
        _workspace_delivery_bundle_response(), FINAL))
    harness = Observations()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=TOOLS)
    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert harness.files == {"src/business.ts": "changed"}
    assert [c.tool_name for c in harness.calls].count("workspace.read") == 3
    assert len(gateway.requests) == 7


async def test_same_arguments_with_changed_read_content_are_new_information() -> None:
    class Changed(Observations):
        async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, **kwargs: object) -> HarnessToolCallResult:
            if request.tool_name == "workspace.read":
                self.files["src/business.ts"] = str(len(self.calls))
            return await super().invoke(tenant_id, request, **kwargs)  # type: ignore[arg-type]

    gateway = WorkspaceDeliverySequenceGateway((write(), *(observe("workspace.read") for _ in range(10)),
        _workspace_delivery_bundle_response(), FINAL))
    harness = Changed()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=TOOLS)
    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(harness.calls) == 12


async def test_restore_reconstructs_stall_correction_from_causal_prefix_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint, context, repository, harness, first = await _workspace_delivery_partial_checkpoint(
        responses=(write(), observe("workspace.read"), observe("workspace.read")),
        expected_tools=3, harness=Observations(), tools=TOOLS,
    )
    before = checkpoint.to_payload()
    gateway = WorkspaceDeliverySequenceGateway((observe("workspace.read"), _workspace_delivery_bundle_response(), FINAL))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository, tools=TOOLS)
    await runtime.restore_checkpoint(checkpoint)
    credits: list[object] = []
    original = adapter._tool_progress_step_deadline

    def credit(*args: object, **kwargs: object) -> float:
        credits.append(None)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(adapter, "_tool_progress_step_deadline", credit)
    with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
        async for _event in runtime.run(context.model_copy(update={"checkpoint": checkpoint})):
            pass
    assert len(first.requests) == 3 and len(gateway.requests) == 1
    assert [c.tool_name for c in harness.calls] == ["workspace.write_text", *("workspace.read" for _ in range(3))]
    assert credits == [], "hydrating the prefix must not renew step time"
    assert checkpoint.to_payload() == before
    restored = await runtime.save_checkpoint()
    assert restored.state["usage"] == {"tokens": 8, "cost_usd": "0"}
    assert restored.state["timeout_progress_units"] == 2
    for key, row in cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["models"]).items():
        assert cast(Mapping[str, Mapping[str, JsonValue]], restored.state["models"])[key] == row


@pytest.mark.parametrize("legacy", (False, True))
async def test_completed_restore_preserves_hashes_and_cached_final_without_calls(legacy: bool) -> None:
    repository = InMemoryArtifactRepository()
    harness = Observations()
    gateway = WorkspaceDeliverySequenceGateway((write(), observe("workspace.read"),
                                               _workspace_delivery_bundle_response(), FINAL))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository, tools=TOOLS)
    context = _workspace_delivery_sequence_context()
    stream = runtime.run(context)
    assert isinstance(stream, CrewRunStream)
    stream._state.workspace_meaningful_progress = not legacy
    events = [event async for event in stream]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runtime.save_checkpoint()
    if legacy:
        payload = checkpoint.to_payload()
        cast(dict[str, object], payload["state"]).pop("workspace_meaningful_progress")
        payload["state_sha256"] = ""
        checkpoint = RuntimeCheckpoint.from_payload(payload)
    before = checkpoint.to_payload()
    restored_gateway = WorkspaceDeliverySequenceGateway(())
    restored = _workspace_delivery_sequence_runtime(restored_gateway, harness,
        artifact_repository=repository, tools=TOOLS)
    await restored.restore_checkpoint(checkpoint)
    old_calls = len(harness.calls)
    recovered = [event async for event in restored.run(context.model_copy(update={"checkpoint": checkpoint}))]
    assert recovered[-1].kind is EventKind.RUNTIME_COMPLETED
    assert restored_gateway.requests == [] and len(harness.calls) == old_calls
    assert checkpoint.to_payload() == before


async def test_forged_semantic_count_is_rejected_before_gateway_or_tool() -> None:
    checkpoint, context, repository, harness, _first = await _workspace_delivery_partial_checkpoint(
        responses=(write(), observe("workspace.read")), expected_tools=2, harness=Observations(), tools=TOOLS,
    )
    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])
    state["timeout_progress_units"] = cast(int, state["timeout_progress_units"]) + 1
    payload["state_sha256"] = ""
    forged = RuntimeCheckpoint.from_payload(payload)
    gateway = WorkspaceDeliverySequenceGateway(())
    restored = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository, tools=TOOLS)
    await restored.restore_checkpoint(forged)
    old_calls = len(harness.calls)
    with pytest.raises(RuntimeExecutionError, match="checkpoint workspace progress"):
        async for _event in restored.run(context.model_copy(update={"checkpoint": forged})):
            pass
    assert gateway.requests == [] and len(harness.calls) == old_calls


async def test_stall_correction_cannot_be_reset_by_claiming_completion_without_bundle() -> None:
    gateway = WorkspaceDeliverySequenceGateway((write(), observe("workspace.read"),
        observe("workspace.read"), ModelResponse(text="Already finished.", usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_bundle_response(), FINAL))
    harness = Observations()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=TOOLS)
    with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
        async for _event in runtime.run(_workspace_delivery_sequence_context()):
            pass
    assert len(gateway.requests) == 4
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", "workspace.read", "workspace.read"]


async def test_fresh_read_alias_is_not_replaced_by_stale_semantic_cache() -> None:
    def alias() -> ModelResponse:
        return ModelResponse(text=None, tool_calls=(ToolCall(id="alias", name="workspace_read",
            arguments={"path": "src/business.ts"}),), usage=TokenUsage(1, 1, 2))

    gateway = WorkspaceDeliverySequenceGateway((write(), alias(), alias(), alias(),
                                               _workspace_delivery_bundle_response(), FINAL))
    harness = Observations()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=(*TOOLS, "workspace_read"))
    with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
        async for _event in runtime.run(_workspace_delivery_sequence_context()):
            pass
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", *("workspace.read" for _ in range(3))]


@pytest.mark.parametrize("boundary", ("prepared", "running"))
async def test_pending_model_boundary_preserves_hash_and_never_replays_successful_tools(boundary: str) -> None:
    blocked = asyncio.Event()
    saved = asyncio.Event()

    class Paused(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == 3:
                blocked.set()
                await asyncio.Event().wait()
            return await super().complete_with_context(request)

    gateway = Paused((write(), observe("workspace.read"), observe("workspace.read")))
    harness = Observations()
    repository = InMemoryArtifactRepository()
    context = _workspace_delivery_sequence_context()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository, tools=TOOLS)
    checkpoints: list[RuntimeCheckpoint] = []

    async def consume() -> None:
        async for event in runtime.run(context):
            if event.checkpoint is None:
                continue
            models = cast(Mapping[str, Mapping[str, JsonValue]], event.checkpoint.state["models"])
            if len(models) == 4 and any(row["status"] == boundary for row in models.values()):
                checkpoints.append(event.checkpoint)
                saved.set()

    consumer = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(5):
            await blocked.wait()
            await saved.wait()
    finally:
        await runtime.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    checkpoint = checkpoints[-1]
    before = checkpoint.to_payload()
    fresh = WorkspaceDeliverySequenceGateway((observe("workspace.read"),))
    restored = _workspace_delivery_sequence_runtime(fresh, harness, artifact_repository=repository, tools=TOOLS)
    await restored.restore_checkpoint(checkpoint)
    expected = RuntimeExecutionError if boundary == "prepared" else ModelOutcomeUncertain
    reason = "workspace delivery stalled" if boundary == "prepared" else "outcome requires confirmation"
    with pytest.raises(expected, match=reason):
        async for _event in restored.run(context.model_copy(update={"checkpoint": checkpoint})):
            pass
    assert len(fresh.requests) == (1 if boundary == "prepared" else 0)
    assert len(harness.calls) == (4 if boundary == "prepared" else 3)
    assert checkpoint.to_payload() == before
