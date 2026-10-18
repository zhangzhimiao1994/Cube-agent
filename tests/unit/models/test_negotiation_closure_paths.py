from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.failure_receipt import get_gateway_failure_receipt
from agent_hub.models.gateway import (
    GatewayRejectedOutput,
    ModelGatewayError,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.litellm_client import ModelClientError, ModelTransportError
from agent_hub.models.types import TokenUsage
from agent_hub.runtime.contracts import EventKind, RunEvent, TaskContext
from agent_hub.runtime.direct import _retryable_gateway_failure
from agent_hub.runtime.failure_reason import (
    safe_runtime_failure_diagnostic,
    safe_runtime_failure_reason,
)
from agent_hub.runtime.hybrid import HybridPlan, HybridRuntime, HybridUpgrade
from tests.contracts.test_required_tool_transport import provider_response, request
from tests.unit.models.test_negotiation_fallback_boundary import (
    assert_selected_only,
    gateway_for,
    rejected,
)
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, final_zip_artifact


class NegotiatedFailureRuntime(MultiArtifactRuntime):
    def __init__(self, mode: TaskMode, second: str, *, timeout: float = 3) -> None:
        super().__init__(mode, ())
        self.timeout = timeout
        self.wire: list[httpx.Request] = []
        self.second_entered = asyncio.Event()
        self.error: Exception | None = None

        async def handle(req: httpx.Request) -> httpx.Response:
            self.wire.append(req)
            if len(self.wire) == 1:
                return rejected()
            if len(self.wire) > 2:
                return httpx.Response(
                    200, json=provider_response("chat_completions", "project_inspect"),
                )
            self.second_entered.set()
            if second == "hang":
                await asyncio.Future[None]()
                raise AssertionError("cancelled HTTP cannot return a result")
            if second == "unknown":
                raise RuntimeError("PRIVATE_UNKNOWN_ADAPTER_FAILURE")
            return httpx.Response(
                200, json=provider_response(
                    "chat_completions", None if second == "text" else "project_verify",
                ),
            )

        self.gateway, self.capacity, self.secret = gateway_for(handle)

    async def complete(self) -> None:
        await self.gateway.complete_with_context(request(
            "chat_completions", required_tool="project_inspect", timeout_seconds=self.timeout,
        ))

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.contexts.append(context)
        try:
            await self.complete()
        except (ModelClientError, ModelTransportError, ModelGatewayError) as error:
            self.error = error
            yield RunEvent(
                kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=context.run_id,
                reason=safe_runtime_failure_reason(error),
                payload=safe_runtime_failure_diagnostic(error),
            )
            return
        yield RunEvent(
            kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=context.run_id,
            reason="explicit_completion",
        )


def context() -> TaskContext:
    return TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.HYBRID,
        request="Build and verify a project", timeout_seconds=10,
    )


def assert_stop_diagnostic(error: Exception) -> None:
    diagnostic = safe_runtime_failure_diagnostic(error)
    assert diagnostic["retryable"] is False
    assert diagnostic["error_code"] == "model.request_negotiation_exhausted"
    assert "PRIVATE_UNKNOWN_ADAPTER_FAILURE" not in json.dumps(diagnostic)


async def test_outer_deadline_during_second_http_cannot_enable_direct_retry() -> None:
    child = NegotiatedFailureRuntime(TaskMode.DIRECT, "hang", timeout=2)
    with pytest.raises(Exception) as caught:
        await child.complete()
    assert child.second_entered.is_set()
    assert_selected_only(child.wire, child.capacity, child.secret)
    assert not _retryable_gateway_failure(caught.value)
    scope = get_gateway_scope_diagnostic(caught.value)
    assert scope is not None
    assert all(item[1] is None and not item[3] for item in child.capacity.records)
    receipt = get_gateway_failure_receipt(caught.value)
    assert receipt is None or not receipt.history_complete
    assert_stop_diagnostic(caught.value)


