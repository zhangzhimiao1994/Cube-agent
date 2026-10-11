"""Synthetic offline coverage; no actual user data or delivery acceptance."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import textwrap
from collections.abc import Mapping
from types import CodeType, FunctionType
from typing import Any

import pytest

from agent_hub.models.gateway import GatewayCompletion, GatewayResponseCancelled
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import (
    CrewDispatchRuntime,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
)
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    WorkspaceDeliverySequenceHarness,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
    _workspace_delivery_write_response,
)
from tests.unit.runtime.crew.test_incremental_read_rejection import offline_only  # noqa: F401
from tests.unit.runtime.crew.test_request_checkpoint_fingerprint_recovery import (
    _natural_workspace_run,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def legacy_exact_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in mutation of the described 84ab condition, never product files."""
    if os.environ.get("MOFANG_CHECKPOINT_EXPECT_84AB") != "1":
        return
    method = CrewDispatchRuntime._complete_gateway_messages
    source = textwrap.dedent(inspect.getsource(method))
    condition = "and artifact.source_ids == (str(trigger_model_artifact.id),)"
    current = next(line for line in source.splitlines() if line.strip() == condition)
    original = (
        current.replace(
            condition,
            'and tool_call.name in {"workspace.read", "workspace.list"}',
        )
        + "\n"
        + current
    )
    assert source.count(current) == 1, "expected exactly one cached exact-trigger condition"
    compiled = compile(source.replace(current, original, 1), "<84ab-exact-progress>", "exec")
    code = next(item for item in compiled.co_consts if isinstance(item, CodeType))
    restored = FunctionType(code, vars(adapter), method.__name__, method.__defaults__)
    restored.__kwdefaults__ = method.__kwdefaults__
    monkeypatch.setattr(CrewDispatchRuntime, method.__name__, restored)


def _rows(checkpoint: RuntimeCheckpoint, key: str) -> list[Mapping[str, Any]]:
    ledger = checkpoint.state[key]
    assert isinstance(ledger, Mapping)
    rows: list[Mapping[str, Any]] = []
    for row in ledger.values():
        assert isinstance(row, Mapping)
        rows.append(row)
    return rows


