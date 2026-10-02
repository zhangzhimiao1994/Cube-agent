"""Network failure checkpoints must preserve paid-call and recovery boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import (
    CrewDispatchRuntime,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
)
from agent_hub.runtime.crew.plan import DispatchPlan
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    RoleAwareGateway,
    _context,
    _one_step_plan_with_two_model_fallbacks,
    _reviewed_step_plan,
)
from tests.unit.runtime.crew.test_structured_handoff_repair import RepairCaptureGateway


class FailedCorrectionGateway(RepairCaptureGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if self.requests:
            self.requests.append(request)
            raise ModelTransportError("model transport failed")
        return await super().complete_with_context(request)


class ProviderFailureGateway(RoleAwareGateway):
    def __init__(self, status_code: int | None) -> None:
        super().__init__()
        self.status_code = status_code

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        raise ModelTransportError("model transport failed", status_code=self.status_code)


def failed_checkpoint(events: list[RunEvent], *, model_count: int) -> RuntimeCheckpoint:
    return next(
        checkpoint
        for event in events
        if (checkpoint := event.checkpoint) is not None
        and checkpoint.state["phase"] == "running"
        and isinstance(models := checkpoint.state["models"], Mapping)
        and len(models) == model_count
        and any(
            isinstance(state, Mapping) and state["status"] == "failed"
            for state in models.values()
        )
        and not any(
            isinstance(state, Mapping) and state["status"] in {"prepared", "running"}
            for state in models.values()
        )
    )


async def failed_candidate_run(
    status_code: int | None,
) -> tuple[DispatchPlan, InMemoryArtifactRepository, list[RunEvent]]:
    plan = _one_step_plan_with_two_model_fallbacks()
    repository = InMemoryArtifactRepository()
    gateway = ProviderFailureGateway(status_code)
    runtime = CrewDispatchRuntime(
        gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)
    assert [request.logical_model for request in gateway.requests] == ["primary", "backup", "final"]
    return plan, repository, events


async def test_failed_correction_checkpoint_does_not_restart_paid_models() -> None:
    plan = _reviewed_step_plan(reviewer_retries=1)
    # Keep original and replayed correction request limits identical.
    plan = plan.model_copy(update={
        "agents": tuple(agent.model_copy(update={"max_output_tokens": 128}) for agent in plan.agents),
    })
    repository = InMemoryArtifactRepository()
    gateway = FailedCorrectionGateway(("invalid worker JSON", 129, True))
    runtime = CrewDispatchRuntime(
        gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="structured correction failed"):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 2
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    checkpoint = failed_checkpoint(events, model_count=2)
    repairs = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["structured_repairs"])
    assert repairs["draft"]["status"] == "uncertain"
    assert checkpoint.state["usage"] == {"tokens": 129, "cost_usd": "0"}
    before = checkpoint.to_payload()

    replay_gateway = RoleAwareGateway()
    replay = CrewDispatchRuntime(
        replay_gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    await replay.restore_checkpoint(checkpoint)
    replay_events: list[RunEvent] = []
    replay_failure: RuntimeExecutionError | None = None
    try:
        async for event in replay.run(_context(checkpoint=checkpoint)):
            replay_events.append(event)
    except RuntimeExecutionError as error:
        replay_failure = error

    assert replay_gateway.requests == []
    assert replay_failure is not None
    assert not any(
        event.kind in {EventKind.STEP_RETRYING, EventKind.STEP_COMPLETED, EventKind.RUNTIME_COMPLETED}
        for event in replay_events
    )
    assert not any(event.kind is EventKind.COST_RECORDED for event in replay_events)
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("status_code", [None, 429], ids=["no_http_status", "http_429"])
async def test_exhausted_network_checkpoint_preserves_failure_without_new_calls(
    status_code: int | None,
) -> None:
    plan, repository, events = await failed_candidate_run(status_code)
    checkpoint = failed_checkpoint(events, model_count=3)
    models = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["models"])
    assert {(state["attempt"], state["status"]) for state in models.values()} == {
        (0, "failed"), (1, "failed"), (2, "failed"),
    }
    original_failure = next(event for event in events if event.kind is EventKind.STEP_FAILED)
    assert original_failure.payload["recovery_status"] == "failed_after_compact_retry"
    before = checkpoint.to_payload()

    replay_gateway = RoleAwareGateway()
    replay = CrewDispatchRuntime(
        replay_gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    await replay.restore_checkpoint(checkpoint)
    replay_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError) as failure:
        async for event in replay.run(_context(checkpoint=checkpoint)):
            replay_events.append(event)

    assert replay_gateway.requests == []
    assert not isinstance(failure.value, ModelOutcomeUncertain)
    replay_failure = next(event for event in replay_events if event.kind is EventKind.STEP_FAILED)
    assert replay_failure.reason == original_failure.reason
    assert replay_failure.payload["error_code"] == original_failure.payload["error_code"]
    assert replay_failure.payload["recovery_status"] == "failed_after_compact_retry"
    assert replay_failure.payload["recovery_attempts"] == 2
    assert not any(event.kind is EventKind.COST_RECORDED for event in replay_events)
    assert checkpoint.to_payload() == before


async def test_v9_network_checkpoint_continues_remaining_final_candidate() -> None:
    plan, repository, events = await failed_candidate_run(429)
    checkpoint = failed_checkpoint(events, model_count=2)
    state = dict(checkpoint.state)
    # V9 checkpoints omit the later persisted timeout fields and use a stride of two.
    for key in (
        "remaining_timeout_seconds", "remaining_absolute_timeout_seconds", "timeout_progress_units",
    ):
        state.pop(key, None)
    legacy = RuntimeCheckpoint(
        id=uuid4(), runtime_type=checkpoint.runtime_type, runtime_version="9",
        run_id=checkpoint.run_id, tenant_id=checkpoint.tenant_id,
        mode=checkpoint.mode, state=state,
    )
    models = cast(Mapping[str, Mapping[str, JsonValue]], legacy.state["models"])
    assert {(item["attempt"], item["status"]) for item in models.values()} == {
        (0, "failed"), (1, "failed"),
    }
    before = legacy.to_payload()
    replay_gateway = RoleAwareGateway()
    replay = CrewDispatchRuntime(
        replay_gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    await replay.restore_checkpoint(legacy)
    replay_events = [event async for event in replay.run(_context(checkpoint=legacy))]

    assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [request.logical_model for request in replay_gateway.requests] == ["final"]
    completed = await replay.save_checkpoint()
    completed_models = cast(Mapping[str, Mapping[str, JsonValue]], completed.state["models"])
    assert {(item["attempt"], item["status"]) for item in completed_models.values()} == {
        (0, "failed"), (1, "failed"), (2, "succeeded"),
    }
    assert completed.state["usage"] == {"tokens": 2, "cost_usd": "0"}
    assert legacy.to_payload() == before