async def test_outer_deadline_during_second_http_stops_hybrid_preflight_dispatch() -> None:
    child = NegotiatedFailureRuntime(TaskMode.DISCUSS, "hang", timeout=2)
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, ())
    synth = MultiArtifactRuntime(TaskMode.DIRECT, ())
    runtime = HybridRuntime(
        dispatch, child, synth,
        plan=HybridPlan(upgrade=HybridUpgrade.DISCUSS_DISPATCH_DISCUSS),
    )
    events = [event async for event in runtime.run(context())]
    assert child.second_entered.is_set()
    assert_selected_only(child.wire, child.capacity, child.secret)
    assert dispatch.contexts == []
    assert synth.contexts == []
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert child.error is not None
    assert_stop_diagnostic(child.error)


async def test_user_cancel_during_second_http_still_cancels_hybrid_without_completion() -> None:
    child = NegotiatedFailureRuntime(TaskMode.DISCUSS, "hang")
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, ())
    synth = MultiArtifactRuntime(TaskMode.DIRECT, ())
    runtime = HybridRuntime(
        dispatch, child, synth,
        plan=HybridPlan(upgrade=HybridUpgrade.DISCUSS_DISPATCH_DISCUSS),
    )
    events: list[RunEvent] = []

    async def collect() -> None:
        async for event in runtime.run(context()):
            events.append(event)

    pending = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(child.second_entered.wait(), timeout=5)
        assert not pending.done()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await pending
        assert_selected_only(child.wire, child.capacity, child.secret)
        assert child.capacity.records == []
        assert get_gateway_failure_receipt(caught.value) is None
        scope = get_gateway_scope_diagnostic(caught.value)
        assert scope is not None
        assert scope.phase.value == "cancellation"
        assert scope.reason.value == "cancelled"
        assert dispatch.contexts == []
        assert synth.contexts == []
        assert events[-1].kind is EventKind.RUNTIME_CANCELLED
        assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("second", ["unknown", "text", "wrong_tool"])
@pytest.mark.parametrize("stage", [TaskMode.DISCUSS, TaskMode.DIRECT])
async def test_second_failure_after_zip_stays_failed_with_original_evidence(
    second: str, stage: TaskMode,
) -> None:
    child = NegotiatedFailureRuntime(stage, second)
    zip_result = final_zip_artifact()
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, (zip_result,))
    discussion = child if stage is TaskMode.DISCUSS else MultiArtifactRuntime(TaskMode.DISCUSS, ())
    synth = child if stage is TaskMode.DIRECT else MultiArtifactRuntime(TaskMode.DIRECT, ())
    runtime = HybridRuntime(dispatch, discussion, synth)
    events = [event async for event in runtime.run(context())]
    assert_selected_only(child.wire, child.capacity, child.secret)
    assert any(event.artifact == zip_result for event in events)
    error = child.error
    assert error is not None
    if second == "unknown":
        assert isinstance(error, ModelClientError | ModelGatewayError)
        assert not isinstance(error, ModelTransportError | GatewayRejectedOutput)
        assert getattr(error, "status_code", None) is None
        assert [(item[1], item[3]) for item in child.capacity.records] == [(None, False)]
        scope = get_gateway_scope_diagnostic(error)
        assert scope is not None
        assert scope.phase.value == "unknown_adapter"
        assert scope.reason.value == "unknown_failure"
    else:
        assert isinstance(error, GatewayRejectedOutput)
        evidence = error.evidence
        assert evidence is not None
        assert evidence.usage == TokenUsage(2, 3, 5)
        assert evidence.usage_status == "known"
        assert evidence.status == "completed"
        assert evidence.reason == "invalid_tool"
        assert evidence.final_text is None
        assert not evidence.correction_eligible
        assert error.deployment_id == "primary"
        assert not error.fallback_used
        assert all(not item[3] for item in child.capacity.records)
    receipt = get_gateway_failure_receipt(error)
    assert receipt is None or not receipt.history_complete
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    if stage is TaskMode.DISCUSS:
        assert synth.contexts == []
    assert_stop_diagnostic(error)
    assert "PRIVATE_UNKNOWN_ADAPTER_FAILURE" not in json.dumps(
        [event.to_payload() for event in events], default=str,
    )
