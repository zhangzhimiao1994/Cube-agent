"""Offline request reconstruction must preserve the causal checkpoint prefix."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest

from agent_hub.models.types import ModelMessage, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import (
    CrewDispatchRuntime,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
)
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    WorkspaceDeliverySequenceHarness,
    _workspace_delivery_bundle_response,
    _workspace_delivery_partial_checkpoint,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
    _workspace_delivery_write_response,
)
from tests.unit.runtime.crew.test_incremental_read_rejection import offline_only  # noqa: F401


def _model_states(checkpoint: RuntimeCheckpoint) -> Mapping[str, JsonValue]:
    states = checkpoint.state["models"]
    if not isinstance(states, Mapping):
        raise TypeError("model ledger must be a mapping")
    return states


@pytest.mark.parametrize("later_bundle_first", [False, True])
@pytest.mark.parametrize("compact_retry", [False, True])
async def test_step_reentry_rebuilds_cached_requests_without_rebilling(
    later_bundle_first: bool,
    compact_retry: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: dict[tuple[int, int], ModelRequest] = {}
    differences: list[object] = []
    execute = CrewDispatchRuntime._execute_model_request

    async def capture(self: CrewDispatchRuntime, *args: Any, **kwargs: Any) -> Any:
        coordinate = (kwargs["attempt"], kwargs["cursor"].value)
        request = args[3]
        previous = requests.setdefault(coordinate, request)
        if self._model_request_sha256(previous) != self._model_request_sha256(request):
            differences.append((coordinate, previous, request))
        return await execute(self, *args, **kwargs)

    monkeypatch.setattr(CrewDispatchRuntime, "_execute_model_request", capture)
    repository = InMemoryArtifactRepository()
    harness = WorkspaceDeliverySequenceHarness()
    context = _workspace_delivery_sequence_context()
    gateway = WorkspaceDeliverySequenceGateway(
        (
            _workspace_delivery_write_response(
                "src/business.ts", "export const ready = true;", "write"
            ),
            _workspace_delivery_bundle_response(),
            ModelResponse(text="", usage=TokenUsage(1, 1, 2))
            if compact_retry
            else _workspace_delivery_write_response(
                "src/business.ts", "export const ready = false;", "edit"
            ),
            _workspace_delivery_bundle_response(),
            ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
        )
    )
    original = _workspace_delivery_sequence_runtime(
        gateway, harness, artifact_repository=repository
    )
    async with asyncio.timeout(10):
        events = [event async for event in original.run(context)]
    checkpoint = next(
        event.checkpoint
        for event in reversed(events)
        if event.checkpoint is not None
        and event.checkpoint.state["phase"] == "running"
        and len(_model_states(event.checkpoint)) == 5
        and all(
            isinstance(row, Mapping) and row["status"] in {"succeeded", "failed"}
            for row in _model_states(event.checkpoint).values()
        )
    )
    # A JSON object has no causal iteration order. Exercise both legal tool orders.
    payload = json.loads(json.dumps(checkpoint.to_payload()))
    payload["state"]["tools"] = dict(
        sorted(
            payload["state"]["tools"].items(),
            key=lambda pair: (pair[1]["attempt"], pair[1]["round"]),
            reverse=later_bundle_first,
        )
    )
    boundary = RuntimeCheckpoint.from_payload(payload)
    before = boundary.to_payload()
    replay_gateway = WorkspaceDeliverySequenceGateway(())
    resumed = _workspace_delivery_sequence_runtime(
        replay_gateway,
        harness,
        artifact_repository=repository,
    )
    await resumed.restore_checkpoint(boundary)
    async with asyncio.timeout(10):
        try:
            replay_events = [
                event
                async for event in resumed.run(
                    context.model_copy(update={"checkpoint": boundary}),
                )
            ]
        except RuntimeExecutionError as error:
            pytest.fail(f"{error}: {differences!r}")

    assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert replay_gateway.requests == []
    assert [call.tool_name for call in harness.calls] == (
        ["workspace.write_text", "workspace.bundle", "workspace.bundle"]
        if compact_retry
        else [
            "workspace.write_text",
            "workspace.bundle",
            "workspace.write_text",
            "workspace.bundle",
        ]
    )
    assert differences == []
    assert not any(event.kind is EventKind.COST_RECORDED for event in replay_events)
    saved = await resumed.save_checkpoint()
    assert saved.state["models"] == boundary.state["models"]
    assert saved.state["tools"] == boundary.state["tools"]
    assert saved.state["usage"] == boundary.state["usage"]
    assert boundary.to_payload() == before


async def test_changed_framework_request_is_rejected_before_any_gateway_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint, context, repository, harness, _ = await _workspace_delivery_partial_checkpoint()
    before = checkpoint.to_payload()
    tool_count = len(harness.calls)
    guidance = CrewDispatchRuntime._guidance_messages

    def changed(
        cls: type[CrewDispatchRuntime], *args: Any, **kwargs: Any
    ) -> tuple[ModelMessage, ...]:
        return (
            *guidance(*args, **kwargs),
            ModelMessage(role="user", content="Changed synthetic task."),
        )

    monkeypatch.setattr(CrewDispatchRuntime, "_guidance_messages", classmethod(changed))
    gateway = WorkspaceDeliverySequenceGateway(())
    resumed = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    await resumed.restore_checkpoint(checkpoint)
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="^model request changed after checkpoint$"):
        async for event in resumed.run(context.model_copy(update={"checkpoint": checkpoint})):
            events.append(event)

    assert gateway.requests == []
    assert len(harness.calls) == tool_count
    assert not any(
        event.kind in {EventKind.STEP_RETRYING, EventKind.COST_RECORDED} for event in events
    )
    assert checkpoint.to_payload() == before


async def _natural_workspace_run() -> tuple[
    TaskContext,
    InMemoryArtifactRepository,
    WorkspaceDeliverySequenceHarness,
    list[RunEvent],
]:
    context = _workspace_delivery_sequence_context()
    repository = InMemoryArtifactRepository()
    harness = WorkspaceDeliverySequenceHarness()
    gateway = WorkspaceDeliverySequenceGateway(
        (
            _workspace_delivery_write_response(
                "src/business.ts", "export const ready = true;", "write"
            ),
            _workspace_delivery_bundle_response(),
            ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
        )
    )
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    async with asyncio.timeout(10):
        events = [event async for event in runtime.run(context)]
    return context, repository, harness, events


@pytest.mark.parametrize("status", ["prepared", "running"])
async def test_natural_model_boundary_only_dispatches_an_unsubmitted_request(status: str) -> None:
    context, repository, harness, events = await _natural_workspace_run()
    checkpoint = next(
        event.checkpoint
        for event in events
        if event.checkpoint is not None
        and len(_model_states(event.checkpoint)) == 3
        and any(
            isinstance(row, Mapping) and row["call_index"] == 2 and row["status"] == status
            for row in _model_states(event.checkpoint).values()
        )
    )
    before = checkpoint.to_payload()
    tool_count = len(harness.calls)
    gateway = WorkspaceDeliverySequenceGateway(
        (ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),)
    )
    resumed = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    await resumed.restore_checkpoint(checkpoint)
    replay_events: list[RunEvent] = []
    if status == "running":
        with pytest.raises(ModelOutcomeUncertain, match="model outcome requires confirmation"):
            async for event in resumed.run(context.model_copy(update={"checkpoint": checkpoint})):
                replay_events.append(event)
        assert gateway.requests == []
        assert (await resumed.save_checkpoint()).to_payload() == before
    else:
        replay_events = [
            event
            async for event in resumed.run(
                context.model_copy(update={"checkpoint": checkpoint}),
            )
        ]
        assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(gateway.requests) == 1
        saved = await resumed.save_checkpoint()
        assert saved.state["tools"] == checkpoint.state["tools"]
        assert saved.state["usage"] == {"tokens": 6, "cost_usd": "0"}
    assert len(harness.calls) == tool_count
    assert checkpoint.to_payload() == before
