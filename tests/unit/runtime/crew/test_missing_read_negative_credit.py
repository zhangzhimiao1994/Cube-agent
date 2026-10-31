"""Negative reader feedback cannot buy legacy deadlines, rounds or checkpoint progress."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.types import ModelMessage, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.progress import workspace_progress_key
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_partial_checkpoint,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import (
    Observations,
    offline_only,  # noqa: F401
    write,
)

PATH = "src/missing.ts"
STRICT: dict[str, JsonValue] = {"path": PATH, "status": "not_found",
    "error_code": "workspace.file_not_found", "truncated": False}


@pytest.mark.parametrize("name", ("read_context", "workspace.read", "workspace_read"))
@pytest.mark.parametrize("payload", (
    {"status": "not_found", "matches": (), "truncated": False},
    {"error_code": "workspace.file_not_found", "matches": (), "truncated": False},
    {"status": "not_found", "error_code": "workspace.file_not_found", "truncated": False},
    {**STRICT, "text": ""},
))
def test_malformed_negative_markers_precede_reader_path_query_split(
    name: str, payload: dict[str, JsonValue],
) -> None:
    assert workspace_progress_key(name, "", payload) is None
    frame = ModelMessage("user", "UNTRUSTED_CAPABILITY_RESULTS_JSON=" + json.dumps([
        {"name": "read_context", "result": {"matches": (), "truncated": False}},
        {"name": name, "result": payload},
    ]))
    assert adapter.CrewDispatchRuntime._visible_workspace_read_keys((frame,)) is None


def test_ordinary_query_retains_progress_and_visibility_semantics() -> None:
    query = {"query": "context", "matches": (), "truncated": False}
    assert workspace_progress_key("read_context", "", query) is not None
    frame = ModelMessage("user", "UNTRUSTED_CAPABILITY_RESULTS_JSON=" + json.dumps([
        {"name": "read_context", "result": query}, {"name": "read_context", "result": STRICT},
    ]))
    assert adapter.CrewDispatchRuntime._visible_workspace_read_keys((frame,)) == frozenset()


class NegativeReader(Observations):
    def __init__(self, payload: Mapping[str, JsonValue]) -> None:
        super().__init__()
        self.payload = payload

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        if request.tool_name not in {"read_context", "workspace.read", "workspace_read"}:
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        assert user_id is not None and role is Role.OPERATOR
        self.calls.append(request)
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                     status="succeeded", payload=self.payload)


def read(name: str) -> ModelResponse:
    return ModelResponse(text=None, usage=TokenUsage(1, 1, 2), tool_calls=(
        ToolCall(id="missing", name=name, arguments={"path": PATH}),
    ))


def policy(stream: adapter.CrewRunStream, continuation: bool, meaningful: bool) -> None:
    stream._state.workspace_delivery_continuation = continuation
    stream._state.workspace_meaningful_progress = meaningful
    stream._state.workspace_context_refresh = False


CASES = (
    ("workspace.read", STRICT), ("workspace_read", STRICT), ("read_context", STRICT),
    ("read_context", {"status": "not_found", "matches": (), "truncated": False}),
    ("read_context", {"error_code": "workspace.file_not_found", "matches": (), "truncated": False}),
    ("workspace.read", {**STRICT, "text": ""}),
)


@pytest.mark.parametrize("continuation,meaningful", ((True, False), (False, True), (False, False)))
@pytest.mark.parametrize("name,payload", CASES)
async def test_legacy_negative_live_and_exact_replay_do_not_buy_soft_budget(
    monkeypatch: pytest.MonkeyPatch, continuation: bool, meaningful: bool,
    name: str, payload: Mapping[str, JsonValue],
) -> None:
    monkeypatch.setattr(adapter, "_tool_round_budget", lambda *_: adapter._ToolRoundBudget(2, 2, 4))
    credits: list[float] = []
    extend = adapter._tool_progress_step_deadline

    def record_extension(*args: object, **kwargs: object) -> float:
        deadline = extend(*args, **kwargs)  # type: ignore[arg-type]
        credits.append(deadline)
        return deadline

    monkeypatch.setattr(adapter, "_tool_progress_step_deadline", record_extension)
    tools = ("workspace.write_text", "workspace.list", "workspace.bundle", name)
    task = _workspace_delivery_sequence_context()
    task = task.model_copy(update={"timeout_seconds": 60, "routing_decision": {
        **task.routing_decision, "runtime_timeout_source": "project_scale_soft_budget",
        "runtime_timeout_soft_seconds": 10, "runtime_timeout_absolute_seconds": 180,
        "critical_path_complexity_units": 1,
    }})
    # One positive write keeps its legacy credit; two negative reads earn none.
    first = ModelResponse(text=None, usage=TokenUsage(1, 1, 2),
                          tool_calls=(*write().tool_calls, *read(name).tool_calls))
    responses = (first, *(read(name) for _ in range(4)))
    gateway, harness = WorkspaceDeliverySequenceGateway(responses), NegativeReader(payload)
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=tools)
    stream = runtime.run(task)
    assert isinstance(stream, adapter.CrewRunStream)
    policy(stream, continuation, meaningful)
    with pytest.raises(adapter.RuntimeExecutionError, match="step capability round limit exceeded"):
        async for _event in stream:
            pass
    saved = await runtime.save_checkpoint()
    assert len(credits) == 1, "only the successful write may renew the step deadline"
    assert len(gateway.requests) == 3 and len(harness.calls) == 3
    assert saved.state["timeout_progress_units"] == 4  # Three model artifacts and the write.
    assert saved.state["usage"] == {"tokens": 6, "cost_usd": "0"}
    remaining = saved.state["remaining_timeout_seconds"]
    absolute_remaining = saved.state["remaining_absolute_timeout_seconds"]
    assert isinstance(remaining, int | float) and not isinstance(remaining, bool)
    assert isinstance(absolute_remaining, int | float) and not isinstance(absolute_remaining, bool)
    assert 0 < remaining <= absolute_remaining <= 180

    # Save an actual successful tool boundary, then replay its exact calls.
    original_run = adapter.CrewDispatchRuntime.run

    def legacy_run(subject: adapter.CrewDispatchRuntime, context: TaskContext) -> adapter.CrewRunStream:
        result = cast(adapter.CrewRunStream, original_run(subject, context))
        policy(result, continuation, meaningful)
        return result

    credits.clear()
    replay_harness = NegativeReader(payload)
    with monkeypatch.context() as generate:
        generate.setattr(adapter.CrewDispatchRuntime, "run", legacy_run)
        checkpoint, task, repository, _, _ = await _workspace_delivery_partial_checkpoint(
            responses=(first, read(name)), expected_tools=3, context=task,
            harness=replay_harness, tools=tools,
        )
    assert len(credits) == 1 and checkpoint.state["timeout_progress_units"] == 3
    before, calls = checkpoint.to_payload(), tuple(replay_harness.calls)
    credits.clear()
    resumed_gateway = WorkspaceDeliverySequenceGateway((read(name),))
    restored = _workspace_delivery_sequence_runtime(resumed_gateway, replay_harness,
        artifact_repository=repository, tools=tools)
    await restored.restore_checkpoint(checkpoint)
    with pytest.raises(adapter.RuntimeExecutionError, match="step capability round limit exceeded"):
        async for _event in restored.run(task.model_copy(update={"checkpoint": checkpoint})):
            pass
    resumed = await restored.save_checkpoint()
    # Preserve the old policy's positive-write replay behavior, but add no negative credit.
    assert len(credits) == 1
    assert tuple(replay_harness.calls) == calls and len(resumed_gateway.requests) == 1
    assert resumed.state["timeout_progress_units"] == 4
    assert resumed.state["usage"] == {"tokens": 6, "cost_usd": "0"}
    assert checkpoint.to_payload() == before
    assert not resumed.state["terminal"] or resumed.state["phase"] != "completed"
    for ledger in ("models", "tools"):
        for key, row in cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state[ledger]).items():
            assert cast(Mapping[str, Mapping[str, JsonValue]], resumed.state[ledger])[key] == row


@pytest.mark.parametrize("continuation,meaningful", ((True, False), (False, True), (False, False)))
@pytest.mark.parametrize("name", ("workspace.read", "workspace_read", "read_context"))
async def test_legacy_successful_reads_keep_credit_but_cannot_cross_hard_round_limit(
    monkeypatch: pytest.MonkeyPatch, continuation: bool, meaningful: bool, name: str,
) -> None:
    monkeypatch.setattr(adapter, "_tool_round_budget", lambda *_: adapter._ToolRoundBudget(2, 2, 4))
    credits: list[float] = []
    extend = adapter._tool_progress_step_deadline

    def record_extension(*args: object, **kwargs: object) -> float:
        deadline = extend(*args, **kwargs)  # type: ignore[arg-type]
        credits.append(deadline)
        return deadline

    monkeypatch.setattr(adapter, "_tool_progress_step_deadline", record_extension)
    first = ModelResponse(text=None, usage=TokenUsage(1, 1, 2),
                          tool_calls=(*write().tool_calls, *read(name).tool_calls))
    gateway = WorkspaceDeliverySequenceGateway((first, *(read(name) for _ in range(4))))
    harness = NegativeReader({"path": PATH, "text": "actual file", "truncated": False})
    tools = ("workspace.write_text", "workspace.list", "workspace.bundle", name)
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=tools)
    stream = runtime.run(_workspace_delivery_sequence_context())
    assert isinstance(stream, adapter.CrewRunStream)
    policy(stream, continuation, meaningful)
    events = []
    with pytest.raises(adapter.RuntimeExecutionError, match="step capability round limit exceeded"):
        async for event in stream:
            events.append(event)
    checkpoint = await runtime.save_checkpoint()
    assert len(credits) == 4
    assert len(gateway.requests) == len(harness.calls) == 5
    assert checkpoint.state["timeout_progress_units"] == 10
    assert checkpoint.state["usage"] == {"tokens": 10, "cost_usd": "0"}
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
