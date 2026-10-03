import asyncio
import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelResponse, TokenUsage
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.contracts import EventKind, RunEvent, TaskContext
from agent_hub.runtime.direct import DirectRunStream, DirectRuntime, RuntimeExecutionError
from agent_hub.runtime.model_scope import validate_model_scope_artifact
from tests.unit.capabilities.test_scoped_read import project_path, write_file
from tests.unit.models.test_failure_receipt import OutcomesTransport, make_gateway
from tests.unit.models.test_gateway import CapacityStub, lease
from tests.unit.runtime.test_direct_prompt import RecordingCapabilityGateway
from tests.unit.runtime.test_instruction_context import load
from tests.unit.test_real_user_four_scale_acceptance_script import load_script


@pytest.mark.parametrize("outcome", ["empty", "provider408"])
@pytest.mark.parametrize("batch", [False, True])
async def test_direct_failure_publishes_actual_complete_scope_not_completion(
    outcome: str, batch: bool,
) -> None:
    failure: ModelResponse | Exception = (
        ModelResponse(text="", usage=TokenUsage(7, 0, 7)) if outcome == "empty"
        else ModelTransportError("private body", status_code=408)
    )
    first = ModelResponse(text=json.dumps({"workspace_batch": {
        "files": {"src/main.ts": "export {};\n"}, "complete": False,
        "continuation": "remaining files",
    }}), usage=TokenUsage(10, 20, 30))
    outcomes = [first, failure] if batch else [failure]
    transport = OutcomesTransport(outcomes)
    capacity = CapacityStub([lease("primary") for _ in outcomes])
    runtime = DirectRuntime(
        make_gateway(transport, capacity), logical_model="primary", available_model_attempts=1,
        capability_gateway=RecordingCapabilityGateway() if batch else None,
    )
    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DIRECT, token_budget=50_000,
        request="Build a project with source and tests." if batch else "Answer briefly.",
        routing_decision={"project_scale": "small", "project_delivery": "workspace",
                          "artifact_strategy": "workspace_bundle"} if batch else {},
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(context):
            events.append(event)
    assert [event.kind for event in events] == [
        EventKind.MODEL_STARTED, EventKind.ARTIFACT_CREATED, "model.failure_receipt",
    ]
    artifact = events[1].artifact
    assert artifact is not None and artifact.type == "model_attempt"
    content = validate_model_scope_artifact(artifact, str(context.run_id))
    assert content["call_count"] == (2 if batch else 1)
    calls = cast(list[dict[str, Any]], content["calls"])
    assert calls[-1]["receipt"]["attempts"][0]["outcome"] == (
        "empty_response" if outcome == "empty" else "transport_error"
    )
    assert events[2].payload["artifact_id"] == str(artifact.id)
    assert [event.sequence for event in events] == [1, 2, 3]
    assert runtime._last_checkpoint is None
    public = [_public_event_payload(event.to_payload()) for event in events]
    assert "private body" not in json.dumps(public)
    assert "key-for-" not in json.dumps(public)
    assert len(capacity.releases) == (2 if batch else 1)


async def test_direct_recovery_keeps_failed_call_scope_and_resets_next_run() -> None:
    transport = OutcomesTransport([
        ModelResponse(text=""), ModelResponse(text="recovered", usage=TokenUsage(2, 1, 3)),
        ModelResponse(text="next", usage=TokenUsage(2, 1, 3)),
    ])
    capacity = CapacityStub([lease("primary") for _ in range(3)])
    runtime = DirectRuntime(make_gateway(transport, capacity), logical_model="primary")
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DIRECT,
                          request="Answer briefly.")
    first = [event async for event in runtime.run(context)]
    assert [event.kind for event in first] == [
        EventKind.MODEL_STARTED, EventKind.ARTIFACT_CREATED, "model.failure_receipt",
        EventKind.ARTIFACT_CREATED, EventKind.CHECKPOINT_SAVED, EventKind.RUNTIME_COMPLETED,
    ]
    assert [event.sequence for event in first] == list(range(1, 7))
    assert first[1].artifact is not None
    content = validate_model_scope_artifact(first[1].artifact, str(context.run_id))
    assert content["call_count"] == 2
    assert first[3].artifact is not None
    assert first[-1].payload["artifact_id"] == str(first[3].artifact.id)
    second_context = context.model_copy(update={"run_id": uuid4()})
    second = [event async for event in runtime.run(second_context)]
    assert [event.kind for event in second] == [
        EventKind.MODEL_STARTED, EventKind.ARTIFACT_CREATED,
        EventKind.CHECKPOINT_SAVED, EventKind.RUNTIME_COMPLETED,
    ]