@pytest.mark.parametrize("later_tools_first", [False, True])
async def test_long_successful_ledger_reentry_preserves_extension_and_compressed_history(
    later_tools_first: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: dict[tuple[int, int], ModelRequest] = {}
    replayed: list[tuple[int, int]] = []
    execute = CrewDispatchRuntime._execute_model_request

    async def capture(self: CrewDispatchRuntime, *args: Any, **kwargs: Any) -> Any:
        coordinate = (kwargs["attempt"], kwargs["cursor"].value)
        request = args[3]
        assert isinstance(request, ModelRequest)
        result = await execute(self, *args, **kwargs)
        if coordinate in requests:
            previous = requests[coordinate]
            assert self._model_request_sha256(request) == self._model_request_sha256(previous)
            assert request.messages == previous.messages
            assert request.tools == previous.tools
            replayed.append(coordinate)
        else:
            requests[coordinate] = request
        return result

    monkeypatch.setattr(CrewDispatchRuntime, "_execute_model_request", capture)
    repository = InMemoryArtifactRepository()
    harness = WorkspaceDeliverySequenceHarness()
    context = _workspace_delivery_sequence_context()
    gateway = WorkspaceDeliverySequenceGateway(
        (
            _workspace_delivery_write_response(
                "src/file_00.ts", "export const value = 0;", "write-0"
            ),
            _workspace_delivery_bundle_response(),
            *(
                _workspace_delivery_write_response(
                    f"src/file_{index:02}.ts",
                    f"export const value = {index};",
                    f"write-{index}",
                )
                for index in range(1, 24)
            ),
            _workspace_delivery_bundle_response(),
            ModelResponse(text="Synthetic workspace delivered.", usage=TokenUsage(1, 1, 2)),
        )
    )
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    async with asyncio.timeout(20):
        events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 27
    assert all(gateway.requests[index].tools for index in (8, 16, 24))
    assert any(
        isinstance(message.content, str)
        and message.content.startswith("EARLIER_INTERACTION_WINDOW_COMPRESSED:")
        for request in gateway.requests[20:]
        for message in request.messages
    )
    checkpoint = next(
        event.checkpoint
        for event in reversed(events)
        if event.checkpoint is not None
        and event.checkpoint.state["phase"] == "running"
        and len(_rows(event.checkpoint, "models")) == 27
        and all(row["status"] == "succeeded" for row in _rows(event.checkpoint, "models"))
    )
    assert sorted(row["call_index"] for row in _rows(checkpoint, "models")) == list(range(27))
    assert all(row["attempt"] == 0 for row in _rows(checkpoint, "models"))
    assert len(_rows(checkpoint, "tools")) == 26
    assert all(row["status"] == "succeeded" for row in _rows(checkpoint, "tools"))
    assert checkpoint.state["usage"] == {"tokens": 54, "cost_usd": "0"}
    payload = json.loads(json.dumps(checkpoint.to_payload()))
    payload["state"]["tools"] = dict(
        sorted(
            payload["state"]["tools"].items(),
            key=lambda pair: (pair[1]["round"], pair[1]["tool_index"]),
            reverse=later_tools_first,
        )
    )
    boundary = RuntimeCheckpoint.from_payload(payload)
    before = boundary.to_payload()
    tool_count = len(harness.calls)
    replay_gateway = WorkspaceDeliverySequenceGateway(())
    resumed = _workspace_delivery_sequence_runtime(
        replay_gateway,
        harness,
        artifact_repository=repository,
    )
    await resumed.restore_checkpoint(boundary)
    async with asyncio.timeout(20):
        replay_events = [
            event
            async for event in resumed.run(
                context.model_copy(update={"checkpoint": boundary}),
            )
        ]
    assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert replayed == [(0, index) for index in range(27)]
    assert replay_gateway.requests == []
    assert len(harness.calls) == tool_count
    assert not any(
        event.kind in {EventKind.COST_RECORDED, EventKind.STEP_RETRYING} for event in replay_events
    )
    saved = await resumed.save_checkpoint()
    for key in ("models", "tools", "usage", "step_usage", "rejected_outputs"):
        assert saved.state[key] == boundary.state[key]
    assert boundary.to_payload() == before


async def test_cross_trigger_semantic_duplicate_forces_synthesis_without_another_write() -> None:
    response = _workspace_delivery_write_response("src/same.ts", "export const ready = true;", "w")
    gateway = WorkspaceDeliverySequenceGateway(
        (
            response,
            response,
            _workspace_delivery_bundle_response(),
        )
    )
    harness = WorkspaceDeliverySequenceHarness()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness)
    events: list[RunEvent] = []
    async with asyncio.timeout(10):
        with pytest.raises(
            RuntimeExecutionError, match="identical capability after result synthesis"
        ):
            async for event in runtime.run(_workspace_delivery_sequence_context()):
                events.append(event)
    assert len(gateway.requests) == 3
    assert gateway.requests[1].tools
    assert gateway.requests[2].tools == ()
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text"]
    checkpoint = await runtime.save_checkpoint()
    models = sorted(_rows(checkpoint, "models"), key=lambda row: row["call_index"])
    assert models[0]["artifact_id"] != models[1]["artifact_id"]
    assert models[0]["status"] == models[1]["status"] == "succeeded"
    assert len(_rows(checkpoint, "tools")) == 1
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


async def test_received_cancelled_checkpoint_does_not_resubmit_or_rebill() -> None:
    context, repository, harness, _ = await _natural_workspace_run()

    class CancelledGateway(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            receipt = await super().complete_with_context(request)
            raise GatewayResponseCancelled(receipt=receipt)

    gateway = CancelledGateway(
        (ModelResponse(text="Synthetic cancelled receipt.", usage=TokenUsage(1, 1, 2)),)
    )
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    events: list[RunEvent] = []
    async with asyncio.timeout(10):
        with pytest.raises(asyncio.CancelledError):
            async for event in runtime.run(context):
                events.append(event)
    checkpoint = await runtime.save_checkpoint()
    assert len(gateway.requests) == 1
    assert [row["status"] for row in _rows(checkpoint, "models")] == ["received_cancelled"]
    assert checkpoint.state["usage"] == {"tokens": 2, "cost_usd": "0"}
    before = checkpoint.to_payload()
    tool_count = len(harness.calls)
    replay_gateway = WorkspaceDeliverySequenceGateway(())
    resumed = _workspace_delivery_sequence_runtime(
        replay_gateway,
        harness,
        artifact_repository=repository,
    )
    await resumed.restore_checkpoint(checkpoint)
    replay_events: list[RunEvent] = []
    async with asyncio.timeout(10):
        with pytest.raises(ModelOutcomeUncertain, match="model outcome requires confirmation"):
            async for event in resumed.run(context.model_copy(update={"checkpoint": checkpoint})):
                replay_events.append(event)
    assert replay_gateway.requests == []
    assert len(harness.calls) == tool_count
    assert not any(
        event.kind
        in {EventKind.COST_RECORDED, EventKind.STEP_RETRYING, EventKind.RUNTIME_COMPLETED}
        for event in replay_events
    )
    assert (await resumed.save_checkpoint()).to_payload() == before
    assert checkpoint.to_payload() == before
