"""Native post-tool recovery uses fake gateways and in-memory private storage only."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import pytest

from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    ScopeIncompletePhase,
    ScopeIncompleteReason,
    _GatewayFailureHistory,
)
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    ModelCapability,
    ModelRequest,
    ModelResponse,
    RejectedOutputEvidence,
    TokenUsage,
)
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import RuntimeExecutionError
from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository
from tests.unit.models.test_gateway import deployment
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import (
    Observations,
    offline_only,  # noqa: F401
    write,
)
from tests.unit.runtime.crew.test_native_tool_continuation import native, subject

HANDOFF = ModelResponse(text="The source was written; its bundle is still required.", usage=TokenUsage(1, 1, 2))
FINAL = ModelResponse(text="Workspace delivered with its actual bundle.", usage=TokenUsage(1, 1, 2))


class NativeGateway:
    def __init__(self, *outcomes: Any, status: Literal["completed", "incomplete", "refused", "unknown"] = "incomplete", trusted: bool = True,
                 entries: int = 1, usage_known: bool = True) -> None:
        self.outcomes = outcomes
        self.requests: list[ModelRequest] = []
        self.status, self.trusted, self.entries, self.usage_known = status, trusted, entries, usage_known
        self._registry = ModelRegistry((deployment(
            "primary", logical_model="general", capabilities=frozenset(ModelCapability),
            max_output_tokens=100_000, context_window_tokens=1_000_000,
        ),))

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        assert len(self.requests) <= len(self.outcomes), "unexpected extra model request"
        outcome = self.outcomes[len(self.requests) - 1]
        if outcome == "incomplete":
            error = GatewayRejectedOutput(
                evidence=RejectedOutputEvidence(
                    final_text=None, status=self.status, reason="incomplete",
                    usage=TokenUsage(1, 16, 17) if self.usage_known else None,
                    usage_status="known" if self.usage_known else "missing",
                ),
                deployment_id="primary", logical_model=request.logical_model,
                provider_id="deepseek", provider_model="deepseek/chat", cost_usd=Decimal(0),
            )
            if self.trusted:
                history = _GatewayFailureHistory(entered_count=self.entries)
                history.mark_incomplete(ScopeIncompletePhase.TRANSPORT, ScopeIncompleteReason.REJECTED_OUTPUT)
                history.attach_diagnostic(error)
            raise error
        if outcome == "cancel":
            raise asyncio.CancelledError
        return GatewayCompletion(
            response=outcome, deployment_id="primary", logical_model=request.logical_model,
            provider_id="deepseek", provider_model="deepseek/chat", cost_usd=Decimal(0),
        )


def runtime_for(gateway: NativeGateway, harness: Any = None, private: Any = None,
                artifacts: Any = None) -> Any:
    runtime = subject(gateway, harness if harness is not None else Observations(),
                      private if private is not None else InMemoryProviderContinuationRepository())
    runtime._plan = runtime._plan.model_copy(update={
        "total_token_budget": 100_000, "total_cost_usd": Decimal("0.10"),
        "steps": (runtime._plan.steps[0].model_copy(update={
            "token_budget": 100_000, "cost_budget_usd": Decimal("0.10"),
        }),),
    })
    if artifacts is not None:
        runtime._artifact_repository = artifacts
    return runtime


def context() -> Any:
    return _workspace_delivery_sequence_context().model_copy(update={"token_budget": 100_000})


async def test_native_handoff_keeps_delivery_guard_until_actual_bundle() -> None:
    gateway = NativeGateway(native(write()), "incomplete", native(HANDOFF),
                            native(_workspace_delivery_bundle_response()), native(FINAL))
    harness = Observations()
    runtime = runtime_for(gateway, harness)
    events = [event async for event in runtime.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", "workspace.bundle"]
    assert len(gateway.requests) == 5
    source, recovery, continuation = gateway.requests[1:4]
    assert source.response_schema is recovery.response_schema is None
    assert source.native_tool_continuation and recovery.native_tool_continuation
    assert recovery.tools == () and not recovery.allow_fallback
    assert recovery.logical_model == source.logical_model
    assert recovery.timeout_seconds <= source.timeout_seconds
    assert recovery.messages[:-1] == source.messages
    assert "JSON" not in recovery.messages[-1].content
    assert "POST_TOOL_FINALIZATION" in recovery.messages[-1].content
    assert any(message.tool_call_id == "write-business" for message in recovery.messages)
    assert continuation.tools
    assert any("WORKSPACE_DELIVERY_CONTINUATION" in message.content for message in continuation.messages)
    checkpoint = await runtime.save_checkpoint()
    repair = checkpoint.state["structured_repairs"]["implementer_step"]
    assert (repair["version"], repair["mode"], repair["status"]) == (2, "post_tool_finalization", "succeeded")
    assert checkpoint.state["usage"]["tokens"] == 25


async def test_native_handoff_without_bundle_never_completes() -> None:
    gateway = NativeGateway(native(write()), "incomplete", native(HANDOFF), native(HANDOFF))
    harness = Observations()
    runtime = runtime_for(gateway, harness)
    events = []
    with pytest.raises(RuntimeExecutionError, match="bundle is missing|workspace delivery stalled"):
        async for event in runtime.run(context()):
            events.append(event)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text"]
    assert len(gateway.requests) == 4


async def completed_fixture() -> tuple[Any, Any, Any, Any]:
    gateway = NativeGateway(native(write()), "incomplete", native(HANDOFF),
                            native(_workspace_delivery_bundle_response()), native(FINAL))
    private = InMemoryProviderContinuationRepository()
    artifacts = InMemoryArtifactRepository()
    runtime = runtime_for(gateway, Observations(), private, artifacts)
    ctx = context()
    events = [event async for event in runtime.run(ctx)]
    return runtime, events, ctx, private


def phase_checkpoint(events: Any, phase: str) -> Any:
    if phase == "completed":
        return next(event.checkpoint for event in reversed(events) if event.checkpoint is not None)
    count, status = {"source": (2, "rejected"), "prepared": (3, "prepared"),
                     "running": (3, "running"), "succeeded": (3, "succeeded"),
                     "bundled": (4, "succeeded")}[phase]
    return next(event.checkpoint for event in events if event.checkpoint is not None
                and event.checkpoint.state["phase"] == "running"
                and len(event.checkpoint.state["models"]) == count
                and max(event.checkpoint.state["models"].values(),
                        key=lambda row: row["call_index"])["status"] == status
                and (phase != "bundled" or (len(event.checkpoint.state["tools"]) == 2
                     and all(row["status"] == "succeeded"
                             for row in event.checkpoint.state["tools"].values()))))


@pytest.mark.parametrize("phase", ["source", "prepared", "running", "succeeded", "bundled", "completed"])
async def test_native_restore_consumes_receipts_without_repeating_write_or_handoff(phase: str) -> None:
    original, events, ctx, private = await completed_fixture()
    checkpoint = phase_checkpoint(events, phase)
    before = checkpoint.to_payload()
    for _ in range(2):
        # Model a crash at this exact persisted prefix, not a rollback of the
        # already completed run into a private repository containing future slots.
        prefix_private = InMemoryProviderContinuationRepository()
        for row in checkpoint.state["models"].values():
            reference = row.get("provider_continuation")
            if reference is not None:
                await prefix_private.put(await private.get(
                    checkpoint.tenant_id, checkpoint.run_id, UUID(reference["id"]), reference["sha256"],
                ))
        outcomes = {
            "source": (native(HANDOFF), native(_workspace_delivery_bundle_response()), native(FINAL)),
            "prepared": (native(HANDOFF), native(_workspace_delivery_bundle_response()), native(FINAL)),
            "running": (),
            "succeeded": (native(_workspace_delivery_bundle_response()), native(FINAL)),
            "bundled": (native(FINAL),), "completed": (),
        }[phase]
        gateway = NativeGateway(*outcomes)
        harness = Observations()
        restored = runtime_for(gateway, harness, prefix_private, original._artifact_repository)
        await restored.restore_checkpoint(checkpoint)
        if phase == "running":
            with pytest.raises(RuntimeExecutionError, match="outcome requires confirmation"):
                _ = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": checkpoint}))]
        else:
            replay = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": checkpoint}))]
            assert replay[-1].kind is EventKind.RUNTIME_COMPLETED
            saved = await restored.save_checkpoint()
            assert saved.state["usage"]["tokens"] == 25
            for key, row in checkpoint.state["models"].items():
                if row["status"] in {"succeeded", "rejected"}:
                    assert saved.state["models"][key] == row
        assert len(gateway.requests) == len(outcomes)
        assert all(call.tool_name != "workspace.write_text" for call in harness.calls)
        if phase in {"running", "bundled", "completed"}:
            assert harness.calls == []
        assert checkpoint.to_payload() == before


@pytest.mark.parametrize("change", [
    "missing_native", "legacy_native", "source_request", "receipt", "correction_request", "history",
    "source_ids", "source_usage", "source_status", "mode", "version", "tool_trigger", "tool_sha",
    "tool_status", "private_reference", "closed_future_trigger",
])
async def test_native_restore_tamper_stops_before_model_or_tool_io(change: str) -> None:
    original, events, ctx, private = await completed_fixture()
    checkpoint = phase_checkpoint(events, "completed" if change == "closed_future_trigger" else "prepared")
    payload = checkpoint.to_payload()
    state = payload["state"]
    repair = state["structured_repairs"]["implementer_step"]
    source_key = repair["source_key"]
    tool = next(row for row in state["tools"].values() if row["name"] == "workspace.write_text")
    if change == "missing_native":
        state.pop("native_tool_continuation")
    elif change == "legacy_native":
        payload["runtime_version"] = "10"
    elif change in {"source_request", "receipt", "correction_request", "history"}:
        repair[{
            "source_request": "source_request_sha256", "receipt": "source_receipt_sha256",
            "correction_request": "correction_request_sha256", "history": "tool_history_sha256",
        }[change]] = "0" * 64
    elif change == "source_ids":
        state["rejected_outputs"][source_key]["source_ids"] = []
    elif change == "source_usage":
        state["rejected_outputs"][source_key]["usage"]["total_tokens"] += 1
    elif change == "source_status":
        state["rejected_outputs"][source_key]["output_status"] = "unknown"
    elif change in {"mode", "version"}:
        repair[change] = "format_repair" if change == "mode" else 1
    elif change in {"tool_trigger", "tool_sha", "tool_status"}:
        tool[{"tool_trigger": "trigger_model_artifact_id", "tool_sha": "sha256",
              "tool_status": "status"}[change]] = "running" if change == "tool_status" else "0" * 64
    elif change == "private_reference":
        next(row for row in state["models"].values() if row["status"] == "succeeded").pop("provider_continuation")
    elif change == "closed_future_trigger":
        later = max(state["models"].values(), key=lambda row: row["call_index"])
        tool["trigger_model_artifact_id"] = later["artifact_id"]
    payload["state_sha256"] = ""
    altered = RuntimeCheckpoint.from_payload(payload)
    gateway = NativeGateway()
    harness = Observations()
    restored = runtime_for(gateway, harness, private, original._artifact_repository)
    with pytest.raises(RuntimeExecutionError):
        await restored.restore_checkpoint(altered)
        _ = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": altered}))]
    assert gateway.requests == [] and harness.calls == []


@pytest.mark.parametrize("kwargs", [
    {"status": "unknown"}, {"trusted": False}, {"entries": 2}, {"usage_known": False},
])
async def test_native_unknown_or_untrusted_incomplete_cannot_buy_handoff(kwargs: Any) -> None:
    gateway = NativeGateway(native(write()), "incomplete", **kwargs)
    harness = Observations()
    runtime = runtime_for(gateway, harness)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(context())]
    assert len(gateway.requests) == 2
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text"]


@pytest.mark.parametrize("budget", ["tokens", "cost", "deadline"])
async def test_native_recovery_keeps_original_budget_and_deadline(budget: str, monkeypatch: Any) -> None:
    gateway = NativeGateway(native(write()), "incomplete")
    runtime = runtime_for(gateway)
    if budget == "tokens":
        runtime._plan = runtime._plan.model_copy(update={
            "total_token_budget": 19,
            "steps": (runtime._plan.steps[0].model_copy(update={"token_budget": 19}),),
        })
    elif budget == "cost":
        runtime._plan = runtime._plan.model_copy(update={
            "total_cost_usd": Decimal(0),
            "steps": (runtime._plan.steps[0].model_copy(update={"cost_budget_usd": Decimal(0)}),),
        })
    else:
        def expired(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeExecutionError("post-tool finalization deadline exhausted")
        monkeypatch.setattr(runtime, "_post_tool_finalization_remaining_timeout", expired)
    with pytest.raises(RuntimeExecutionError, match="budget|accounting|deadline"):
        _ = [event async for event in runtime.run(context())]
    assert len(gateway.requests) == 2


async def test_native_cancelled_handoff_never_replays_running_slot() -> None:
    gateway = NativeGateway(native(write()), "incomplete", "cancel")
    harness = Observations()
    private = InMemoryProviderContinuationRepository()
    runtime = runtime_for(gateway, harness, private)
    ctx = context()
    with pytest.raises(asyncio.CancelledError):
        _ = [event async for event in runtime.run(ctx)]
    checkpoint = await runtime.save_checkpoint()
    assert not any(row["name"] == "workspace.bundle" for row in checkpoint.state["tools"].values())
    restored_gateway = NativeGateway()
    restored = runtime_for(restored_gateway, harness, private, runtime._artifact_repository)
    await restored.restore_checkpoint(checkpoint)
    try:
        replay = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": checkpoint}))]
    except RuntimeExecutionError as error:
        assert "outcome requires confirmation" in str(error)
    else:
        assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in replay)
    assert len(gateway.requests) == 3 and restored_gateway.requests == []
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text"]


@pytest.mark.parametrize("grow", [False, True])
async def test_native_growth_closed_history_excludes_only_later_delivery_suffix(grow: bool) -> None:
    class SaturatedNativeGateway(NativeGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                if grow:
                    assert error.evidence is not None
                    error.evidence = replace(error.evidence, usage=TokenUsage(
                        1, request.max_output_tokens, 1 + request.max_output_tokens,
                    ))
                raise

    gateway = SaturatedNativeGateway(
        native(write()), "incomplete", *(("incomplete",) if grow else ()),
        native(HANDOFF), native(_workspace_delivery_bundle_response()), native(FINAL),
    )
    private = InMemoryProviderContinuationRepository()
    harness = Observations()
    runtime = runtime_for(gateway, harness, private)
    ctx = context()
    ctx = ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "post_tool_finalization_growth_version": 1,
    }})
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", "workspace.bundle"]
    assert len(gateway.requests) == (6 if grow else 5)
    for request in gateway.requests[2:4 if grow else 3]:
        assert request.tools == () and request.response_schema is None and not request.allow_fallback
    if grow:
        assert gateway.requests[3].max_output_tokens == 2 * gateway.requests[2].max_output_tokens
    checkpoint = await runtime.save_checkpoint()
    restored_gateway = NativeGateway()
    restored = runtime_for(restored_gateway, Observations(), private, runtime._artifact_repository)
    await restored.restore_checkpoint(checkpoint)
    replay = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": checkpoint}))]
    assert replay[-1].kind is EventKind.RUNTIME_COMPLETED
    assert restored_gateway.requests == []
    assert (await restored.save_checkpoint()).state["usage"] == checkpoint.state["usage"]
