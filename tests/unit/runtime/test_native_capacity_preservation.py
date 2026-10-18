from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityBackendError, CapacityPool
from agent_hub.models.failure_receipt import get_gateway_failure_receipt
from agent_hub.models.gateway import (
    GatewayCompletion,
    ModelGateway,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    ModelOutcomeUncertain,
    ToolOutcomeUncertain,
    _DiscussionDurability,
)
from agent_hub.runtime.contracts import EventKind, RunEvent, TaskContext
from agent_hub.runtime.direct import DirectRuntime, Gateway, RuntimeExecutionError
from agent_hub.runtime.failure_reason import (
    gateway_scope_failure_payload,
    runtime_failure_diagnostic_from_reason,
    safe_runtime_failure_diagnostic,
)
from agent_hub.runtime.hybrid import HybridRuntime
from tests.unit.models.test_capacity import fingerprint
from tests.unit.models.test_capacity_diagnostic import FailingRedis
from tests.unit.models.test_failure_receipt import OutcomesTransport, make_gateway
from tests.unit.models.test_gateway import (
    CapacityStub,
    SecretStub,
    TransportStub,
    deployment,
    lease,
    request,
)
from tests.unit.runtime.test_direct_prompt import RecordingCapabilityGateway
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime

REASON = "model gateway failed: model capacity backend failed"
PRIVATE = "PRIVATE_PASSWORD redis://private.invalid provider body"
FIELDS = {
    "gateway_scope_phase": "pretransport_capacity",
    "gateway_scope_reason": "capacity_backend_failure",
    "gateway_scope_transport_entered_count": 0,
    "gateway_scope_failure_attempt_count": 0,
    "capacity_operation": "acquire",
    "capacity_backend_reason": "programming_error",
    "capacity_exception_category": "type_error",
    "capacity_timeout_origin": "none",
    "capacity_commit_state": "unknown",
}


def native_gateway() -> tuple[ModelGateway, FailingRedis, TransportStub]:
    selected = deployment("primary")
    redis = FailingRedis(5, TypeError(PRIVATE))
    capacity = CapacityPool(redis, deployments=[selected], fingerprint_resolver=fingerprint)
    events: list[object] = []
    transport = TransportStub(events)
    return ModelGateway(ModelRegistry([selected]), capacity, SecretStub(events), transport), redis, transport


def context(mode: TaskMode, *, batch: bool = False) -> TaskContext:
    return TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=mode, token_budget=50_000,
        request="Build a project with source and tests." if batch else "Answer briefly.",
        routing_decision={"project_scale": "small", "project_delivery": "workspace",
                          "artifact_strategy": "workspace_bundle"} if batch else {},
    )


def plan() -> DiscussionPlan:
    return DiscussionPlan(
        participants=(
            DiscussionParticipant(id="analyst", role="Analyst", goal="Analyze",
                                  logical_model="primary"),
            DiscussionParticipant(id="critic", role="Critic", goal="Review",
                                  logical_model="primary"),
        ), selector_model="primary", max_turns=4,
    )


class BatchThenFailure:
    def __init__(self, gateway: Gateway, batch: bool) -> None:
        self.gateway = gateway
        self.batch = batch
        self.calls = 0

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.calls += 1
        if self.batch and self.calls == 1:
            return GatewayCompletion(
                response=ModelResponse(text=json.dumps({"workspace_batch": {
                    "files": {"src/main.ts": "export {};\n"}, "complete": False,
                    "continuation": "remaining files",
                }}), usage=TokenUsage(10, 20, 30)),
                deployment_id="primary", logical_model=request.logical_model,
                provider_id="provider", provider_model="provider/primary",
            )
        return await self.gateway.complete_with_context(request)


@pytest.mark.parametrize("batch", [False, True])
async def test_direct_preserves_issued_capacity_before_both_error_scrubs(batch: bool) -> None:
    gateway, redis, transport = native_gateway()
    sequence = BatchThenFailure(gateway, batch)
    runtime = DirectRuntime(sequence, logical_model="primary", available_model_attempts=1,
                            capability_gateway=RecordingCapabilityGateway() if batch else None)
    with pytest.raises(RuntimeExecutionError) as caught:
        _ = [event async for event in runtime.run(context(TaskMode.DIRECT, batch=batch))]

    error = caught.value
    assert gateway_scope_failure_payload(error) == FIELDS
    assert get_gateway_scope_diagnostic(error) is not None
    assert get_gateway_failure_receipt(error) is None
    assert error.__cause__ is error.__context__ is None
    assert PRIVATE not in str(error)
    assert sequence.calls == (2 if batch else 1)
    assert redis.failed_calls == 1 and not transport.calls
    fields = safe_runtime_failure_diagnostic(error)
    public = _public_event_payload(RunEvent(
        kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(), reason=str(error),
        payload=fields,
    ).to_payload())
    public_payload = public["payload"]
    assert isinstance(public_payload, Mapping)
    assert all(public_payload[key] == value for key, value in FIELDS.items())
    assert PRIVATE not in json.dumps(public)


