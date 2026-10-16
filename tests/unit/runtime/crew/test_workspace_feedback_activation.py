"""Feedback retention is versioned without changing historical request hashes."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.types import ModelMessage, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.contracts import EventKind, JsonValue, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.crew.adapter import (
    CrewDispatchRuntime,
    CrewRunStream,
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

FINAL = ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2))


def trace_retention(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    values: list[bool] = []
    original = CrewDispatchRuntime._compact_interaction_messages

    def capture(*args: object, **kwargs: object) -> object:
        values.append(kwargs.get("preserve_feedback") is True)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(CrewDispatchRuntime, "_compact_interaction_messages", staticmethod(capture))
    return values


async def test_new_incremental_delivery_enables_and_persists_feedback_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = trace_retention(monkeypatch)
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("src/business.ts", "export const ready = true;", "write"),
        _workspace_delivery_bundle_response(), FINAL,
    ))
    runtime = _workspace_delivery_sequence_runtime(gateway, WorkspaceDeliverySequenceHarness())
    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state.get("workspace_feedback_retention") is True
    assert values and all(values)


async def test_missing_retention_flag_preserves_cached_request_hashes_and_legacy_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint, context, repository, harness, _first = await _workspace_delivery_partial_checkpoint()
    payload = checkpoint.to_payload()
    cast(dict[str, object], payload["state"]).pop("workspace_feedback_retention", None)
    payload["state_sha256"] = ""
    legacy = RuntimeCheckpoint.from_payload(payload)
    before = legacy.to_payload()
    values = trace_retention(monkeypatch)
    gateway = WorkspaceDeliverySequenceGateway((_workspace_delivery_bundle_response(), FINAL))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    await runtime.restore_checkpoint(legacy)
    calls = len(harness.calls)
    events = [event async for event in runtime.run(context.model_copy(update={"checkpoint": legacy}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(harness.calls) == calls + 1 and len(gateway.requests) == 2
    assert values and not any(values)
    assert legacy.to_payload() == before
    saved = await runtime.save_checkpoint()
    assert saved.state.get("workspace_feedback_retention") is False
    for key, row in cast(Mapping[str, Mapping[str, JsonValue]], legacy.state["models"]).items():
        assert cast(Mapping[str, Mapping[str, JsonValue]], saved.state["models"])[key] == row


@pytest.mark.parametrize("flag", ("true", 1, None))
async def test_invalid_retention_flag_rejected_before_any_external_call(flag: object) -> None:
    checkpoint, context, repository, harness, _first = await _workspace_delivery_partial_checkpoint()
    payload = checkpoint.to_payload()
    cast(dict[str, object], payload["state"])["workspace_feedback_retention"] = flag
    payload["state_sha256"] = ""
    forged = RuntimeCheckpoint.from_payload(payload)
    gateway = WorkspaceDeliverySequenceGateway(())
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    calls = len(harness.calls)
    with pytest.raises(RuntimeExecutionError, match="checkpoint"):
        await runtime.restore_checkpoint(forged)
        async for _event in runtime.run(context.model_copy(update={"checkpoint": forged})):
            pass
    assert not gateway.requests and len(harness.calls) == calls


async def _assert_compressed_checkpoint_restore(
    monkeypatch: pytest.MonkeyPatch, *, legacy: bool,
) -> None:
    class LargeResultHarness(WorkspaceDeliverySequenceHarness):
        async def invoke(
            self, tenant_id: UUID, request: HarnessToolCallRequest, *,
            user_id: UUID | None = None, role: Role | None = None,
        ) -> HarnessToolCallResult:
            result = await super().invoke(tenant_id, request, user_id=user_id, role=role)
            if request.tool_name == "workspace.write_text":
                return replace(result, payload={**result.payload, "summary": "written-" + "r" * 6000})
            return result

    windows: list[tuple[tuple[ModelMessage, ...], tuple[ModelMessage, ...], bool, int]] = []
    compact = CrewDispatchRuntime._compact_interaction_messages

    def capture_window(
        messages: Sequence[ModelMessage], *, max_prompt_bytes: int,
        message_limit: int, preserve_feedback: bool = False,
    ) -> tuple[ModelMessage, ...]:
        result = compact(
            messages, max_prompt_bytes=max_prompt_bytes, message_limit=message_limit,
            preserve_feedback=preserve_feedback,
        )
        windows.append((tuple(messages), result, preserve_feedback, max_prompt_bytes))
        return result

    monkeypatch.setattr(
        CrewDispatchRuntime, "_compact_interaction_messages", staticmethod(capture_window),
    )
    context = _workspace_delivery_sequence_context()
    context = context.model_copy(update={
        "routing_decision": {**context.routing_decision, "context_window_tokens": 7168},
    })
    responses = tuple(
        _workspace_delivery_write_response(
            f"src/business-{index}.ts", f"export const ready{index} = true;", f"write-{index}",
        ) for index in range(4)
    )
    harness = LargeResultHarness()
    run = CrewDispatchRuntime.run

    def legacy_run(runtime: CrewDispatchRuntime, task: TaskContext) -> CrewRunStream:
        stream = cast(CrewRunStream, run(runtime, task))
        # Select the historical renderer before any model request/SHA is created.
        stream._state.workspace_feedback_retention = False
        return stream

    with monkeypatch.context() as generation_patch:
        if legacy:
            generation_patch.setattr(CrewDispatchRuntime, "run", legacy_run)
        checkpoint, context, repository, _harness, first = await _workspace_delivery_partial_checkpoint(
            responses=responses, harness=harness, context=context, expected_tools=4,
        )

    assert checkpoint.state["workspace_feedback_retention"] is (not legacy)
    assert len(first.requests) == len(harness.calls) == 4
    pending_messages = windows[-1][1]
    original_request = first.requests[-1]
    raw, rendered, enabled, budget = next(
        window for window in windows if window[1] == original_request.messages
    )
    assert enabled is (not legacy)
    assert sum(len(json.dumps(
        message.content, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode()) for message in raw) > budget
    assert any(
        message.role == "system"
        and str(message.content).startswith("EARLIER_INTERACTION_WINDOW_COMPRESSED:")
        for message in rendered
    )
    assert rendered != compact(
        raw, max_prompt_bytes=budget, preserve_feedback=legacy,
    ), "the cached SHA must distinguish legacy and feedback-preserving compaction"
    if not legacy:
        latest_feedback = next(
            message for message in reversed(raw)
            if message.role == "user"
            and str(message.content).startswith("UNTRUSTED_CAPABILITY_RESULTS_JSON=")
        )
        latest_control = next(
            message for message in reversed(raw)
            if message.role == "system"
            and str(message.content).startswith("CAPABILITY_RESULT_CONTINUATION:")
        )
        assert latest_feedback in rendered and latest_control in rendered
        assert "src/business-2.ts" in str(latest_feedback.content)

    payload = checkpoint.to_payload()
    if legacy:
        # Only the schema marker is removed; every cached SHA was already generated by legacy.
        cast(dict[str, object], payload["state"]).pop("workspace_feedback_retention")
        payload["state_sha256"] = ""
    restored = RuntimeCheckpoint.from_payload(payload)
    assert restored.state["models"] == checkpoint.state["models"]
    before = restored.to_payload()
    calls = tuple(harness.calls)
    files = dict(harness.files)
    replayed: list[tuple[ModelRequest, str]] = []
    request_sha256 = CrewDispatchRuntime._model_request_sha256

    def capture_request(request: ModelRequest) -> str:
        digest = request_sha256(request)
        replayed.append((request, digest))
        return digest

    monkeypatch.setattr(
        CrewDispatchRuntime, "_model_request_sha256", staticmethod(capture_request),
    )
    gateway = WorkspaceDeliverySequenceGateway((_workspace_delivery_bundle_response(), FINAL))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    await runtime.restore_checkpoint(restored)
    events = [
        event async for event in runtime.run(context.model_copy(update={"checkpoint": restored}))
    ]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 2
    assert tuple(harness.calls[:-1]) == calls
    assert harness.calls[-1].tool_name == "workspace.bundle"
    assert harness.files == files
    assert gateway.requests[0].messages == pending_messages
    if not legacy:
        results = [
            message for message in gateway.requests[0].messages
            if message.role == "user"
            and str(message.content).startswith("UNTRUSTED_CAPABILITY_RESULTS_JSON=")
        ]
        assert results
        packet = json.loads(str(results[-1].content).partition("=")[2])
        assert packet[0]["name"] == "workspace.write_text"
        assert packet[0]["result"]["path"] == "src/business-3.ts"
        assert packet[0]["result"]["summary"] == "written-" + "r" * 6000
        assert any(
            message.role == "system"
            and str(message.content).startswith("CAPABILITY_RESULT_CONTINUATION:")
            for message in gateway.requests[0].messages
        )
    for request in first.requests:
        assert any(
            digest == request_sha256(request) and replay.messages == request.messages
            for replay, digest in replayed
        ), "a cached request must be reconstructed with the same SHA and message roles/content"
    assert restored.to_payload() == before
    saved = await runtime.save_checkpoint()
    assert saved.state["workspace_feedback_retention"] is (not legacy)
    assert saved.state["usage"] == {"tokens": 12, "cost_usd": "0"}
    for ledger in ("models", "tools"):
        for key, row in cast(Mapping[str, Mapping[str, JsonValue]], restored.state[ledger]).items():
            assert cast(Mapping[str, Mapping[str, JsonValue]], saved.state[ledger])[key] == row


async def test_legacy_compressed_checkpoint_restores_original_hashes_without_replaying_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _assert_compressed_checkpoint_restore(monkeypatch, legacy=True)


async def test_feedback_compressed_checkpoint_restores_hashes_controls_and_results_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _assert_compressed_checkpoint_restore(monkeypatch, legacy=False)
