"""Issued wait diagnostics are observations, never provider or retry authority."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import replace
from typing import Any, Literal, cast

import pytest
from autogen_core import CancellationToken
from autogen_core.models import UserMessage

from agent_hub.models.capacity import CapacityLease
from agent_hub.models.gateway import (
    CapacityController,
    GatewayCompletion,
    GatewayScopeDiagnostic,
    ModelGateway,
    ScopeIncompletePhase,
    ScopeIncompleteReason,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment, ModelRequest
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    GatewayChatCompletionClient,
    ModelOutcomeUncertain,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, TaskContext
from agent_hub.runtime.failure_reason import gateway_scope_failure_payload
from tests.unit.models.test_gateway import (
    CapacityStub,
    SecretStub,
    TransportStub,
    deployment,
    lease,
    request,
)
from tests.unit.runtime.autogen.test_dynamic_limits import _context, _plan
from tests.unit.runtime.autogen.test_model_tool_bridge import _durability
from tests.unit.runtime.test_generated_draft_autogen import TOOLS, DraftGateway

Phase = Literal["capacity", "transport"]
PRIVATE = "PRIVATE_CANCEL_DATA_DO_NOT_EXPORT"


class WaitingCapacity(CapacityStub):
    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()

    async def acquire(
        self, candidates: Sequence[Deployment], wait_timeout: float, *,
        estimated_tokens: int | Mapping[str, int],
    ) -> CapacityLease:
        del candidates, wait_timeout, estimated_tokens
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("capacity fixture must be cancelled")


class WaitFixture:
    def __init__(self, phase: Phase) -> None:
        self.phase = phase
        self.events: list[object] = []
        self.capacity = WaitingCapacity() if phase == "capacity" else CapacityStub([lease("primary")])
        self.secrets = SecretStub(self.events)
        self.transport = TransportStub(self.events, block=asyncio.Event())
        self.gateway = ModelGateway(
            ModelRegistry([deployment("primary")]), cast(CapacityController, self.capacity),
            self.secrets, self.transport,
        )

    async def wait_started(self) -> None:
        if isinstance(self.capacity, WaitingCapacity):
            await self.capacity.started.wait()
        else:
            while not self.transport.calls:
                await asyncio.sleep(0)

    async def cancelled_error(self) -> asyncio.CancelledError:
        pending = asyncio.create_task(self.gateway.complete_with_context(request()))
        try:
            await asyncio.wait_for(self.wait_started(), timeout=2)
            pending.cancel(PRIVATE)
            try:
                await pending
            except asyncio.CancelledError as error:
                return error
            raise AssertionError("real ModelGateway must preserve cancellation")
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("phase,entered", [("capacity", 0), ("transport", 1)])
async def test_real_gateway_cancel_projects_issued_scope_without_capacity_details(
    phase: Phase, entered: int,
) -> None:
    fixture = WaitFixture(phase)
    error = await fixture.cancelled_error()
    scope = get_gateway_scope_diagnostic(error)
    assert scope is not None and scope.capacity_backend is None
    assert scope.transport_entered_count == entered and scope.failure_attempt_count == 0
    assert gateway_scope_failure_payload(error) == {}, "legacy default must stay unchanged"
    fields = gateway_scope_failure_payload(error, include_noncapacity=True)
    assert fields == {
        "gateway_scope_phase": "cancellation",
        "gateway_scope_reason": "cancelled",
        "gateway_scope_transport_entered_count": entered,
        "gateway_scope_failure_attempt_count": 0,
    }
    assert PRIVATE not in json.dumps(fields)
    assert not any(key.startswith("capacity_") for key in fields)
    assert len(fixture.transport.calls) == entered
    assert len(fixture.capacity.releases) == entered
    if phase == "capacity":
        assert not fixture.secrets.references


@pytest.mark.parametrize("phase,entered", [("capacity", 0), ("transport", 1)])
async def test_client_cancel_retains_safe_scope_and_unknown_cannot_retry(
    phase: Phase, entered: int,
) -> None:
    fixture, durable = WaitFixture(phase), _durability()
    client = GatewayChatCompletionClient(
        fixture.gateway, "primary", DiscussionUsage(), durability=durable,
        request_timeout=lambda: 30.0,
    )
    messages = [UserMessage(content="private request must not enter diagnostics", source="user")]
    token = CancellationToken()
    pending = asyncio.create_task(client.create(messages, cancellation_token=token))
    try:
        await asyncio.wait_for(fixture.wait_started(), timeout=2)
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=2)
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert durable.failure_reason is None
    assert durable.failure_payload == {
        "gateway_scope_phase": "cancellation",
        "gateway_scope_reason": "cancelled",
        "gateway_scope_transport_entered_count": entered,
        "gateway_scope_failure_attempt_count": 0,
    }
    assert not durable._model_lock.locked()
    assert durable.model_entries[0]["status"] == "running"
    before = tuple(fixture.capacity.events)
    restored = _durability(entries=tuple(durable.model_entries), artifacts=durable.artifacts)
    fresh = GatewayChatCompletionClient(
        fixture.gateway, "primary", DiscussionUsage(), durability=restored,
        request_timeout=lambda: 30.0,
    )
    with pytest.raises(ModelOutcomeUncertain):
        await fresh.create(messages)
    assert len(fixture.transport.calls) == entered
    assert tuple(fixture.capacity.events) == before
    assert not restored._model_lock.locked()


def engineering_runtime(fixture: WaitFixture, seconds: float) -> tuple[AutoGenDiscussionRuntime, TaskContext]:
    context = _context(timeout_seconds=seconds)
    context = context.model_copy(update={"routing_decision": {
        "engineering_delivery_contract_version": 1, "engineering_action_version": 1,
        "sandbox_profile": "workspace_write", "project_id": "wait-project",
        "workspace_session_id": "wait-session",
    }})
    plan = _plan(wall_time_seconds=seconds)
    plan = replace(plan, selector_model="primary", participants=tuple(
        replace(participant, logical_model="primary", allowed_tools=TOOLS if index == 0 else ())
        for index, participant in enumerate(plan.participants)
    ))
    tools = DraftGateway(context)
    return AutoGenDiscussionRuntime(
        fixture.gateway, plan, capability_gateway=tools, harness_tool_gateway=tools,
    ), context


async def collect(
    runtime: AutoGenDiscussionRuntime, context: TaskContext, events: list[RunEvent],
) -> None:
    try:
        async for event in runtime.run(context):
            events.append(event)
    except asyncio.CancelledError:
        return


@pytest.mark.parametrize("phase,entered", [("capacity", 0), ("transport", 1)])
async def test_engineering_wall_time_stays_failed_with_budget_and_wait_metadata(
    phase: Phase, entered: int,
) -> None:
    fixture = WaitFixture(phase)
    runtime, context = engineering_runtime(fixture, 0.5)
    events: list[RunEvent] = []
    await asyncio.wait_for(collect(runtime, context, events), timeout=5)
    terminal = [event for event in events if event.kind in {
        EventKind.RUNTIME_FAILED, EventKind.RUNTIME_CANCELLED, EventKind.RUNTIME_COMPLETED,
    }]
    assert len(terminal) == 1 and terminal[0].kind is EventKind.RUNTIME_FAILED
    failed = terminal[0]
    assert failed.reason == "engineering_delivery_missing"
    assert failed.payload.get("termination_reason") == "wall_time"
    assert failed.payload.get("error_code") == "runtime.discussion_budget_exhausted"
    assert failed.payload.get("error_stage") == "runtime_budget"
    assert failed.payload.get("error_category") == "budget_exhausted"
    assert failed.payload.get("retryable") is False
    assert failed.payload.get("gateway_scope_phase") in {"cancellation", "outer_deadline"}
    assert failed.payload.get("gateway_scope_reason") in {"cancelled", "deadline_exhausted"}
    assert failed.payload.get("gateway_scope_transport_entered_count") == entered
    assert failed.payload.get("gateway_scope_failure_attempt_count") == 0
    assert RunEvent.from_payload(failed.to_payload()) == failed
    assert len(fixture.transport.calls) == entered
    assert not any(event.kind in {EventKind.TOOL_STARTED, EventKind.TOOL_COMPLETED} for event in events)
    assert PRIVATE not in json.dumps(failed.to_payload())
    assert context.timeout_seconds == 0.5 and context.token_budget == 100


@pytest.mark.parametrize("source,absolute,expected", [
    ("project_scale_soft_budget", 10.0, "soft"),
    ("project_scale_soft_budget", 0.15, "absolute"),
    ("explicit", 10.0, "explicit"),
])
async def test_wall_failure_identifies_soft_absolute_or_explicit_limit(
    source: str, absolute: float, expected: str,
) -> None:
    fixture = WaitFixture("transport")
    runtime, context = engineering_runtime(fixture, 0.15)
    class BlockedModel:
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            await asyncio.Event().wait()
            raise AssertionError("the runtime must cancel the pending model")

    runtime._gateway = BlockedModel()
    context = context.model_copy(update={"routing_decision": {**context.routing_decision,
        "engineering_progress_budget_version": 1, "runtime_timeout_source": source,
        "runtime_timeout_soft_seconds": 0.15, "runtime_timeout_absolute_seconds": absolute}})
    events: list[RunEvent] = []
    await asyncio.wait_for(collect(runtime, context, events), timeout=5)
    failure = next(event for event in events if event.kind is EventKind.RUNTIME_FAILED)
    assert failure.reason == "engineering_delivery_missing", failure.reason
    assert failure.payload.get("runtime_timeout_kind") == expected, failure.to_payload()
    soft_remaining = failure.payload.get("remaining_soft_timeout_seconds")
    assert isinstance(soft_remaining, float) and 0 <= soft_remaining <= 0.02
    remaining = failure.payload.get("remaining_absolute_timeout_seconds")
    assert isinstance(remaining, float)
    assert (remaining > 8) if expected == "soft" else 0 <= remaining <= 0.02
    assert failure.payload.get("engineering_work_progress_units") == 0


@pytest.mark.parametrize("phase,entered", [("capacity", 0), ("transport", 1)])
async def test_user_cancel_keeps_cancelled_identity_without_budget_failure(
    phase: Phase, entered: int,
) -> None:
    fixture = WaitFixture(phase)
    runtime, context = engineering_runtime(fixture, 30.0)
    events: list[RunEvent] = []
    pending = asyncio.create_task(collect(runtime, context, events))
    try:
        await asyncio.wait_for(fixture.wait_started(), timeout=2)
        await runtime.cancel()
        await asyncio.wait_for(pending, timeout=3)
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    terminal = [event for event in events if event.kind in {
        EventKind.RUNTIME_FAILED, EventKind.RUNTIME_CANCELLED, EventKind.RUNTIME_COMPLETED,
    }]
    assert len(terminal) == 1 and terminal[0].kind is EventKind.RUNTIME_CANCELLED
    cancelled = terminal[0]
    assert cancelled.reason is None
    assert cancelled.payload.get("gateway_scope_phase") == "cancellation"
    assert cancelled.payload.get("gateway_scope_reason") == "cancelled"
    assert cancelled.payload.get("gateway_scope_transport_entered_count") == entered
    assert cancelled.payload.get("gateway_scope_failure_attempt_count") == 0
    assert not {"error_code", "error_stage", "error_category", "termination_reason", "retryable"} & cancelled.payload.keys()
    assert RunEvent.from_payload(cancelled.to_payload()) == cancelled
    assert len(fixture.transport.calls) == entered


async def test_first_safe_cancel_diagnostic_is_not_overwritten_without_failure_reason() -> None:
    first = await WaitFixture("capacity").cancelled_error()
    second = await WaitFixture("transport").cancelled_error()
    durable = _durability()
    durable.fail_model(source=first)
    saved = dict(durable.failure_payload)
    assert saved.get("gateway_scope_transport_entered_count") == 0
    durable.fail_model(source=second)
    assert durable.failure_reason is None
    assert durable.failure_payload == saved


async def test_existing_failure_reason_and_payload_are_not_rebound_by_cancel() -> None:
    error = await WaitFixture("transport").cancelled_error()
    durable = _durability()
    durable.fail_model("discussion_failed")
    saved = dict(durable.failure_payload)
    durable.fail_model(source=error)
    assert durable.failure_reason == "discussion_failed"
    assert durable.failure_payload == saved


@pytest.mark.parametrize("forgery", ["absent", "unissued", "copied"])
async def test_forged_cancel_metadata_is_not_public_or_durable(forgery: str) -> None:
    error = asyncio.CancelledError(PRIVATE)
    if forgery == "unissued":
        error.__dict__["_gateway_scope_diagnostic"] = (
            object(), error, GatewayScopeDiagnostic(
                ScopeIncompletePhase.CANCELLATION, ScopeIncompleteReason.CANCELLED, 1, 0,
            ),
        )
    elif forgery == "copied":
        issued = await WaitFixture("transport").cancelled_error()
        error.__dict__.update(issued.__dict__)
    assert get_gateway_scope_diagnostic(error) is None
    assert gateway_scope_failure_payload(error) == {}
    assert gateway_scope_failure_payload(error, include_noncapacity=True) == {}
    durable = _durability()
    durable.fail_model(source=error)
    assert durable.failure_payload == {} and durable.failure_reason is None


@pytest.mark.parametrize("key,value", [
    ("termination_reason", "cancelled"),
    ("error_code", "model.provider_transport_failed"),
    ("error_stage", "model_provider"),
    ("error_category", "transient"),
    ("retryable", True),
])
def test_wall_budget_metadata_rejects_mismatched_classification(key: str, value: JsonValue) -> None:
    payload: dict[str, JsonValue] = {
        "termination_reason": "wall_time",
        "error_code": "runtime.discussion_budget_exhausted",
        "error_stage": "runtime_budget",
        "error_category": "budget_exhausted",
        "error_summary": "Engineering delivery was not completed before the time budget.",
        "suggested_action": "Inspect wait diagnostics without replaying the run.",
        "retryable": False,
    }
    valid = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=_context().run_id,
                     reason="engineering_delivery_missing", payload=payload)
    assert RunEvent.from_payload(valid.to_payload()) == valid
    with pytest.raises(ValueError):
        RunEvent(kind=valid.kind, sequence=1, run_id=valid.run_id,
                 reason=valid.reason, payload={**payload, key: value})


@pytest.mark.parametrize("key,value", [
    ("runtime_timeout_kind", True), ("runtime_timeout_kind", "upstream"),
    ("remaining_soft_timeout_seconds", True), ("remaining_soft_timeout_seconds", -1),
    ("remaining_soft_timeout_seconds", 100), ("remaining_absolute_timeout_seconds", "300"),
    ("remaining_absolute_timeout_seconds", 3601), ("remaining_absolute_timeout_seconds", -1),
    ("engineering_work_progress_units", True), ("engineering_work_progress_units", -1),
    ("engineering_work_progress_units", 2**63), ("termination_reason", None),
])
def test_timeout_diagnostic_rejects_unbounded_or_inconsistent_values(key: str, value: JsonValue) -> None:
    payload: dict[str, JsonValue] = {
        "termination_reason": "wall_time", "error_code": "runtime.discussion_budget_exhausted",
        "error_stage": "runtime_budget", "error_category": "budget_exhausted",
        "error_summary": "Soft budget expired", "suggested_action": "Inspect progress", "retryable": False,
        "runtime_timeout_kind": "soft", "remaining_soft_timeout_seconds": 0.0,
        "remaining_absolute_timeout_seconds": 60.0, "engineering_work_progress_units": 1,
    }
    valid = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=_context().run_id,
        reason="engineering_delivery_missing", payload=payload)
    assert RunEvent.from_payload(valid.to_payload()) == valid
    with pytest.raises(ValueError):
        RunEvent(kind=valid.kind, sequence=1, run_id=valid.run_id,
            reason=valid.reason, payload={**payload, key: value})
    for missing in ("runtime_timeout_kind", "remaining_soft_timeout_seconds", "remaining_absolute_timeout_seconds"):
        with pytest.raises(ValueError):
            RunEvent(kind=valid.kind, sequence=1, run_id=valid.run_id, reason=valid.reason,
                payload={name: item for name, item in payload.items() if name != missing})


@pytest.mark.parametrize("key,value", [
    ("error_code", "runtime.discussion_budget_exhausted"),
    ("termination_reason", "wall_time"),
    ("unrelated", "untrusted"),
    ("raw_body", PRIVATE),
])
def test_cancel_scope_rejects_failure_classification_and_unrelated_fields(key: str, value: JsonValue) -> None:
    payload: dict[str, JsonValue] = {
        "gateway_scope_phase": "cancellation",
        "gateway_scope_reason": "cancelled",
        "gateway_scope_transport_entered_count": 1,
        "gateway_scope_failure_attempt_count": 0,
    }
    valid = RunEvent(kind=EventKind.RUNTIME_CANCELLED, sequence=1, run_id=_context().run_id,
                     payload=payload)
    assert RunEvent.from_payload(valid.to_payload()) == valid
    with pytest.raises(ValueError):
        RunEvent(kind=valid.kind, sequence=1, run_id=valid.run_id, payload={**payload, key: value})


def test_cancel_scope_cannot_carry_a_failure_reason() -> None:
    with pytest.raises(ValueError):
        RunEvent(kind=EventKind.RUNTIME_CANCELLED, sequence=1, run_id=_context().run_id,
                 reason="engineering_delivery_missing", payload={
                     "gateway_scope_phase": "cancellation",
                     "gateway_scope_reason": "cancelled",
                     "gateway_scope_transport_entered_count": 0,
                     "gateway_scope_failure_attempt_count": 0,
                 })


def test_cancel_without_scope_still_rejects_arbitrary_payload() -> None:
    legacy = RunEvent(kind=EventKind.RUNTIME_CANCELLED, sequence=1, run_id=_context().run_id)
    assert RunEvent.from_payload(legacy.to_payload()) == legacy
    with pytest.raises(ValueError):
        RunEvent(kind=legacy.kind, sequence=1, run_id=legacy.run_id,
                 payload={"unrelated": PRIVATE})


@pytest.mark.parametrize("first", ["user", "wall"])
async def test_first_cause_survives_cleanup_crossing_wall_deadline(
    first: Literal["user", "wall"], monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = WaitFixture("transport")
    runtime, context = engineering_runtime(fixture, 0.5)
    cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
    original = runtime._await_autogen_cleanup
    delayed = False

    async def controlled_cleanup(awaitable: Coroutine[Any, Any, Any]) -> bool:
        nonlocal delayed
        outcome = await original(awaitable)
        if not delayed:
            delayed = True
            cleanup_started.set()
            while not release_cleanup.is_set():
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    # A second cancellation must not shorten this fixture's cleanup delay.
                    continue
        return outcome

    monkeypatch.setattr(runtime, "_await_autogen_cleanup", controlled_cleanup)
    events: list[RunEvent] = []
    pending = asyncio.create_task(collect(runtime, context, events))
    try:
        await asyncio.wait_for(fixture.wait_started(), timeout=2)
        deadline = runtime._wall_deadline
        assert deadline is not None and asyncio.get_running_loop().time() < deadline
        if first == "user":
            await runtime.cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        await asyncio.sleep(max(0.0, deadline - asyncio.get_running_loop().time()) + 0.05)
        assert asyncio.get_running_loop().time() >= deadline
        if first == "wall":
            await runtime.cancel()
        release_cleanup.set()
        await asyncio.wait_for(pending, timeout=3)
    finally:
        release_cleanup.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    terminal = [event for event in events if event.kind in {
        EventKind.RUNTIME_FAILED, EventKind.RUNTIME_CANCELLED, EventKind.RUNTIME_COMPLETED,
    }]
    assert len(terminal) == 1
    event = terminal[0]
    if first == "user":
        assert event.kind is EventKind.RUNTIME_CANCELLED
        assert event.reason is None
        assert "termination_reason" not in event.payload and "error_code" not in event.payload
    else:
        assert event.kind is EventKind.RUNTIME_FAILED
        assert event.reason == "engineering_delivery_missing"
        assert event.payload.get("termination_reason") == "wall_time"
        assert event.payload.get("error_code") == "runtime.discussion_budget_exhausted"
    assert len(fixture.transport.calls) == 1
    assert context.timeout_seconds == 0.5