class LegacyFailure:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.calls += 1
        raise CapacityBackendError(PRIVATE)


async def test_direct_legacy_failure_keeps_exact_diagnostic_and_attempt_count() -> None:
    gateway = LegacyFailure()
    runtime = DirectRuntime(gateway, logical_model="primary", available_model_attempts=1)
    with pytest.raises(RuntimeExecutionError) as caught:
        _ = [event async for event in runtime.run(context(TaskMode.DIRECT))]
    assert gateway_scope_failure_payload(caught.value) == {}
    assert safe_runtime_failure_diagnostic(caught.value) == runtime_failure_diagnostic_from_reason(REASON)
    assert gateway.calls == 1


async def test_autogen_keeps_native_diagnostic_after_framework_wrap_and_not_in_checkpoint() -> None:
    gateway, redis, transport = native_gateway()
    runtime = AutoGenDiscussionRuntime(gateway, plan())
    task = context(TaskMode.DISCUSS)
    events = [event async for event in runtime.run(task)]
    failed = events[-1]
    assert failed.kind is EventKind.RUNTIME_FAILED and failed.reason == REASON
    assert dict(failed.payload) == {**runtime_failure_diagnostic_from_reason(REASON), **FIELDS}
    assert redis.failed_calls == 1 and not transport.calls
    assert PRIVATE not in json.dumps(_public_event_payload(failed.to_payload()))
    checkpoint = await runtime.save_checkpoint()
    encoded = json.dumps(checkpoint.to_payload())
    assert "capacity_" not in encoded and "gateway_scope_" not in encoded
    ledger = checkpoint.state["model_ledger"]
    assert isinstance(ledger, tuple) and isinstance(ledger[0], Mapping)
    assert ledger[0]["status"] == "running"

    recovered_gateway = LegacyFailure()
    recovered = AutoGenDiscussionRuntime(recovered_gateway, plan())
    await recovered.restore_checkpoint(checkpoint)
    replay = [event async for event in recovered.run(task.model_copy(update={"checkpoint": checkpoint}))]
    assert replay[-1].kind is EventKind.RUNTIME_FAILED
    assert replay[-1].reason == "model_outcome_uncertain"
    assert dict(replay[-1].payload) == {}
    assert recovered_gateway.calls == 0


async def test_autogen_legacy_failure_keeps_empty_payload_on_reused_runtime() -> None:
    gateway, _, _ = native_gateway()
    sequence = BatchThenFailure(gateway, False)
    runtime = AutoGenDiscussionRuntime(sequence, plan())
    first = [event async for event in runtime.run(context(TaskMode.DISCUSS))]
    assert first[-1].payload.get("capacity_operation") == "acquire"
    legacy = LegacyFailure()
    sequence.gateway = legacy
    second = [event async for event in runtime.run(context(TaskMode.DISCUSS))]
    assert second[-1].kind is EventKind.RUNTIME_FAILED and second[-1].reason == REASON
    assert dict(second[-1].payload) == {}
    assert legacy.calls == 1


@pytest.mark.parametrize("boundary", ["model", "tool"])
async def test_autogen_unknown_replaces_reason_and_clears_previous_capacity_payload(
    boundary: str,
) -> None:
    gateway, _, _ = native_gateway()
    with pytest.raises(CapacityBackendError) as caught:
        await gateway.complete(request())
    durability = _DiscussionDurability(AsyncMock(), AsyncMock(), AsyncMock(), lambda _: None,
                                      run_id=uuid4())

    async def before() -> None:
        if boundary == "model":
            await durability.before_model(request())
        else:
            await durability.before_tool(actor="analyst", name="lookup",
                                         arguments_hash="a" * 64, replay_safe=False)

    await before()
    durability.fail_model(REASON, source=caught.value)
    assert durability.failure_payload == FIELDS
    durability.fail_model("unaccounted_usage")
    assert durability.failure_reason == REASON and durability.failure_payload == FIELDS
    with pytest.raises(ModelOutcomeUncertain if boundary == "model" else ToolOutcomeUncertain):
        await before()
    assert durability.failure_reason == f"{boundary}_outcome_uncertain"
    assert durability.failure_payload == {}


