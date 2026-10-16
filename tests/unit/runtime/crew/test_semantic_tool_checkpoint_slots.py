"""Causal sparse semantic reuse: synthetic RAM-only checkpoint regressions."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.types import ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import (
    CapabilityOutcomeUncertain,
    CrewDispatchRuntime,
    CrewRunStream,
    RuntimeExecutionError,
    _ModelLedger,
    _mutable_json,
    _ReviewLedger,
    _scope_project_workspace_tool_call,
    _ToolLedger,
)
from agent_hub.runtime.generated_file_recovery import reusable_generated_file_result
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    WorkspaceDeliverySequenceHarness,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
)
from tests.unit.runtime.crew.test_incremental_read_rejection import offline_only  # noqa: F401

pytestmark = pytest.mark.asyncio

FIRST = ToolCall(id="first", name="workspace.write_text", arguments={"path": "first.txt", "content": "synthetic first"})
SECOND = ToolCall(id="second", name="workspace.write_text", arguments={"path": "second.txt", "content": "synthetic second"})
FINAL = ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2))


class ApprovalHarness(WorkspaceDeliverySequenceHarness):
    def __init__(self) -> None:
        super().__init__()
        self.approved = False
        self.waiting_calls: list[HarnessToolCallRequest] = []

    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        if request.tool_name == "workspace.write_text" and request.arguments.get("path") == "second.txt" and not self.approved:
            assert request.approval_required is True
            self.waiting_calls.append(request)
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name, status="failed",
                payload={"approval_id": "approval_sparse_slot"}, failure_reason="capability requires approval")
        if request.tool_name in {"workspace.read", "workspace.list", "workspace_read", "project.generate_zip"}:
            self.calls.append(request)
            return HarnessToolCallResult(
                call_id=request.call_id, tool_name=request.tool_name, status="succeeded",
                payload={"text": "synthetic RAM observation"},
            )
        return await super().invoke(tenant_id, request, user_id=user_id, role=role)


@dataclass
class SparseCase:
    checkpoint: RuntimeCheckpoint
    context: TaskContext
    repository: InMemoryArtifactRepository
    gateway: WorkspaceDeliverySequenceGateway
    harness: ApprovalHarness


async def sparse_case(first: ToolCall = FIRST) -> SparseCase:
    gateway = WorkspaceDeliverySequenceGateway((
        ModelResponse(text=None, tool_calls=(first,), usage=TokenUsage(1, 1, 2)),
        ModelResponse(text=None, tool_calls=(first, SECOND), usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_bundle_response(), FINAL,
    ))
    harness = ApprovalHarness()
    repository = InMemoryArtifactRepository()
    context = _workspace_delivery_sequence_context()
    runtime = _workspace_delivery_sequence_runtime(
        gateway, harness, artifact_repository=repository,
        tools=tuple(dict.fromkeys((first.name, "workspace.write_text", "workspace.list", "workspace.bundle"))),
    )
    stream = runtime.run(context)
    assert isinstance(stream, CrewRunStream)
    checkpoint = None
    try:
        async with asyncio.timeout(5):
            async for event in stream:
                candidate = event.checkpoint
                if candidate is None:
                    continue
                tools = cast(Mapping[str, Mapping[str, JsonValue]], candidate.state["tools"])
                if any(s["status"] == "waiting_approval" for s in tools.values()):
                    checkpoint = candidate
                    break
    finally:
        await stream.aclose()
    assert checkpoint is not None
    tools = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["tools"])
    current = [s for s in tools.values() if s["round"] == 1]
    assert any(row["tool_index"] == 1 and row["status"] == "waiting_approval" for row in current)
    assert len(gateway.requests) == 2 and len(harness.waiting_calls) == 1
    if first == FIRST:
        assert len(current) == 1 and len(harness.calls) == 1
        assert harness.files == {"first.txt": "synthetic first"}
    return SparseCase(checkpoint, context, repository, gateway, harness)


async def test_cached_slot_zero_then_pending_slot_one_restores_without_duplicate_effects() -> None:
    case = await sparse_case()
    frozen = case.checkpoint.to_payload()
    case.harness.approved = True
    resumed = _workspace_delivery_sequence_runtime(case.gateway, case.harness, artifact_repository=case.repository)
    await resumed.restore_checkpoint(case.checkpoint)
    events = [event async for event in resumed.run(case.context.model_copy(update={"checkpoint": case.checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.arguments.get("path") for call in case.harness.calls if call.tool_name == "workspace.write_text"] == [
        "first.txt", "second.txt",
    ]
    second = next(call for call in case.harness.calls if call.arguments.get("path") == "second.txt")
    assert second.idempotency_key == case.harness.waiting_calls[0].idempotency_key
    assert len(case.gateway.requests) == 4
    complete = await resumed.save_checkpoint()
    assert complete.state["usage"] == {"tokens": 8, "cost_usd": "0"}
    assert case.checkpoint.to_payload() == frozen
    before = (len(case.gateway.requests), len(case.harness.calls))
    cached = _workspace_delivery_sequence_runtime(case.gateway, case.harness, artifact_repository=case.repository)
    await cached.restore_checkpoint(complete)
    assert [event async for event in cached.run(case.context.model_copy(update={"checkpoint": complete}))][-1].kind is EventKind.RUNTIME_COMPLETED
    assert (len(case.gateway.requests), len(case.harness.calls)) == before


@pytest.mark.parametrize("status", ["uncertain", "running"])
async def test_sparse_unknown_outcome_never_reinvokes_or_spends_model_usage(status: str) -> None:
    case = await sparse_case()
    payload = case.checkpoint.to_payload()
    state = cast(dict[str, JsonValue], payload["state"])
    tools = cast(dict[str, dict[str, JsonValue]], state["tools"])
    pending = next(s for s in tools.values() if s["status"] == "waiting_approval")
    pending.pop("approval_id")
    pending["status"] = status
    pending["replay_safe"] = False
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    resumed = _workspace_delivery_sequence_runtime(case.gateway, case.harness, artifact_repository=case.repository)
    await resumed.restore_checkpoint(checkpoint)
    before = (len(case.gateway.requests), len(case.harness.calls), len(case.harness.waiting_calls))
    with pytest.raises(CapabilityOutcomeUncertain):
        _ = [event async for event in resumed.run(case.context.model_copy(update={"checkpoint": checkpoint}))]
    assert (len(case.gateway.requests), len(case.harness.calls), len(case.harness.waiting_calls)) == before


@dataclass
class GraphCase:
    runtime: CrewDispatchRuntime
    context: TaskContext
    checkpoint: RuntimeCheckpoint
    artifacts: dict[str, Artifact]
    tools: _ToolLedger
    models: _ModelLedger

    def validate(self) -> None:
        self.runtime._validate_artifact_graph(
            self.runtime._plan, self.context, tuple(self.artifacts.values()), {}, {},
            self.tools, self.models, _ReviewLedger(), (), self.checkpoint.runtime_version,
            workspace_delivery_continuation=self.checkpoint.state.get("workspace_delivery_continuation") is True,
            workspace_meaningful_progress=self.checkpoint.state.get("workspace_meaningful_progress") is True,
            workspace_context_refresh=self.checkpoint.state.get("workspace_context_refresh") is True,
        )


async def graph_case(case: SparseCase, first: ToolCall = FIRST) -> GraphCase:
    registry = cast(Mapping[str, str], case.checkpoint.state["artifact_registry"])
    artifacts = await case.repository.get_many(
        case.context.tenant_id, case.context.run_id,
        tuple(ArtifactReference(UUID(key), sha) for key, sha in registry.items()),
    )
    by_id = {str(artifact.id): artifact for artifact in artifacts}
    tools = dict(cast(Mapping[str, Mapping[str, JsonValue]], case.checkpoint.state["tools"]))
    models = dict(cast(Mapping[str, Mapping[str, JsonValue]], case.checkpoint.state["models"]))
    return GraphCase(
        _workspace_delivery_sequence_runtime(
            case.gateway, case.harness, artifact_repository=case.repository,
            tools=tuple(dict.fromkeys((first.name, "workspace.write_text", "workspace.list", "workspace.bundle"))),
        ), case.context, case.checkpoint, by_id,
        _ToolLedger(states=tools, artifacts={key: by_id[cast(str, row["artifact_id"])]
                                           for key, row in tools.items() if row.get("artifact_id")}),
        _ModelLedger(states=models, artifacts={key: by_id[cast(str, row["artifact_id"])]
                                              for key, row in models.items() if row.get("artifact_id")}),
    )


def replace_calls(graph: GraphCase, calls: tuple[ToolCall, ...]) -> None:
    key = next(key for key, row in graph.models.states.items() if row["call_index"] == 1)
    original = graph.models.artifacts[key]
    content = cast(dict[str, JsonValue], _mutable_json(original.content))
    content["tool_calls"] = cast(JsonValue, [
        {"id": call.id, "name": call.name, "arguments": _mutable_json(call.arguments)} for call in calls
    ])
    changed = Artifact(
        id=original.id, version=original.version, type=original.type, producer=original.producer,
        content=content, source_ids=original.source_ids, provenance=original.provenance,
    )
    graph.models.artifacts[key] = changed
    graph.artifacts[str(changed.id)] = changed
    graph.models.states[key] = {**graph.models.states[key], "sha256": changed.content_sha256}


async def test_graph_accepts_sparse_pending_independently_of_structural_validation() -> None:
    graph = await graph_case(await sparse_case())
    graph.validate()


@pytest.mark.parametrize("prior_status", ["succeeded", "rejected", "uncertain"])
async def test_unknown_or_non_successful_gap_before_pending_is_rejected(prior_status: str) -> None:
    case = await sparse_case()
    graph = await graph_case(case)
    graph.validate()
    if prior_status == "succeeded":
        replace_calls(graph, (ToolCall(id="unseen", name=FIRST.name, arguments={"path": "never.txt", "content": "new"}), SECOND))
    else:
        key = next(key for key, row in graph.tools.states.items() if row["round"] == 0)
        graph.tools.states[key] = {**graph.tools.states[key], "status": prior_status}
    before = (len(case.gateway.requests), len(case.harness.calls))
    with pytest.raises(RuntimeExecutionError, match="checkpoint .*lineage"):
        graph.validate()
    assert (len(case.gateway.requests), len(case.harness.calls)) == before


@pytest.mark.parametrize("past_matches", [True, False])
async def test_future_matching_receipt_requires_a_consumed_matching_past(past_matches: bool) -> None:
    graph = await graph_case(await sparse_case())
    past_key = next(key for key, row in graph.tools.states.items() if row["round"] == 0)
    pending_key = next(key for key, row in graph.tools.states.items() if row["round"] == 1)
    past = graph.tools.artifacts[past_key]
    trigger = next(artifact for key, artifact in graph.models.artifacts.items()
                   if graph.models.states[key]["call_index"] == 1)
    call = FIRST if past_matches else SECOND
    replace_calls(graph, (call, call))
    arguments_sha = graph.tools.states[past_key if past_matches else pending_key]["arguments_sha256"]
    content = {**past.content, "arguments_sha256": arguments_sha}
    future = Artifact(id=uuid4(), type=past.type, producer=past.producer, content=content,
                      source_ids=(str(trigger.id),), provenance=past.provenance)
    future_state = {
        **graph.tools.states[pending_key], "status": "succeeded", "name": call.name,
        "arguments_sha256": arguments_sha,
        "artifact_id": str(future.id), "sha256": future.content_sha256,
    }
    future_state.pop("approval_id")
    # Deliberately store the later receipt first; causal order is defined by the graph, not insertion order.
    graph.tools.states = {pending_key: future_state, past_key: graph.tools.states[past_key]}
    graph.tools.artifacts = {pending_key: future, past_key: past}
    graph.artifacts[str(future.id)] = future
    if past_matches:
        graph.validate()
    else:
        with pytest.raises(RuntimeExecutionError, match="checkpoint capability artifact lineage"):
            graph.validate()


async def test_pending_slot_cannot_hide_a_later_persisted_slot() -> None:
    graph = await graph_case(await sparse_case())
    graph.validate()
    replace_calls(graph, (FIRST, SECOND, SECOND))
    pending = next(row for row in graph.tools.states.values() if row["round"] == 1)
    graph.tools.states["later-synthetic-slot"] = {**pending, "tool_index": 2}
    with pytest.raises(RuntimeExecutionError, match="checkpoint artifact graph"):
        graph.validate()


@pytest.mark.parametrize("name", ["workspace.read", "workspace.list", "workspace_read", "workspace.bundle"])
async def test_mutable_observation_or_fresh_bundle_cannot_fill_new_round_gap(name: str) -> None:
    first = ToolCall(id="observe", name=name, arguments={"path": "first.txt"} if name != "workspace.bundle" else {"title": "Synthetic"})
    case = await sparse_case(first)
    graph = await graph_case(case, first)
    graph.validate()
    key = next(key for key, row in graph.tools.states.items() if row["round"] == 1 and row["tool_index"] == 0)
    removed = graph.tools.artifacts.pop(key)
    del graph.tools.states[key]
    del graph.artifacts[str(removed.id)]
    with pytest.raises(RuntimeExecutionError, match="checkpoint .*lineage"):
        graph.validate()


@pytest.mark.parametrize("name", ["workspace.read", "workspace.list", "workspace_read"])
async def test_hydration_enforces_saved_mutable_observation_flags_without_reexecution(name: str) -> None:
    first = ToolCall(id="observe", name=name, arguments={"path": "first.txt"})
    case = await sparse_case(first)
    graph = await graph_case(case, first)
    # The untampered dense observation prefix is valid before removing its new receipt.
    await graph.runtime.restore_checkpoint(case.checkpoint)
    graph.validate()
    payload = case.checkpoint.to_payload()
    state = cast(dict[str, JsonValue], payload["state"])
    tools = cast(dict[str, dict[str, JsonValue]], state["tools"])
    key = next(key for key, row in tools.items() if row["round"] == 1 and row["tool_index"] == 0)
    removed = tools.pop(key)
    registry = cast(dict[str, str], state["artifact_registry"])
    del registry[cast(str, removed["artifact_id"])]
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    before = (len(case.gateway.requests), len(case.harness.calls), len(case.harness.waiting_calls))
    await graph.runtime.restore_checkpoint(checkpoint)
    context = case.context.model_copy(update={"checkpoint": checkpoint})
    stream = graph.runtime.run(context)
    assert isinstance(stream, CrewRunStream)
    stream._state.deadline = asyncio.get_running_loop().time() + 30
    try:
        with pytest.raises(RuntimeExecutionError, match="checkpoint capability artifact lineage"):
            await graph.runtime._hydrate_checkpoint(checkpoint, context, graph.runtime._plan, stream._state)
    finally:
        await stream.aclose()
    assert (len(case.gateway.requests), len(case.harness.calls), len(case.harness.waiting_calls)) == before


@pytest.mark.parametrize("name", ["workspace.read", "workspace.list", "workspace_read", "workspace.bundle"])
async def test_legacy_flags_preserve_prior_semantic_reuse_rules(name: str) -> None:
    first = ToolCall(id="legacy", name=name, arguments={"path": "first.txt"} if name != "workspace.bundle" else {"title": "Synthetic"})
    graph = await graph_case(await sparse_case(first), first)
    key = next(key for key, row in graph.tools.states.items() if row["round"] == 1 and row["tool_index"] == 0)
    removed = graph.tools.artifacts.pop(key)
    del graph.tools.states[key]
    del graph.artifacts[str(removed.id)]
    payload = graph.checkpoint.to_payload()
    state = cast(dict[str, JsonValue], payload["state"])
    for flag in ("workspace_delivery_continuation", "workspace_meaningful_progress", "workspace_context_refresh"):
        state.pop(flag, None)
    payload["state_sha256"] = ""
    graph.checkpoint = RuntimeCheckpoint.from_payload(payload)
    graph.validate()


@pytest.mark.parametrize("kind", ["duplicate_coordinate", "past_call_count"])
async def test_forged_slot_coordinates_are_rejected_without_external_calls(kind: str) -> None:
    case = await sparse_case()
    graph = await graph_case(case)
    payload = case.checkpoint.to_payload()
    state = cast(dict[str, JsonValue], payload["state"])
    tools = cast(dict[str, dict[str, JsonValue]], state["tools"])
    key = next(key for key, row in tools.items() if row["round"] == 1)
    row = tools[key]
    altered = {**row, "tool_index": 2} if kind == "past_call_count" else row.copy()
    alternate = graph.runtime._tool_call_key(
        case.context.run_id, cast(str, row["step_id"]), cast(int, row["attempt"]),
        cast(int, row["round"]), cast(int, altered["tool_index"]), cast(str, row["name"]),
        cast(str, row["arguments_sha256"]),
        trigger_model_artifact_id=cast(str, row["trigger_model_artifact_id"]),
    )
    assert alternate != key
    if kind == "past_call_count":
        del tools[key]
    tools[alternate] = altered
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    before = (len(case.gateway.requests), len(case.harness.calls))
    with pytest.raises(RuntimeExecutionError, match="checkpoint"):
        await graph.runtime.restore_checkpoint(checkpoint)
        _ = [event async for event in graph.runtime.run(case.context.model_copy(update={"checkpoint": checkpoint}))]
    assert (len(case.gateway.requests), len(case.harness.calls)) == before


async def test_project_generate_zip_without_current_generated_evidence_can_reuse_receipt() -> None:
    first = ToolCall(id="project-zip", name="project.generate_zip", arguments={"title": "Synthetic", "files": {"index.txt": "synthetic"}})
    case = await sparse_case(first)
    assert len(case.harness.calls) == 1
    assert next(row for row in cast(Mapping[str, Mapping[str, JsonValue]], case.checkpoint.state["tools"]).values()
                if row["round"] == 1)["tool_index"] == 1
    graph = await graph_case(case, first)
    assert reusable_generated_file_result(first.name, tuple(graph.artifacts.values()), arguments=first.arguments) is None
    graph.validate()
    await graph.runtime.restore_checkpoint(case.checkpoint)
    frozen = case.checkpoint.to_payload()
    context = case.context.model_copy(update={"checkpoint": case.checkpoint})
    stream = graph.runtime.run(context)
    assert isinstance(stream, CrewRunStream)
    stream._state.deadline = asyncio.get_running_loop().time() + 30
    try:
        hydrated = await graph.runtime._hydrate_checkpoint(case.checkpoint, context, graph.runtime._plan, stream._state)
        assert len(hydrated[2].states) == 2
        assert len(hydrated[2].artifacts) == 1
    finally:
        await stream.aclose()
    assert [call.tool_name for call in case.harness.calls] == ["project.generate_zip"]
    assert len(case.gateway.requests) == 2
    assert case.checkpoint.to_payload() == frozen


async def test_project_zip_current_generated_evidence_cannot_be_misrepresented_as_semantic_hole() -> None:
    first = ToolCall(id="project-zip", name="project.generate_zip", arguments={"title": "Synthetic", "files": {"index.txt": "synthetic"}})
    graph = await graph_case(await sparse_case(first), first)
    graph.validate()
    key = next(key for key, row in graph.tools.states.items() if row["round"] == 0)
    original = graph.tools.artifacts[key]
    content = cast(dict[str, JsonValue], _mutable_json(original.content))
    content["result"] = {
        "presentation": "final_attachment", "artifact_id": str(uuid4()),
        "file": {"filename": "synthetic.zip", "mime_type": "application/zip", "download_url": "/synthetic-only"},
    }
    changed = Artifact(id=original.id, type=original.type, producer=original.producer,
                       content=content, source_ids=original.source_ids, provenance=original.provenance)
    graph.tools.artifacts[key] = changed
    graph.artifacts[str(changed.id)] = changed
    graph.tools.states[key] = {**graph.tools.states[key], "sha256": changed.content_sha256}
    scoped_arguments = _scope_project_workspace_tool_call(graph.context, first).arguments
    assert reusable_generated_file_result(first.name, (changed,), arguments=scoped_arguments) is not None
    with pytest.raises(RuntimeExecutionError, match="checkpoint .*lineage"):
        graph.validate()
