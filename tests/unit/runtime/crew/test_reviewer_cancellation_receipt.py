from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal

import pytest

from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    GatewayResponseCancelled,
)
from agent_hub.models.types import ModelRequest, ModelResponse, RejectedOutputEvidence, TokenUsage
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    _context,
    _reviewed_step_plan,
)
from tests.unit.runtime.crew.test_structured_handoff_repair import (
    RepairCaptureGateway,
    assert_private_rejection,
)


@pytest.mark.parametrize("phase", ["original", "correction"])
@pytest.mark.parametrize("rejected", [False, True])
async def test_cancelled_review_receipt_accounts_once_without_approval_or_repair(
    phase: str, rejected: bool,
) -> None:
    marker = "PRIVATE_CANCELLED_REVIEW_RECEIPT"

    class CancelledReview(RepairCaptureGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            review_calls = sum(item.logical_model == "review" for item in self.requests)
            if request.logical_model != "review" or (phase == "correction" and review_calls == 0):
                return await super().complete_with_context(request)
            self.requests.append(request)
            count = 17 if phase == "correction" else 8
            usage = TokenUsage(1, count - 1, count)
            receipt: GatewayCompletion | GatewayRejectedOutput
            if rejected:
                receipt = GatewayRejectedOutput(
                    evidence=RejectedOutputEvidence(
                        final_text=marker, usage=usage, usage_status="known",
                        status="completed", reason="invalid_json",
                    ),
                    deployment_id="primary", logical_model="review", provider_id="provider",
                    provider_model="provider/model", cost_usd=Decimal(0),
                )
            else:
                receipt = GatewayCompletion(
                    response=ModelResponse(
                        text='{"verdict":"approve","feedback":"' + marker + '"}', usage=usage,
                    ),
                    deployment_id="primary", logical_model="review", provider_id="provider",
                    provider_model="provider/model", cost_usd=Decimal(0),
                )
            raise GatewayResponseCancelled(receipt=receipt)

    gateway = CancelledReview(
        ('{"summary":"candidate"}', 129, False),
        ("INVALID_FIRST_REVIEW", 8, True),
    )
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(), artifact_repository=repository, crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(asyncio.CancelledError):
        async for event in runtime.run(_context()):
            events.append(event)
    assert [request.logical_model for request in gateway.requests] == (
        ["general", "review", "review"] if phase == "correction" else ["general", "review"]
    )
    assert not any(event.kind in {
        EventKind.REVIEW_COMPLETED, EventKind.STEP_COMPLETED, EventKind.RUNTIME_COMPLETED,
    } for event in events)
    assert_private_rejection(events, marker)
    checkpoint = await runtime.save_checkpoint()
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping)
    assert usage["tokens"] == (154 if phase == "correction" else 137)
    models = checkpoint.state["models"]
    assert isinstance(models, Mapping)
    assert sum(isinstance(state, Mapping) and state["status"] == "received_cancelled"
               for state in models.values()) == 1
    repairs = checkpoint.state["structured_repairs"]
    assert isinstance(repairs, Mapping)
    if phase == "correction":
        assert set(repairs) == {"draft"}
        assert isinstance(repairs["draft"], Mapping)
        assert repairs["draft"]["actor"] == "reviewer"
        assert repairs["draft"]["status"] == "uncertain"
    else:
        assert repairs == {}

    replay_gateway = RepairCaptureGateway()
    replay = CrewDispatchRuntime(
        replay_gateway, _reviewed_step_plan(), artifact_repository=repository, crew_factory=FastFactory(),
    )
    with pytest.raises(RuntimeExecutionError):
        await replay.restore_checkpoint(checkpoint)
        _ = [event async for event in replay.run(_context(checkpoint=checkpoint))]
    assert replay_gateway.requests == []
    assert checkpoint.state["usage"] == usage
