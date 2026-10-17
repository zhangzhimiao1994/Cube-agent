"""Fresh maintenance feedback policy, not evidence about the actual failed run.

The candidate-only routing key below reserves a separate opt-in; it is not an
existing product interface or permission. Missing/zero must retain legacy replay.
All observations and tools are synthetic RAM data through the real adapter.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.types import ModelResponse
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import RuntimeExecutionError
from agent_hub.runtime.progress import workspace_progress_key
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_runtime,
    _workspace_delivery_write_response,
)
from tests.unit.runtime.crew.test_maintenance_delivery_execution import (
    FINAL,
    TASK,
    context,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import (  # noqa: F401
    TOOLS,
    Observations,
    observe,
    offline_only,
)

A, B = "src/dependency-a.ts", "src/dependency-b.ts"
VERSION_KEY = "workspace_maintenance_feedback_version"
PREFIX = (observe("workspace.read", A), observe("workspace.read", B), FINAL)
TAIL = (
    _workspace_delivery_write_response(A, "export const repaired = true;", "minimal-repair"),
    _workspace_delivery_bundle_response(), FINAL,
)


class ExistingWorkspace(Observations):
    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None,
                     role: Role | None = None) -> HarnessToolCallResult:
        result = await super().invoke(tenant_id, request, user_id=user_id, role=role)
        if request.tool_name == "workspace.bundle":
            result = replace(result, payload={**result.payload, "workspace_files": tuple(
                {"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                 "size_bytes": len(text.encode())} for path, text in sorted(self.files.items()))})
        return result


@dataclass
class Observation:
    events: list[RunEvent]
    reason: str | None
    checkpoint: RuntimeCheckpoint
    gateway: WorkspaceDeliverySequenceGateway
    tools: ExistingWorkspace
    deadline_credits: list[float]
    runtime: adapter.CrewDispatchRuntime
    task: TaskContext
    repository: InMemoryArtifactRepository


def make_subject(
    responses: tuple[ModelResponse, ...], *, version: JsonValue = None, pressure: bool = True,
    native: bool = True, task: TaskContext | None = None,
    tools: ExistingWorkspace | None = None, repository: InMemoryArtifactRepository | None = None,
) -> tuple[adapter.CrewDispatchRuntime, TaskContext, WorkspaceDeliverySequenceGateway, ExistingWorkspace]:
    gateway, tools = WorkspaceDeliverySequenceGateway(responses), tools or ExistingWorkspace()
    if not tools.files:
        tools.files = {A: "a" * (60_000 if pressure else 1), B: "b" * (60_000 if pressure else 1),
                       **{f"docs/retained-{i}.md": "Retained baseline." for i in range(28)}}
    task = task or context()
    route: dict[str, JsonValue] = {**task.routing_decision, "context_window_tokens": 32768}
    route.pop(VERSION_KEY, None)
    if version is not None:
        route[VERSION_KEY] = version
    task = task.model_copy(update={"routing_decision": route})
    runtime = _workspace_delivery_sequence_runtime(
        gateway, tools, tools=TOOLS, artifact_repository=repository)
    runtime._plan = runtime._plan.model_copy(update={
        "agents": (runtime._plan.agents[0].model_copy(update={
            "goal": "Repair only existing production files, preserve protected files, then bundle.",
        }),),
        "steps": (runtime._plan.steps[0].model_copy(update={
            "task": TASK if native else "Summarize existing source observations.",
            "workspace_maintenance_contract_version": 1 if native else 0,
        }),),
    })
    return runtime, task, gateway, tools


async def exercise(monkeypatch: pytest.MonkeyPatch, responses: tuple[ModelResponse, ...], *,
                   version: int | None, pressure: bool = True, native: bool = True) -> Observation:
    repository = InMemoryArtifactRepository()
    runtime, task, gateway, tools = make_subject(
        responses, version=version, pressure=pressure, native=native, repository=repository)
    credits: list[float] = []
    original = adapter._tool_progress_step_deadline

    def credit(ctx: TaskContext, *, step_deadline: float, run_deadline: float) -> float:
        result = original(ctx, step_deadline=step_deadline, run_deadline=run_deadline)
        credits.append(result)
        return result

    events, reason = [], None
    with monkeypatch.context() as scoped:
        scoped.setattr(adapter, "_tool_progress_step_deadline", credit)
        try:
            async for event in runtime.run(task):
                events.append(event)
        except RuntimeExecutionError as error:
            reason = str(error)
    return Observation(events, reason, await runtime.save_checkpoint(), gateway, tools, credits,
                       runtime, task, repository)


def prove_lost_prewrite_context(result: Observation) -> None:
    assert result.checkpoint.state["workspace_context_refresh"] is True
    assert len(result.gateway.requests) >= 4
    key = workspace_progress_key("workspace.read", "", {"path": A, "text": "a" * 60_000, "truncated": False})
    assert key is not None
    visible = adapter.CrewDispatchRuntime._visible_workspace_read_keys(result.gateway.requests[3].messages)
    assert visible is not None and key not in visible
    assert any(str(message.content).startswith("EARLIER_INTERACTION_WINDOW_COMPRESSED:")
               for message in result.gateway.requests[3].messages)
    assert [call.tool_name for call in result.tools.calls[:3]] == ["workspace.read"] * 3
    assert len({call.idempotency_key for call in result.tools.calls[:3]}) == 3


@pytest.mark.parametrize("version", (None, 0))
async def test_missing_or_zero_keeps_legacy_prewrite_refresh_closed(
    monkeypatch: pytest.MonkeyPatch, version: int | None,
) -> None:
    result = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL), version=version)
    prove_lost_prewrite_context(result)
    assert result.reason == "workspace delivery stalled after unchanged observations"
    assert len(result.gateway.requests) == 4 and len(result.tools.calls) == 3
    assert len(result.tools.files) == 30 and len(result.deadline_credits) == 2
    assert result.checkpoint.state["timeout_progress_units"] == 2


async def test_candidate_lost_prewrite_observation_can_refresh_then_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = (*PREFIX, observe("workspace.read", A), *TAIL)
    result = await exercise(monkeypatch, responses, version=1)
    prove_lost_prewrite_context(result)
    # Current RED must reach this assertion, not fail because the fixture lacked
    # real compaction, authorization, a native maintenance step or read execution.
    assert result.reason is None, "lost prewrite content was treated as an unchanged-observation stall"
    assert type(result.checkpoint.state[VERSION_KEY]) is int and result.checkpoint.state[VERSION_KEY] == 1
    assert result.events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(result.gateway.requests) == len(responses)
    assert [call.tool_name for call in result.tools.calls] == ["workspace.read"] * 3 + [
        "workspace.write_text", "workspace.bundle"]
    assert len(result.tools.files) == 30
    assert len(result.deadline_credits) == 4, "refresh itself cannot earn a deadline extension"


async def test_candidate_visible_duplicate_still_stops_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL), version=1, pressure=False)
    key = workspace_progress_key("workspace.read", "", {"path": A, "text": "a", "truncated": False})
    visible = adapter.CrewDispatchRuntime._visible_workspace_read_keys(result.gateway.requests[3].messages)
    assert visible is not None and key in visible
    assert result.reason == "workspace delivery stalled after unchanged observations"
    assert len(result.gateway.requests) == 4 and len(result.deadline_credits) == 2
    assert all(call.tool_name == "workspace.read" for call in result.tools.calls)


async def test_candidate_hidden_reread_cannot_become_unbounded_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = (*PREFIX, *(observe("workspace.read", A) for _ in range(12)), *TAIL)
    result = await exercise(monkeypatch, responses, version=1)
    prove_lost_prewrite_context(result)
    assert result.reason == "workspace delivery stalled after unchanged observations"
    assert len(result.gateway.requests) <= 6, "one initial refresh, then correction and bounded stop"
    assert len(result.deadline_credits) == 2 and result.checkpoint.state["timeout_progress_units"] == 2
    assert all(call.tool_name == "workspace.read" for call in result.tools.calls)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in result.events)


@pytest.mark.parametrize("version", (True, False, "1", -1, 2, 1.0))
async def test_invalid_feedback_version_rejected_before_io(version: JsonValue) -> None:
    runtime, task, gateway, tools = make_subject(TAIL, version=version)
    with pytest.raises(RuntimeExecutionError, match="version"):
        async for _ in runtime.run(task):
            pass
    assert not gateway.requests and not tools.calls


@pytest.mark.parametrize("version", (True, "1", -1, 2, 1.0))
async def test_invalid_persisted_feedback_version_rejected_before_io(
    monkeypatch: pytest.MonkeyPatch, version: JsonValue,
) -> None:
    original = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL), version=0)
    payload = original.checkpoint.to_payload()
    cast(dict[str, JsonValue], payload["state"])[VERSION_KEY] = version
    payload["state_sha256"] = ""
    invalid = RuntimeCheckpoint.from_payload(payload)
    before, calls = invalid.to_payload(), tuple(original.tools.calls)
    runtime, task, gateway, tools = make_subject(
        (), version=1, task=original.task, tools=original.tools, repository=original.repository)
    with pytest.raises(RuntimeExecutionError, match="checkpoint|version"):
        await runtime.restore_checkpoint(invalid)
        async for _ in runtime.run(task.model_copy(update={"checkpoint": invalid})):
            pass
    assert invalid.to_payload() == before and tuple(tools.calls) == calls and not gateway.requests


def rows(checkpoint: RuntimeCheckpoint, key: str) -> Mapping[str, Mapping[str, JsonValue]]:
    return cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state[key])


def partial(result: Observation, *, models: int, tools: int) -> RuntimeCheckpoint:
    snapshots = [event.checkpoint for event in result.events if event.checkpoint is not None
                 and len(rows(event.checkpoint, "models")) == models
                 and len(rows(event.checkpoint, "tools")) == tools
                 and all(row["status"] == "succeeded" for key in ("models", "tools")
                         for row in rows(event.checkpoint, key).values())]
    assert snapshots, "a real causal paid/model and tool prefix must be checkpointed"
    return snapshots[-1]


async def test_missing_and_zero_keep_request_and_plan_fingerprints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL), version=None)
    checkpoint = partial(original, models=3, tools=2)
    assert VERSION_KEY not in checkpoint.state
    before, calls = checkpoint.to_payload(), tuple(original.tools.calls)
    for version in (None, 0):
        tools = ExistingWorkspace()
        tools.files = dict(original.tools.files)
        runtime, task, gateway, tools = make_subject(
            (observe("workspace.read", A),), version=version, task=original.task,
            tools=tools, repository=original.repository)
        assert runtime._plan.digest == original.runtime._plan.digest
        await runtime.restore_checkpoint(checkpoint)
        with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
            async for _ in runtime.run(task.model_copy(update={"checkpoint": checkpoint})):
                pass
        saved = await runtime.save_checkpoint()
        assert len(gateway.requests) == 1 and len(tools.calls) == 1
        assert VERSION_KEY not in saved.state, "legacy checkpoint serialization must not acquire a new flag"
        for key in ("models", "tools"):
            for ledger_key, row in rows(checkpoint, key).items():
                assert rows(saved, key)[ledger_key] == row, "legacy request/receipt hashes must not change"
        assert checkpoint.to_payload() == before
    assert tuple(original.tools.calls) == calls


async def test_old_checkpoint_cannot_upgrade_from_new_context_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL), version=None)
    checkpoint = partial(original, models=3, tools=2)
    assert VERSION_KEY not in checkpoint.state or checkpoint.state[VERSION_KEY] == 0
    runtime, task, gateway, tools = make_subject(
        (observe("workspace.read", A),), version=1, task=original.task,
        tools=original.tools, repository=original.repository)
    before, calls = checkpoint.to_payload(), tuple(tools.calls)
    await runtime.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
        async for _ in runtime.run(task.model_copy(update={"checkpoint": checkpoint})):
            pass
    saved = await runtime.save_checkpoint()
    assert saved.state.get(VERSION_KEY, 0) == 0 and len(gateway.requests) == 1
    assert tuple(tools.calls[:len(calls)]) == calls and len(tools.calls) == len(calls) + 1
    assert checkpoint.to_payload() == before


async def test_initial_epoch_is_per_key_once_and_never_deadline_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await exercise(monkeypatch, (
        *PREFIX, *(observe("workspace.read", path) for path in (A, B, A, B)), *TAIL,
    ), version=1)
    prove_lost_prewrite_context(result)
    assert result.reason == "workspace delivery stalled after unchanged observations"
    assert len(result.gateway.requests) == 7
    assert len(result.tools.calls) == 6 and all(call.tool_name == "workspace.read" for call in result.tools.calls)
    assert len(result.deadline_credits) == result.checkpoint.state["timeout_progress_units"] == 2
    assert len({call.idempotency_key for call in result.tools.calls}) == 6


@pytest.mark.parametrize("route_version", (None, 0, 1))
async def test_spent_initial_epoch_restore_preserves_hashes_and_does_not_replay(
    monkeypatch: pytest.MonkeyPatch, route_version: int | None,
) -> None:
    original = await exercise(monkeypatch, (
        *PREFIX, *(observe("workspace.read", path) for path in (A, B, A, B)), *TAIL,
    ), version=1)
    checkpoint = partial(original, models=5, tools=4)
    before, files = checkpoint.to_payload(), dict(original.tools.files)
    consumed_keys = {call.idempotency_key for call in original.tools.calls[:4]}
    resumed_tools = ExistingWorkspace()
    resumed_tools.files = dict(files)
    runtime, task, gateway, tools = make_subject(
        (observe("workspace.read", A), observe("workspace.read", B)), version=route_version, task=original.task,
        tools=resumed_tools, repository=original.repository)
    credits: list[float] = []
    original_credit = adapter._tool_progress_step_deadline

    def credit(ctx: TaskContext, *, step_deadline: float, run_deadline: float) -> float:
        result = original_credit(ctx, step_deadline=step_deadline, run_deadline=run_deadline)
        credits.append(result)
        return result

    monkeypatch.setattr(adapter, "_tool_progress_step_deadline", credit)
    await runtime.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
        async for _ in runtime.run(task.model_copy(update={"checkpoint": checkpoint})):
            pass
    saved = await runtime.save_checkpoint()
    assert saved.state[VERSION_KEY] == checkpoint.state[VERSION_KEY] == 1
    assert len(gateway.requests) == 2 and not credits
    assert len(tools.calls) == 2 and all(call.tool_name == "workspace.read" for call in tools.calls)
    assert consumed_keys.isdisjoint(call.idempotency_key for call in tools.calls)
    assert tools.files == files and checkpoint.to_payload() == before
    assert saved.state["timeout_progress_units"] == checkpoint.state["timeout_progress_units"] == 2
    assert cast(Mapping[str, JsonValue], saved.state["usage"])["tokens"] == 14
    for key in ("models", "tools"):
        for ledger_key, row in rows(checkpoint, key).items():
            assert rows(saved, key)[ledger_key] == row


async def test_completed_restore_has_zero_extra_calls_and_preserves_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL), version=1)
    assert result.reason is None
    checkpoint = result.checkpoint
    before, calls, files = checkpoint.to_payload(), tuple(result.tools.calls), dict(result.tools.files)
    runtime, task, gateway, tools = make_subject(
        (), version=1, task=result.task, tools=result.tools, repository=result.repository)
    await runtime.restore_checkpoint(checkpoint)
    events = [event async for event in runtime.run(task.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not gateway.requests and tuple(tools.calls) == calls and tools.files == files
    saved = await runtime.save_checkpoint()
    for key in ("models", "tools", "usage", "timeout_progress_units"):
        assert saved.state[key] == checkpoint.state[key]
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("version", (0, 1))
async def test_non_native_step_is_not_upgraded_by_route_flag(
    monkeypatch: pytest.MonkeyPatch, version: int,
) -> None:
    result = await exercise(monkeypatch, PREFIX, version=version, native=False)
    assert result.reason is None and result.events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(result.gateway.requests) == 3 and len(result.tools.calls) == 2
    assert all(call.tool_name == "workspace.read" for call in result.tools.calls)
    assert not any("WORKSPACE_MAINTENANCE_DELIVERY_CONTINUATION:" in str(message.content)
                   for request in result.gateway.requests for message in request.messages)
