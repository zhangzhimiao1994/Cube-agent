import asyncio
import json
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityBackendError, CapacityQueueFull
from agent_hub.models.gateway import GatewayCompletion, ModelGatewayError
from agent_hub.models.types import ModelResponse
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.contracts import RunEvent, TaskContext
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from agent_hub.runtime.model_scope import ModelScopeTracker
from tests.unit.models.test_failure_receipt import OutcomesTransport, make_gateway
from tests.unit.models.test_gateway import CapacityStub, TransportStub, lease, request
from tests.unit.runtime.test_model_scope import receive
from tests.unit.test_real_user_four_scale_acceptance_script import load_script


def payload(tracker: ModelScopeTracker) -> dict[str, Any]:
    value = getattr(tracker, "diagnostic_payload", None)
    assert isinstance(value, dict), "bounded scope diagnostic payload is missing"
    return value


async def test_direct_public_scope_locates_fifth_call_without_provider_data() -> None:
    capacity = CapacityStub([lease("primary")])
    capacity.record_error = CapacityBackendError("private key https://private.invalid")
    runtime = DirectRuntime(make_gateway(OutcomesTransport([ModelResponse(text="")]), capacity),
                            logical_model="primary", available_model_attempts=1)
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DIRECT,
                          request="private prompt")
    stream = runtime.run(context)
    events = [await anext(stream)]
    tracker = runtime._model_scope_tracker
    assert tracker is not None
    for _ in range(4):
        receive(tracker)
    with pytest.raises(RuntimeExecutionError):
        async for event in stream:
            events.append(event)
    public = [_public_event_payload(event.to_payload()) for event in events]
    scope = next(event for event in public if event["kind"] == "model.scope_incomplete")
    assert scope["payload"] == {
        "actor": "main_agent", "logical_model": "primary", "call_count": 5,
        "first_incomplete_call": 5, "first_incomplete_phase": "recorder",
        "first_incomplete_reason": "outcome_recording_failed", "recorded_call_count": 4,
        "transport_entered_count": 1, "failure_attempt_count": 1,
    }
    assert all(event["kind"] != "model.failure_receipt" for event in public)
    assert all(event.artifact is None for event in events)
    encoded = json.dumps(scope)
    for private in ("private prompt", "private key", "https://private.invalid", "key-for-", "Traceback"):
        assert private not in encoded
    module = load_script()
    assert not module._has_failed_attempt_scope(public)


@pytest.mark.parametrize("message", ["408", "model request deadline exhausted", "cleanup failed"])
def test_unknown_adapter_text_never_drives_classification(message: str) -> None:
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    receive(tracker)
    tracker.failed(tracker.begin(), RuntimeError(message))
    receive(tracker)
    result = payload(tracker)
    assert result["first_incomplete_call"] == 2
    assert result["first_incomplete_phase"] == "unknown_adapter"
    assert result["first_incomplete_reason"] == "unknown_failure"
    assert "transport_entered_count" not in result and "failure_attempt_count" not in result
    assert tracker.incomplete and tracker.artifacts() == ()


def test_unrecorded_call_has_explicit_local_cause_and_first_gap_is_sticky() -> None:
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    tracker.begin()
    assert payload(tracker)["first_incomplete_reason"] == "unrecorded_call"
    tracker.failed(tracker.begin(), RuntimeError("private body"))
    assert payload(tracker)["first_incomplete_call"] == 1
    assert payload(tracker)["first_incomplete_phase"] == "scope_tracker"


def test_tracker_memory_limit_is_local_evidence_limit_and_does_not_stop_calls() -> None:
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    tracker._bytes = 2_000_000
    receive(tracker)
    tracker.failed(tracker.begin(), RuntimeError("private"))
    assert payload(tracker)["first_incomplete_call"] == 1
    assert payload(tracker)["first_incomplete_reason"] == "evidence_limit"
    assert tracker.call_count == 2 and tracker.artifacts() == ()