@pytest.mark.parametrize("mode", [TaskMode.DIRECT, TaskMode.DISCUSS])
async def test_noncapacity_issued_scope_adds_no_public_failure_fields(mode: TaskMode) -> None:
    gateway = make_gateway(OutcomesTransport([ModelTransportError(PRIVATE)]),
                           CapacityStub([lease("primary")]))
    if mode is TaskMode.DIRECT:
        runtime = DirectRuntime(gateway, logical_model="primary", available_model_attempts=1)
        with pytest.raises(RuntimeExecutionError) as caught:
            _ = [event async for event in runtime.run(context(mode))]
        scope = get_gateway_scope_diagnostic(caught.value)
        assert scope is not None and scope.capacity_backend is None
        assert gateway_scope_failure_payload(caught.value) == {}
        assert safe_runtime_failure_diagnostic(caught.value) == runtime_failure_diagnostic_from_reason(
            str(caught.value))
    else:
        discussion = AutoGenDiscussionRuntime(gateway, plan())
        events = [event async for event in discussion.run(context(mode))]
        assert events[-1].kind is EventKind.RUNTIME_FAILED
        assert events[-1].reason == "model gateway failed: model transport failed"
        assert dict(events[-1].payload) == {}


class FailureEventRuntime(MultiArtifactRuntime):
    def __init__(self, payload: dict[str, Any], *, tamper: bool = False) -> None:
        super().__init__(TaskMode.DISPATCH, ())
        self.payload = payload
        self.tamper = tamper

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=context.run_id,
                         reason=REASON, payload=self.payload)
        if self.tamper:
            event = event.model_copy(update={"payload": {**self.payload, "capacity_operation": PRIVATE}})
        yield event


@pytest.mark.parametrize("source", ["event", "exception"])
async def test_hybrid_parent_retains_only_validated_native_capacity_fields(source: str) -> None:
    gateway, redis, transport = native_gateway()
    child: MultiArtifactRuntime
    if source == "event":
        with pytest.raises(CapacityBackendError) as caught:
            await gateway.complete(request())
        child = FailureEventRuntime({**safe_runtime_failure_diagnostic(caught.value),
                                     "actor": "analyst", "step_id": "native_failure"})
    else:
        class RaisedFailure(MultiArtifactRuntime):
            async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
                await gateway.complete(request())
                yield cast(RunEvent, None)  # pragma: no cover
        child = RaisedFailure(TaskMode.DISPATCH, ())
    runtime = HybridRuntime(child, MultiArtifactRuntime(TaskMode.DISCUSS, ()),
                            MultiArtifactRuntime(TaskMode.DIRECT, ()))
    events = [event async for event in runtime.run(context(TaskMode.HYBRID))]
    failed = events[-1]
    assert failed.kind is EventKind.RUNTIME_FAILED
    reason = f"hybrid dispatch failed: {REASON}"
    assert failed.reason == reason
    assert dict(failed.payload) == {**runtime_failure_diagnostic_from_reason(reason), **FIELDS}
    assert "actor" not in failed.payload and "step_id" not in failed.payload
    assert redis.failed_calls == 1 and not transport.calls
    assert PRIVATE not in json.dumps(_public_event_payload(failed.to_payload()))


async def test_hybrid_rejects_diagnostic_that_bypasses_child_event_validation() -> None:
    runtime = HybridRuntime(
        FailureEventRuntime({**runtime_failure_diagnostic_from_reason(REASON), **FIELDS}, tamper=True),
        MultiArtifactRuntime(TaskMode.DISCUSS, ()), MultiArtifactRuntime(TaskMode.DIRECT, ()),
    )
    events = [event async for event in runtime.run(context(TaskMode.HYBRID))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(key.startswith(("capacity_", "gateway_scope_")) for key in events[-1].payload)
    assert PRIVATE not in json.dumps(_public_event_payload(events[-1].to_payload()))


async def test_hybrid_legacy_child_failure_keeps_empty_parent_payload() -> None:
    runtime = HybridRuntime(FailureEventRuntime({}), MultiArtifactRuntime(TaskMode.DISCUSS, ()),
                            MultiArtifactRuntime(TaskMode.DIRECT, ()))
    events = [event async for event in runtime.run(context(TaskMode.HYBRID))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == f"hybrid dispatch failed: {REASON}"
    assert dict(events[-1].payload) == {}