async def test_cancelled_scope_does_not_publish_failure_or_completion() -> None:
    class Transport:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def complete(self, *args: Any) -> ModelResponse:
            self.started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    transport = Transport()
    runtime = DirectRuntime(make_gateway(transport, CapacityStub([lease("primary")])),
                            logical_model="primary")
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DIRECT,
                          request="Answer briefly.")
    stream = cast(DirectRunStream, runtime.run(context))
    events: list[RunEvent] = []

    async def consume() -> None:
        async for event in stream:
            events.append(event)

    pending = asyncio.create_task(consume())
    await transport.started.wait()
    await stream.aclose()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert [event.kind for event in events] == [EventKind.MODEL_STARTED]
    assert runtime._last_checkpoint is None


async def test_cancellation_between_scope_artifact_and_receipt_has_no_completion() -> None:
    runtime = DirectRuntime(make_gateway(OutcomesTransport([ModelResponse(text="")]),
                                        CapacityStub([lease("primary")])),
                            logical_model="primary", available_model_attempts=1)
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DIRECT,
                          request="Answer briefly.")
    stream = cast(DirectRunStream, runtime.run(context))
    first = await anext(stream)
    second = await anext(stream)
    assert first.kind is EventKind.MODEL_STARTED and second.kind is EventKind.ARTIFACT_CREATED
    await stream.aclose()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert runtime._last_checkpoint is None and runtime._active_token is None


async def test_real_gateway_failure_to_public_scope_collector_keeps_all_runs() -> None:
    module = load_script()
    outcomes: list[ModelResponse | Exception] = [ModelResponse(text="original", usage=TokenUsage(2, 1, 3)),
                ModelResponse(text="", usage=TokenUsage(2, 0, 2)),
                ModelResponse(text="recovered", usage=TokenUsage(2, 1, 3))]
    runtime = DirectRuntime(
        make_gateway(OutcomesTransport(outcomes), CapacityStub([lease("primary") for _ in outcomes])),
        logical_model="primary", available_model_attempts=1,
    )
    run_ids: list[str] = []
    by_run: dict[str, dict[str, Any]] = {}
    tenant_id = uuid4()
    for index in range(3):
        context = TaskContext(run_id=uuid4(), tenant_id=tenant_id, mode=TaskMode.DIRECT,
                              request="Answer briefly.")
        run_id = str(context.run_id)
        run_ids.append(run_id)
        events = []
        try:
            async for event in runtime.run(context):
                events.append(_public_event_payload(event.to_payload()))
        except RuntimeExecutionError:
            assert index == 1
        by_run[run_id] = {"status": "failed" if index == 1 else "completed", "events": events}

    class Client:
        def request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            assert method == "GET" and all(value is None for value in kwargs.values())
            run_id = path.split("/")[4]
            if path.endswith("/events"):
                return {"items": by_run[run_id]["events"]}
            return {"id": run_id, "status": by_run[run_id]["status"]}

    evidence = module._collect_model_scope_evidence(
        module.RealUserAcceptanceClient(Client()), logical_model="primary",
        submitted_run_ids=run_ids[:2], accepted_repair_run_ids=[], result_run_id=run_ids[-1],
    )
    assert evidence["ok"] is True, evidence["errors"]
    assert [run["run_id"] for run in evidence["runs"]] == run_ids
    failed_events = evidence["runs"][1]["model_events"]
    assert module._has_failed_attempt_scope(failed_events)
    assert not module._has_direct_model_completion(failed_events)


