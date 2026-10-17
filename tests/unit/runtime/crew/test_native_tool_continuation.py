"""Synthetic private provider continuation; no provider/network execution."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import (
    FINAL,
    TOOLS,
    Observations,
    offline_only,  # noqa: F401
    write,
)


def subject(gateway: Any, harness: Any, repository: Any) -> CrewDispatchRuntime:
    base = _workspace_delivery_sequence_runtime(gateway, harness, tools=TOOLS)
    return CrewDispatchRuntime(
        gateway, base._plan, capability_gateway=base._capabilities,
        harness_tool_gateway=harness, crew_factory=base._factory,
        artifact_repository=base._artifact_repository,
        provider_continuation_repository=repository,
    )


async def test_configured_private_repository_enables_fresh_capture_not_synthetic_provenance() -> None:
    class UnusedRepository:
        async def put(self, snapshot: Any) -> None:
            pytest.fail("harness-derived calls cannot create a provider snapshot")

        async def get(self, *args: Any) -> Any:
            pytest.fail("no native snapshot was stored")

    gateway = WorkspaceDeliverySequenceGateway((write(), _workspace_delivery_bundle_response(), FINAL))
    runtime = subject(gateway, Observations(), UnusedRepository())
    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert all(request.native_tool_continuation for request in gateway.requests)
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["native_tool_continuation"] is True
    models = checkpoint.state["models"]
    assert isinstance(models, Mapping)
    assert all(isinstance(row, Mapping) and "provider_continuation" not in row for row in models.values())


async def test_unconfigured_repository_keeps_legacy_capture_disabled() -> None:
    gateway = WorkspaceDeliverySequenceGateway((write(), _workspace_delivery_bundle_response(), FINAL))
    runtime = _workspace_delivery_sequence_runtime(gateway, Observations(), tools=TOOLS)
    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert all(not getattr(request, "native_tool_continuation", False) for request in gateway.requests)
    assert "native_tool_continuation" not in (await runtime.save_checkpoint()).state


def native(response: Any) -> Any:
    return replace(response, native_assistant=NativeAssistantMessage(
        "primary", response.text,
        tuple(NativeToolCall(call.id, call.name.replace(".", "_"), json.dumps(dict(call.arguments)))
              for call in response.tool_calls),
        reasoning_content="PRIVATE_NATIVE_CONTINUATION",
    ))


async def test_two_turn_native_loop_keeps_wire_ids_and_private_reasoning_out_of_checkpoint() -> None:
    from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository

    gateway = WorkspaceDeliverySequenceGateway((native(write()), native(_workspace_delivery_bundle_response()), FINAL))
    repository = InMemoryProviderContinuationRepository()
    harness = Observations()
    runtime = subject(gateway, harness, repository)
    context = _workspace_delivery_sequence_context()
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    second = gateway.requests[1]
    assistant = [message for message in second.messages if message.native_assistant is not None]
    assert len(assistant) == 1
    assert assistant[0].native_assistant is not None
    assert assistant[0].native_assistant.reasoning_content == "PRIVATE_NATIVE_CONTINUATION"
    assert any(message.tool_call_id == gateway.responses[0].tool_calls[0].id for message in second.messages)
    saved = await runtime.save_checkpoint()
    assert "PRIVATE_NATIVE_CONTINUATION" not in json.dumps(saved.to_payload())
    assert all("PRIVATE_NATIVE_CONTINUATION" not in json.dumps(event.to_payload()) for event in events)
    models = saved.state["models"]
    assert isinstance(models, Mapping)
    assert sum(isinstance(row, Mapping) and "provider_continuation" in row for row in models.values()) == 2
    restored_gateway = WorkspaceDeliverySequenceGateway(())
    restored = subject(restored_gateway, harness, repository)
    restored._artifact_repository = runtime._artifact_repository
    await restored.restore_checkpoint(saved)
    replay = [event async for event in restored.run(context.model_copy(update={"checkpoint": saved}))]
    assert replay[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not restored_gateway.requests
    assert len(harness.calls) == 2


async def test_private_persistence_failure_accounts_received_usage_without_executing_tools() -> None:
    class FailedRepository:
        async def put(self, snapshot: Any) -> None:
            raise RuntimeError("PRIVATE_STORAGE_DRIVER_ARGUMENTS")

        async def get(self, *args: Any) -> Any:
            pytest.fail("failed write must not be treated as durable")

    response = native(write())
    gateway = WorkspaceDeliverySequenceGateway((response,))
    harness = Observations()
    runtime = subject(gateway, harness, FailedRepository())
    events = []
    with pytest.raises(RuntimeExecutionError, match="private provider continuation"):
        async for event in runtime.run(_workspace_delivery_sequence_context()):
            events.append(event)
    checkpoint = await runtime.save_checkpoint()
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping)
    assert usage["tokens"] == response.usage.total_tokens
    assert not harness.calls
    assert len(gateway.requests) == 1
    assert "PRIVATE_STORAGE" not in json.dumps(checkpoint.to_payload())
    assert all("PRIVATE_STORAGE" not in json.dumps(event.to_payload()) for event in events)
    empty_gateway = WorkspaceDeliverySequenceGateway(())
    restored = subject(empty_gateway, harness, FailedRepository())
    restored._artifact_repository = runtime._artifact_repository
    await restored.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match="private provider continuation"):
        _events = [event async for event in restored.run(
            _workspace_delivery_sequence_context().model_copy(update={
                "run_id": checkpoint.run_id, "tenant_id": checkpoint.tenant_id, "checkpoint": checkpoint,
            }),
        )]
    assert not empty_gateway.requests and not harness.calls


async def test_cancel_during_private_store_records_usage_and_never_dispatches_tool() -> None:
    class CancelledRepository:
        async def put(self, snapshot: Any) -> None:
            raise asyncio.CancelledError

        async def get(self, *args: Any) -> Any:
            pytest.fail("cancelled store cannot be resumed")

    response = native(write())
    gateway = WorkspaceDeliverySequenceGateway((response,))
    harness = Observations()
    runtime = subject(gateway, harness, CancelledRepository())
    with pytest.raises(asyncio.CancelledError):
        _events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]
    saved = await runtime.save_checkpoint()
    usage, models = saved.state["usage"], saved.state["models"]
    assert isinstance(usage, Mapping) and isinstance(models, Mapping)
    assert usage["tokens"] == response.usage.total_tokens
    statuses = set()
    for row in models.values():
        assert isinstance(row, Mapping)
        statuses.add(row["status"])
    assert statuses == {"received_cancelled"}
    assert not harness.calls and len(gateway.requests) == 1


async def test_partial_native_restore_rebuilds_history_without_repaying_or_rewriting() -> None:
    from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository

    paused, ready = asyncio.Event(), asyncio.Event()

    class PausingGateway(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: Any) -> Any:
            if self.requests:
                paused.set()
                await asyncio.Event().wait()
            return await super().complete_with_context(request)

    repository = InMemoryProviderContinuationRepository()
    harness = Observations()
    first = PausingGateway((native(write()),))
    runtime = subject(first, harness, repository)
    context = _workspace_delivery_sequence_context()
    checkpoints = []

    async def consume() -> None:
        tool_completed = False
        async for event in runtime.run(context):
            tool_completed |= event.kind is EventKind.TOOL_COMPLETED
            if tool_completed and event.checkpoint is not None:
                models = event.checkpoint.state["models"]
                assert isinstance(models, Mapping)
                if len(models) == 1:
                    checkpoints.append(event.checkpoint)
                    ready.set()

    consumer = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(5):
            await paused.wait()
            await ready.wait()
    finally:
        await runtime.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    checkpoint = checkpoints[-1]
    second = WorkspaceDeliverySequenceGateway((native(_workspace_delivery_bundle_response()), FINAL))
    restored = subject(second, harness, repository)
    restored._artifact_repository = runtime._artifact_repository
    await restored.restore_checkpoint(checkpoint)
    events = [event async for event in restored.run(context.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(first.requests) == 1 and len(second.requests) == 2
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", "workspace.bundle"]
    assert any(message.native_assistant is not None for message in second.requests[0].messages)


async def test_missing_private_reference_fails_before_completed_restore_can_call_model_or_tools() -> None:
    from agent_hub.runtime.contracts import RuntimeCheckpoint
    from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository

    repository = InMemoryProviderContinuationRepository()
    gateway = WorkspaceDeliverySequenceGateway((native(write()), native(_workspace_delivery_bundle_response()), FINAL))
    harness = Observations()
    runtime = subject(gateway, harness, repository)
    context = _workspace_delivery_sequence_context()
    _events = [event async for event in runtime.run(context)]
    payload = (await runtime.save_checkpoint()).to_payload()
    state = payload["state"]
    assert isinstance(state, dict)
    models = state["models"]
    assert isinstance(models, dict)
    for row in models.values():
        assert isinstance(row, dict)
        row.pop("provider_continuation", None)
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    empty_gateway = WorkspaceDeliverySequenceGateway(())
    restored = subject(empty_gateway, harness, repository)
    restored._artifact_repository = runtime._artifact_repository
    await restored.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match="private provider continuation"):
        _events = [event async for event in restored.run(context.model_copy(update={"checkpoint": checkpoint}))]
    assert not empty_gateway.requests and len(harness.calls) == 2


async def test_unsupported_initial_protocol_preserves_existing_legacy_path() -> None:
    from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository

    class LegacyGateway(WorkspaceDeliverySequenceGateway):
        def supports_native_tool_continuation(self, request: Any) -> bool:
            return False

    gateway = LegacyGateway((write(), _workspace_delivery_bundle_response(), FINAL))
    runtime = subject(gateway, Observations(), InMemoryProviderContinuationRepository())
    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert all(not request.native_tool_continuation for request in gateway.requests)