def test_scope_artifact_recorder_failure_is_safe_local_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    receive(tracker)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private provider stack")

    monkeypatch.setattr(tracker, "_artifact", broken)
    assert tracker.artifacts(include_received_only=True) == ()
    assert payload(tracker)["first_incomplete_phase"] == "scope_tracker"
    assert payload(tracker)["first_incomplete_reason"] == "evidence_invalid"


def test_legacy_completion_defaults_keep_complete_scope_semantics() -> None:
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    completion = GatewayCompletion(response=ModelResponse(text="accepted"), deployment_id="primary",
                                   logical_model="primary", provider_id="provider",
                                   provider_model="provider/primary", attempted_logical_models=("primary",))
    tracker.received(tracker.begin(), request(), completion)
    assert payload(tracker) == {}
    assert not tracker.incomplete
    assert tracker.artifacts(include_received_only=True)


def test_pending_call_event_roundtrip_contains_only_local_enums_and_known_counts() -> None:
    runtime = DirectRuntime(cast(Any, object()), logical_model="primary")
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    tracker.begin()
    runtime._model_scope_tracker = tracker
    event = runtime._model_scope_events(run_id=tracker._run_id, sequence=2)[0]
    rebuilt = RunEvent.from_payload(event.to_payload())
    assert rebuilt.payload["first_incomplete_phase"] == "scope_tracker"
    assert rebuilt.payload["first_incomplete_reason"] == "unrecorded_call"


async def test_direct_strict_completion_cannot_erase_a_successful_fallback_gap() -> None:
    gateway = make_gateway(OutcomesTransport([ModelResponse(text="accepted")]),
                           CapacityStub([CapacityQueueFull("private"), lease("backup")]),
                           models=("primary", "backup"))
    runtime = DirectRuntime(gateway, logical_model="primary")
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    runtime._model_scope_tracker = tracker
    result = await runtime._complete_with_scope(request())
    assert result.response.text == "accepted"
    assert tracker.incomplete and tracker.artifacts() == ()
    assert payload(tracker)["first_incomplete_phase"] == "pretransport_capacity"


async def test_cancelled_gateway_child_task_keeps_cause_in_parent_tracker() -> None:
    started = asyncio.Event()

    class BlockingTransport(TransportStub):
        async def complete(self, *args: Any) -> ModelResponse:
            started.set()
            return await super().complete(*args)

    capacity = CapacityStub([lease("primary")])
    gateway = make_gateway(BlockingTransport(capacity.events, block=asyncio.Event()), capacity)
    runtime = DirectRuntime(gateway, logical_model="primary")
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    runtime._model_scope_tracker = tracker
    pending = asyncio.create_task(runtime._complete_with_scope(request()))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert tracker.incomplete and tracker.artifacts() == ()
    assert payload(tracker)["first_incomplete_phase"] == "cancellation"


async def test_concurrent_gateway_observers_do_not_cross_scopes() -> None:
    async def run(fail: bool) -> ModelScopeTracker:
        capacity = CapacityStub([lease("primary")])
        if fail:
            capacity.release_error = CapacityBackendError("private")
        runtime = DirectRuntime(make_gateway(OutcomesTransport([ModelResponse(text="accepted")]), capacity),
                                logical_model="primary")
        tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
        runtime._model_scope_tracker = tracker
        try:
            await runtime._complete_with_scope(request())
        except ModelGatewayError:
            assert fail
        return tracker

    failed, complete = await asyncio.gather(run(True), run(False))
    assert payload(failed)["first_incomplete_phase"] == "cleanup"
    assert payload(complete) == {} and not complete.incomplete
    assert complete.artifacts(include_received_only=True)


def test_scope_recorder_failure_during_received_metadata_is_local_and_safe() -> None:
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    completion = GatewayCompletion(response=ModelResponse(text="private body"),
                                   deployment_id="primary", logical_model="primary",
                                   provider_id="provider", provider_model="provider/private\ntext")
    tracker.received(tracker.begin(), request(), completion)
    assert tracker.incomplete and tracker.artifacts() == ()
    assert payload(tracker)["first_incomplete_phase"] == "scope_tracker"
    assert payload(tracker)["first_incomplete_reason"] == "evidence_invalid"