async def test_unknown_failure_before_success_cannot_hide_in_final_completion() -> None:
    runtime = DirectRuntime(make_gateway(OutcomesTransport([
        ModelTransportError("private"), ModelResponse(text="recovered", usage=TokenUsage(2, 1, 3)),
    ]), CapacityStub([lease("primary"), lease("primary")])), logical_model="primary")
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DIRECT,
                          request="Answer briefly.")
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert any(event.kind == "model.scope_incomplete" for event in events)
    module = load_script()

    class Client:
        def request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            if path.endswith("/events"):
                return {"items": [_public_event_payload(event.to_payload()) for event in events]}
            return {"id": str(context.run_id), "status": "completed"}

    evidence = module._collect_model_scope_evidence(
        module.RealUserAcceptanceClient(Client()), logical_model="primary",
        submitted_run_ids=[str(context.run_id)], accepted_repair_run_ids=[],
        result_run_id=str(context.run_id),
    )
    assert not evidence["ok"]


async def test_foreign_first_workspace_batch_cannot_hide_behind_selected_final_model() -> None:
    def response(path: str, complete: bool) -> ModelResponse:
        return ModelResponse(text=json.dumps({"workspace_batch": {
            "files": {path: "export {};\n"}, "complete": complete,
            "continuation": "" if complete else "remaining files",
        }}), usage=TokenUsage(2, 1, 3))

    transport = OutcomesTransport([ModelTransportError("private", status_code=408),
                                   response("src/first.ts", False), response("src/second.ts", True)])
    runtime = DirectRuntime(make_gateway(transport, CapacityStub([
        lease("primary"), lease("foreign"), lease("primary"),
    ]), models=("primary", "foreign")), logical_model="primary",
        capability_gateway=RecordingCapabilityGateway())
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DIRECT,
                          request="Build a project with source and tests.", token_budget=50_000,
                          routing_decision={"project_scale": "small", "project_delivery": "workspace",
                                            "artifact_strategy": "workspace_bundle"})
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].payload["attempted_logical_models"] == ("primary",)
    scope = next(event.artifact for event in events
                 if event.artifact is not None and event.artifact.type == "model_attempt")
    assert scope is not None and scope.content["call_count"] == 2
    with pytest.raises(ValueError, match="invalid or incomplete"):
        validate_model_scope_artifact(scope, str(context.run_id))


@pytest.mark.parametrize("injection", [False, True])
async def test_scope_checkpoint_restore_preserves_sequence_without_new_calls(
    injection: bool, tmp_path: Path,
) -> None:
    transport = OutcomesTransport([ModelResponse(text=""),
                                   ModelResponse(text="recovered", usage=TokenUsage(2, 1, 3))])
    gateway = make_gateway(transport, CapacityStub([lease("primary"), lease("primary")]))
    runtime = DirectRuntime(gateway, logical_model="primary")
    write_file(tmp_path, f"{project_path()}/AGENTS.md", "Follow this project guidance.")
    instructions = await load(tmp_path) if injection else None
    context = TaskContext(run_id=instructions.run_id if instructions else uuid4(),
                          tenant_id=instructions.tenant_id if instructions else uuid4(),
                          mode=TaskMode.DIRECT, request="Answer briefly.",
                          instruction_context=instructions)
    events = [event async for event in runtime.run(context)]
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None)
    restored = DirectRuntime(gateway, logical_model="primary")
    await restored.restore_checkpoint(checkpoint)
    replay = [event async for event in restored.run(context.model_copy(update={"checkpoint": checkpoint}))]
    assert len(replay) == 1 and replay[0].kind is EventKind.RUNTIME_COMPLETED
    assert replay[0].sequence == events[-1].sequence
