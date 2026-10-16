"""Bounded lost-context refresh is not semantic progress or new budget."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

import pytest

from agent_hub.models.types import ModelMessage, ModelResponse, TokenUsage
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, CrewRunStream, RuntimeExecutionError
from agent_hub.runtime.progress import artifact_progress_key, workspace_progress_key
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_bundle_response,
    _workspace_delivery_partial_checkpoint,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
    _workspace_delivery_write_response,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import (  # noqa: F401
    TOOLS,
    Observations,
    observe,
    offline_only,
)

FINAL = ModelResponse(text="Synthetic final.", usage=TokenUsage(1, 1, 2))
A, B = "src/dependency-a.ts", "src/dependency-b.ts"


def write(content: str = "export const ready = 1;") -> ModelResponse:
    return _workspace_delivery_write_response("src/business.ts", content, "write-business")


def tail() -> tuple[ModelResponse, ...]:
    return (_workspace_delivery_write_response("tests/business.test.ts", "const testReady = true;", "write-test"),
            _workspace_delivery_bundle_response(), FINAL)


def prefix(*, seed: bool = False) -> tuple[ModelResponse, ...]:
    before = (_workspace_delivery_write_response("src/seed.ts", "const seed = true;", "seed"),) if seed else ()
    return (*before, observe("workspace.read", A), observe("workspace.read", B), write(), FINAL)


def context(*, pressure: bool = True) -> TaskContext:
    value = _workspace_delivery_sequence_context()
    if pressure:
        value = value.model_copy(update={"routing_decision": {
            **value.routing_decision, "context_window_tokens": 32768}})
    return value


def harness(*, pressure: bool = True) -> Observations:
    value = Observations()
    value.files[A], value.files[B] = ("a" * 60000, "b" * 60000) if pressure else ("a", "b")
    return value


async def collect(
    responses: tuple[ModelResponse, ...], *, pressure: bool = True, enabled: bool = True,
) -> tuple[list[RunEvent], str | None, RuntimeCheckpoint, WorkspaceDeliverySequenceGateway, Observations]:
    gateway, tools = WorkspaceDeliverySequenceGateway(responses), harness(pressure=pressure)
    runtime = _workspace_delivery_sequence_runtime(gateway, tools, tools=TOOLS)
    stream = cast(CrewRunStream, runtime.run(context(pressure=pressure)))
    if not enabled and hasattr(stream._state, "workspace_context_refresh"):
        stream._state.workspace_context_refresh = False
    events: list[RunEvent] = []
    reason: str | None = None
    try:
        async for event in stream:
            events.append(event)
    except RuntimeExecutionError as error:
        reason = str(error)
    return events, reason, await runtime.save_checkpoint(), gateway, tools


def trace_deadline(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    credits: list[float] = []
    original = adapter._tool_progress_step_deadline

    def trace(*args: object, **kwargs: object) -> float:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        credits.append(result)
        return result

    monkeypatch.setattr(adapter, "_tool_progress_step_deadline", trace)
    return credits


async def test_lost_dependency_after_novel_write_and_early_final_can_refresh_then_deliver() -> None:
    responses = (*prefix(seed=True), observe("workspace.read", A), *tail())
    events, reason, checkpoint, gateway, tools = await collect(responses)
    assert reason is None
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert checkpoint.state.get("workspace_context_refresh") is True
    assert len(gateway.requests) == len(responses)
    assert "tests/business.test.ts" in tools.files
    reads = [event.artifact for event in events if event.kind is EventKind.TOOL_COMPLETED
             and event.tool_name == "workspace.read" and event.artifact is not None]
    key = artifact_progress_key(reads[0])
    assert key is not None
    assert artifact_progress_key(reads[-1]) == key
    visible = CrewDispatchRuntime._visible_workspace_read_keys(gateway.requests[len(prefix(seed=True))].messages)
    assert visible is not None and key not in visible
    assert len({call.idempotency_key for call in tools.calls if call.tool_name == "workspace.read"}) == 3


async def test_visible_duplicate_is_not_a_context_refresh() -> None:
    events, reason, _checkpoint, gateway, tools = await collect(
        (*prefix(), observe("workspace.read", A), *tail()), pressure=False)
    assert reason == "workspace delivery stalled after unchanged observations"
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert len(gateway.requests) == len(prefix()) + 1
    assert "tests/business.test.ts" not in tools.files


@pytest.mark.parametrize("enabled", (False, True))
async def test_read_alias_refresh_uses_fresh_authorized_result_only_for_new_policy(enabled: bool) -> None:
    def alias(response: ModelResponse) -> ModelResponse:
        return replace(response, tool_calls=tuple(
            replace(call, name="workspace_read") if call.name == "workspace.read" else call
            for call in response.tool_calls
        ))

    responses = tuple(alias(response) for response in (
        *prefix(seed=True), observe("workspace.read", A), *tail(),
    ))
    gateway, tools = WorkspaceDeliverySequenceGateway(responses), harness()
    names = tuple("workspace_read" if name == "workspace.read" else name for name in TOOLS)
    subject = _workspace_delivery_sequence_runtime(gateway, tools, tools=names)
    stream = cast(CrewRunStream, subject.run(context()))
    stream._state.workspace_context_refresh = enabled
    events = []
    if enabled:
        events = [event async for event in stream]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len([call for call in tools.calls if call.tool_name == "workspace_read"]) == 3
    else:
        with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
            async for event in stream:
                events.append(event)
        assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
        assert len([call for call in tools.calls if call.tool_name == "workspace_read"]) == 2


async def test_alternating_hidden_reads_are_once_per_key_per_epoch_without_budget_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credits = trace_deadline(monkeypatch)
    responses = (*prefix(), *(observe("workspace.read", path) for path in (A, B, A, B)), *tail())
    events, reason, checkpoint, gateway, tools = await collect(responses)
    assert reason == "workspace delivery stalled after unchanged observations"
    assert len(gateway.requests) == len(prefix()) + 4
    assert len([call for call in tools.calls if call.tool_name == "workspace.read"]) == 6
    assert len(credits) == 3, "only the initial two observations and novel write earn deadline credit"
    assert checkpoint.state["timeout_progress_units"] == 3
    for request, path in zip(gateway.requests[4:8], (A, B, A, B), strict=True):
        key = workspace_progress_key("workspace.read", "", {"path": path,
            "text": tools.files[path], "truncated": False})
        assert key is not None
        visible = CrewDispatchRuntime._visible_workspace_read_keys(request.messages)
        assert visible is not None and key not in visible
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


async def test_no_novel_write_means_no_refresh_epoch() -> None:
    responses = (observe("workspace.read", A), observe("workspace.read", B), FINAL,
                 observe("workspace.read", A), *tail())
    _events, reason, _checkpoint, gateway, tools = await collect(responses)
    assert reason == "workspace delivery stalled after unchanged observations"
    assert len(gateway.requests) == 4
    assert not any(call.tool_name == "workspace.write_text" for call in tools.calls)


async def test_reexecuted_noop_write_does_not_reset_spent_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = adapter._succeeded_semantic_tool_result

    def without_write_reuse(
        ledger: adapter._ToolLedger, *, step_id: str, name: str, arguments_sha256: str,
    ) -> Artifact | None:
        return None if name == "workspace.write_text" else lookup(
            ledger, step_id=step_id, name=name, arguments_sha256=arguments_sha256)

    monkeypatch.setattr(adapter, "_succeeded_semantic_tool_result", without_write_reuse)
    duplicate_and_read = ModelResponse(text=None, tool_calls=(
        write().tool_calls[0], observe("workspace.read", A).tool_calls[0]), usage=TokenUsage(1, 1, 2))
    responses = (*prefix(), observe("workspace.read", A), observe("workspace.read", B),
                 duplicate_and_read, observe("workspace.read", B), *tail())
    _events, reason, checkpoint, gateway, tools = await collect(responses)
    assert reason == "workspace delivery stalled after unchanged observations"
    assert len(gateway.requests) == 8
    assert len([call for call in tools.calls if call.tool_name == "workspace.write_text"]) == 2
    assert checkpoint.state["timeout_progress_units"] == 3


async def test_new_global_write_key_opens_another_bounded_epoch() -> None:
    responses = (*prefix(), observe("workspace.read", A), observe("workspace.read", B),
                 write("export const ready = 2;"), FINAL, observe("workspace.read", A), *tail())
    events, reason, _checkpoint, gateway, _tools = await collect(responses)
    assert reason is None and events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == len(responses)


async def test_refresh_does_not_extend_active_round_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "_tool_round_budget", lambda *args: adapter._ToolRoundBudget(5, 1, 30))
    responses = (*prefix(), observe("workspace.read", A), observe("workspace.read", B), *tail())
    _events, reason, _checkpoint, gateway, tools = await collect(responses)
    assert reason == "step capability round limit exceeded"
    assert len(gateway.requests) == 6
    assert len([call for call in tools.calls if call.tool_name == "workspace.read"]) == 3


def test_read_visibility_uses_only_complete_user_result_frames_not_progress_or_system_text() -> None:
    result = {"path": A, "text": "dependency", "truncated": False}
    frame = "UNTRUSTED_CAPABILITY_RESULTS_JSON=" + json.dumps([
        {"name": "workspace_read", "result": result}])
    key = workspace_progress_key("workspace.read", "", result)
    assert key is not None
    assert CrewDispatchRuntime._visible_workspace_read_keys((ModelMessage(role="user", content=frame),)) == frozenset({key})
    assert CrewDispatchRuntime._visible_workspace_read_keys((
        ModelMessage(role="system", content=frame),
        ModelMessage(role="user", content="WORKSPACE_DELIVERY_PROGRESS_JSON=" + json.dumps(result)),
    )) == frozenset()


@pytest.mark.parametrize("payload", ("broken", "{}", '[{"name":"workspace.read","result":{}}]',
                                     '[{"name":"workspace.read","name":"ignored","result":{}}]'))
def test_unknown_or_duplicate_json_visibility_fails_closed(payload: str) -> None:
    assert CrewDispatchRuntime._visible_workspace_read_keys((ModelMessage(
        role="user", content="UNTRUSTED_CAPABILITY_RESULTS_JSON=" + payload),)) is None


@pytest.mark.parametrize("flag", ("true", 1, None))
async def test_invalid_new_flag_rejected_before_any_gateway_or_harness_call(flag: object) -> None:
    checkpoint, task, repository, tools, _first = await _workspace_delivery_partial_checkpoint()
    payload = checkpoint.to_payload()
    cast(dict[str, object], payload["state"])["workspace_context_refresh"] = flag
    payload["state_sha256"] = ""
    forged = RuntimeCheckpoint.from_payload(payload)
    gateway = WorkspaceDeliverySequenceGateway(())
    runtime = _workspace_delivery_sequence_runtime(gateway, tools, artifact_repository=repository)
    count = len(tools.calls)
    with pytest.raises(RuntimeExecutionError, match="checkpoint"):
        await runtime.restore_checkpoint(forged)
        async for _event in runtime.run(task.model_copy(update={"checkpoint": forged})):
            pass
    assert not gateway.requests and len(tools.calls) == count


async def test_old_missing_flag_keeps_hashes_tools_and_original_stall_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    original_run = CrewDispatchRuntime.run

    def legacy_run(runtime: CrewDispatchRuntime, task: TaskContext) -> CrewRunStream:
        stream = cast(CrewRunStream, original_run(runtime, task))
        if hasattr(stream._state, "workspace_context_refresh"):
            stream._state.workspace_context_refresh = False
        return stream

    responses = prefix()
    with monkeypatch.context() as generate:
        generate.setattr(CrewDispatchRuntime, "run", legacy_run)
        checkpoint, task, repository, tools, first = await _workspace_delivery_partial_checkpoint(
            responses=responses, context=context(), harness=harness(), tools=TOOLS, expected_tools=3)
    payload = checkpoint.to_payload()
    cast(dict[str, object], payload["state"]).pop("workspace_context_refresh", None)
    payload["state_sha256"] = ""
    legacy = RuntimeCheckpoint.from_payload(payload)
    before, count = legacy.to_payload(), len(tools.calls)
    gateway = WorkspaceDeliverySequenceGateway((observe("workspace.read", A), *tail()))
    runtime = _workspace_delivery_sequence_runtime(gateway, tools, artifact_repository=repository, tools=TOOLS)
    await runtime.restore_checkpoint(legacy)
    with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
        async for _event in runtime.run(task.model_copy(update={"checkpoint": legacy})):
            pass
    saved = await runtime.save_checkpoint()
    assert saved.state.get("workspace_context_refresh") is False
    assert len(first.requests) == len(responses) and len(gateway.requests) == 1
    assert len(tools.calls) == count + 1
    assert legacy.to_payload() == before
    for key, row in cast(Mapping[str, Mapping[str, JsonValue]], legacy.state["models"]).items():
        assert cast(Mapping[str, Mapping[str, JsonValue]], saved.state["models"])[key] == row


async def test_pressure_restore_rebuilds_epoch_and_spent_keys_without_reexecuting_or_rebilling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = (*prefix(), observe("workspace.read", A))
    checkpoint, task, repository, tools, first = await _workspace_delivery_partial_checkpoint(
        responses=responses, context=context(), harness=harness(), tools=TOOLS, expected_tools=4)
    assert checkpoint.state.get("workspace_context_refresh") is True
    assert any(str(message.content).startswith("EARLIER_INTERACTION_WINDOW_COMPRESSED:")
               for message in first.requests[-1].messages)
    before, calls, files = checkpoint.to_payload(), tuple(tools.calls), dict(tools.files)
    credits = trace_deadline(monkeypatch)
    gateway = WorkspaceDeliverySequenceGateway((observe("workspace.read", B), observe("workspace.read", A),
                                               observe("workspace.read", B), *tail()))
    runtime = _workspace_delivery_sequence_runtime(gateway, tools, artifact_repository=repository, tools=TOOLS)
    await runtime.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
        async for _event in runtime.run(task.model_copy(update={"checkpoint": checkpoint})):
            pass
    saved = await runtime.save_checkpoint()
    assert len(first.requests) == 5 and len(gateway.requests) == 3
    assert tuple(tools.calls[:len(calls)]) == calls and len(tools.calls) == len(calls) + 3
    assert tools.files == files and checkpoint.to_payload() == before
    assert credits == []
    assert saved.state["usage"] == {"tokens": 16, "cost_usd": "0"}
    assert saved.state["timeout_progress_units"] == 3
    for ledger in ("models", "tools"):
        for key, row in cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state[ledger]).items():
            assert cast(Mapping[str, Mapping[str, JsonValue]], saved.state[ledger])[key] == row


@pytest.mark.parametrize("enabled,prior_write", ((True, True), (False, True), (True, False)))
async def test_internal_retry_can_refresh_old_dependencies_only_with_prior_write(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, prior_write: bool,
) -> None:
    credits = trace_deadline(monkeypatch)
    before = (write(),) if prior_write else ()
    responses = (*before, observe("workspace.read", A), observe("workspace.read", B),
                 ModelResponse(text="", usage=TokenUsage(1, 1, 2)),
                 observe("workspace.read", A), observe("workspace.read", B),
                 _workspace_delivery_bundle_response(), FINAL)
    events, reason, checkpoint, gateway, tools = await collect(responses, enabled=enabled)
    retries = [event for event in events if event.kind is EventKind.STEP_RETRYING]
    assert len(retries) == 1 and retries[0].reason == "model response text is empty"
    succeeded = enabled and prior_write
    if succeeded:
        assert reason is None and events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(gateway.requests) == len(responses)
    else:
        assert reason == "workspace delivery stalled after unchanged observations"
        assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
        assert len(gateway.requests) == len(before) + 5
    assert [call.tool_name for call in tools.calls] == [
        *("workspace.write_text",) * len(before), *("workspace.read",) * 4,
        *("workspace.bundle",) * int(succeeded),
    ]
    assert len({call.idempotency_key for call in tools.calls}) == len(tools.calls)
    assert len(credits) == len(before) + 2 + int(succeeded)
    # Successful delivery also earns the existing completed-step and final-text units.
    assert checkpoint.state["timeout_progress_units"] == (6 if succeeded else len(before) + 2)
    refreshed = [event.checkpoint for event in events if event.checkpoint is not None
                 and len(cast(Mapping[str, JsonValue], event.checkpoint.state["tools"])) == len(before) + 4]
    assert refreshed and all(snapshot.state["timeout_progress_units"] == len(before) + 2
                             for snapshot in refreshed)
    rows = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["tools"])
    assert sorted(cast(int, row["attempt"]) for row in rows.values()
                  if row["name"] == "workspace.read") == [0, 0, 1, 1]


async def test_internal_retry_restore_does_not_reopen_spent_initial_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository

    responses = (write(), observe("workspace.read", A), observe("workspace.read", B),
                 ModelResponse(text="", usage=TokenUsage(1, 1, 2)),
                 observe("workspace.read", A), observe("workspace.read", B),
                 _workspace_delivery_bundle_response(), FINAL)
    repository, tools = InMemoryArtifactRepository(), harness()
    gateway = WorkspaceDeliverySequenceGateway(responses)
    subject = _workspace_delivery_sequence_runtime(gateway, tools, artifact_repository=repository, tools=TOOLS)
    task = context()
    events = [event async for event in subject.run(task)]
    snapshots = [event.checkpoint for event in events if event.checkpoint is not None
                 and len(cast(Mapping[str, JsonValue], event.checkpoint.state["tools"])) == 5
                 and len(cast(Mapping[str, JsonValue], event.checkpoint.state["models"])) == 6
                 and all(row["status"] == "succeeded" for row in
                         cast(Mapping[str, Mapping[str, JsonValue]], event.checkpoint.state["tools"]).values())
                 and all(row["status"] not in {"prepared", "running"} for row in
                         cast(Mapping[str, Mapping[str, JsonValue]], event.checkpoint.state["models"]).values())]
    assert snapshots
    checkpoint = snapshots[-1]
    payload, calls, files = checkpoint.to_payload(), tuple(tools.calls), dict(tools.files)
    credits = trace_deadline(monkeypatch)
    fresh = WorkspaceDeliverySequenceGateway((observe("workspace.read", A), observe("workspace.read", B),
                                              _workspace_delivery_bundle_response(), FINAL))
    restored = _workspace_delivery_sequence_runtime(fresh, tools, artifact_repository=repository, tools=TOOLS)
    await restored.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match="workspace delivery stalled"):
        async for _event in restored.run(task.model_copy(update={"checkpoint": checkpoint})):
            pass
    saved = await restored.save_checkpoint()
    assert len(fresh.requests) == 2
    assert tuple(tools.calls[:len(calls)]) == calls and len(tools.calls) == len(calls) + 2
    assert all(call.tool_name == "workspace.read" for call in tools.calls[len(calls):])
    assert tools.files == files and checkpoint.to_payload() == payload
    assert credits == [] and saved.state["timeout_progress_units"] == 3
    for ledger in ("models", "tools"):
        for key, row in cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state[ledger]).items():
            assert cast(Mapping[str, Mapping[str, JsonValue]], saved.state[ledger])[key] == row


@pytest.mark.parametrize("boundary", ("rounds", "retries"))
async def test_internal_retry_refresh_does_not_extend_configured_limits(
    monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    if boundary == "rounds":
        monkeypatch.setattr(adapter, "_tool_round_budget", lambda *args: adapter._ToolRoundBudget(2, 2, 20))
    credits = trace_deadline(monkeypatch)
    empty = ModelResponse(text="", usage=TokenUsage(1, 1, 2))
    responses = (write(), observe("workspace.read", A), observe("workspace.read", B), empty,
                 observe("workspace.read", A), observe("workspace.read", B),
                 _workspace_delivery_bundle_response() if boundary == "rounds" else empty,
                 _workspace_delivery_bundle_response(), FINAL)
    events, reason, checkpoint, gateway, tools = await collect(responses)
    assert reason == ("step capability round limit exceeded" if boundary == "rounds"
                      else "model response text is empty")
    assert len([event for event in events if event.kind is EventKind.STEP_RETRYING]) == 1
    assert len(gateway.requests) == 7
    assert [call.tool_name for call in tools.calls] == ["workspace.write_text", *("workspace.read",) * 4]
    assert len(credits) == checkpoint.state["timeout_progress_units"] == 3
