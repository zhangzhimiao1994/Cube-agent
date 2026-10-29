"""Received invalid tool batches are never capabilities or replay authority."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    ModelGateway,
    ScopeIncompletePhase,
    ScopeIncompleteReason,
    _GatewayFailureHistory,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.litellm_client import LiteLLMClient, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    ModelCapability,
    ModelRequest,
    RejectedOutputEvidence,
    TokenUsage,
    ToolCall,
)
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import RuntimeExecutionError
from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease
from tests.unit.runtime.crew.test_adapter_failure_reason import RecordingHarnessToolGateway
from tests.unit.runtime.crew.test_known_incomplete_recovery import collect
from tests.unit.runtime.crew.test_structured_handoff_repair import assert_private_rejection
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_recovery import (
    ToolIncompleteGateway,
    draft_models,
    subject,
    tool_plan,
)

PRIVATE = "PRIVATE_MALFORMED_TOOL_ARGUMENTS_NEVER_EXECUTE"


class InvalidToolGateway(ToolIncompleteGateway):
    change = ""

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        index = len(self.requests)
        if self.outcomes[index] != "invalid_tool":
            return await super().complete_with_context(request)
        self.requests.append(request)
        evidence = RejectedOutputEvidence(
            final_text=PRIVATE, usage=TokenUsage(1, 16, 17),
            usage_status="known", status="completed", reason="invalid_tool",
        )
        if self.change in {"missing", "invalid"}:
            evidence = replace(evidence, usage_status=self.change, usage=None)  # type: ignore[arg-type]
        elif self.change == "unknown":
            evidence = replace(evidence, final_text=None, status="unknown")
        elif self.change == "no_text":
            evidence = replace(evidence, final_text=None)
        error = GatewayRejectedOutput(
            evidence=evidence,
            deployment_id="selected", logical_model=request.logical_model,
            provider_id="provider", provider_model="provider/model", cost_usd=Decimal("0.01"),
        )
        if self.change == "fallback":
            error.fallback_used = True
        elif self.change == "model":
            error.logical_model = "different"
        history = _GatewayFailureHistory(entered_count=2 if self.change == "multi_transport" else 1)
        history.mark_incomplete(ScopeIncompletePhase.TRANSPORT, ScopeIncompleteReason.REJECTED_OUTPUT)
        if self.change != "untrusted":
            history.attach_diagnostic(error)
        raise error


async def test_invalid_batch_after_tool_has_one_no_tool_same_role_correction() -> None:
    gateway = InvalidToolGateway("tool", "invalid_tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    repository = InMemoryArtifactRepository()
    runtime = subject(gateway, tool_plan(), repository, harness)
    events = await collect(runtime)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(harness.calls) == 1
    assert len(gateway.requests) == 4
    original, rejected, correction, _ = gateway.requests
    assert correction.logical_model == rejected.logical_model == original.logical_model
    assert correction.response_schema == rejected.response_schema
    assert correction.messages[:-1] == rejected.messages
    assert correction.tools == () and correction.required_tool is None
    assert not correction.allow_fallback
    assert correction.max_output_tokens <= rejected.max_output_tokens
    assert PRIVATE not in str(correction.messages)
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    assert_private_rejection(events, PRIVATE)
    checkpoint = await runtime.save_checkpoint()
    assert [(row["attempt"], row["call_index"], row["status"])
            for row in draft_models(checkpoint)] == [
        (0, 0, "succeeded"), (0, 1, "rejected"), (0, 2, "succeeded"),
    ]
    repair = mapping(mapping(checkpoint.state["structured_repairs"])["draft"])
    assert repair["version"] == 3 and repair["mode"] == "invalid_tool_correction"
    assert repair["status"] == "succeeded"
    assert mapping(checkpoint.state["usage"])["tokens"] == 62
    replay_gateway = InvalidToolGateway()
    replay_harness = RecordingHarnessToolGateway()
    replay = subject(replay_gateway, tool_plan(), repository, replay_harness)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint)
    assert replay_gateway.requests == [] and replay_harness.calls == []


async def test_invalid_tool_does_not_restart_the_step_after_initial_incomplete_recovery() -> None:
    gateway = InvalidToolGateway("incomplete", "tool", "invalid_tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    events = await collect(runtime)
    assert len(gateway.requests) == 5 and len(harness.calls) == 1
    assert sum(event.kind is EventKind.STEP_RETRYING for event in events) == 1
    checkpoint = await runtime.save_checkpoint()
    assert [row["attempt"] for row in draft_models(checkpoint)] == [0, 1, 1, 1]
    assert gateway.requests[3].messages[:-1] == gateway.requests[2].messages


@pytest.mark.parametrize("phase", ["source", "prepared", "running", "succeeded"])
async def test_restore_only_resumes_the_reserved_correction_slot(phase: str) -> None:
    repository = InMemoryArtifactRepository()
    runtime = subject(InvalidToolGateway("tool", "invalid_tool", "success", "final"),
                      tool_plan(), repository, RecordingHarnessToolGateway())
    events = await collect(runtime)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and event.checkpoint.state["phase"] == "running"
                      and len(draft_models(event.checkpoint)) == (2 if phase == "source" else 3)
                      and draft_models(event.checkpoint)[-1]["status"] == (
                          "rejected" if phase == "source" else phase))
    before = checkpoint.to_payload()
    gateway = InvalidToolGateway(*(("final",) if phase == "succeeded" else ("success", "final")))
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, tool_plan(), repository, harness)
    await restored.restore_checkpoint(checkpoint)
    await collect(restored, checkpoint=checkpoint, fail=(
        "structured output invalid" if phase == "source" else
        "outcome requires confirmation" if phase == "running" else None))
    assert harness.calls == []
    if phase in {"source", "running"}:
        assert gateway.requests == []
        assert (await restored.save_checkpoint()).to_payload() == before
    else:
        assert len(gateway.requests) == (1 if phase == "succeeded" else 2)
        saved = await restored.save_checkpoint()
        assert saved.state["tools"] == checkpoint.state["tools"]
        assert mapping(saved.state["usage"])["tokens"] == 62
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("outcome", ["invalid_tool", "invalid", "incomplete", "transport", "tool"])
async def test_correction_failure_never_invokes_or_retries_tools(outcome: str) -> None:
    gateway = InvalidToolGateway("tool", "invalid_tool", outcome)
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    events = await collect(runtime, fail="structured|provider unavailable")
    assert len(gateway.requests) == 3 and len(harness.calls) == 1
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    assert mapping(mapping((await runtime.save_checkpoint()).state["structured_repairs"])["draft"])[
        "status"] in {"rejected", "uncertain"}


@pytest.mark.parametrize("change", ["untrusted", "multi_transport", "missing", "invalid", "unknown",
                                  "fallback", "model"])
async def test_only_known_single_trusted_transport_rejection_is_correctable(change: str) -> None:
    gateway = InvalidToolGateway("tool", "invalid_tool")
    gateway.change = change
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="structured output invalid|dispatch usage unaccounted")
    assert len(gateway.requests) == 2 and len(harness.calls) == 1
    assert (await runtime.save_checkpoint()).state["structured_repairs"] == {}


async def test_completed_invalid_tool_without_text_can_only_generate_fresh_handoff() -> None:
    gateway = InvalidToolGateway("tool", "invalid_tool", "success", "final")
    gateway.change = "no_text"
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await collect(runtime)
    assert len(gateway.requests) == 4 and gateway.requests[2].tools == ()


async def test_local_forbidden_tool_batch_is_not_gateway_correction_authority() -> None:
    class ForbiddenGateway(InvalidToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            if len(self.requests) == 2:
                completion = replace(completion, response=replace(completion.response, tool_calls=(
                    ToolCall(id="never-execute", name="shell.execute", arguments={}),
                )))
            return completion

    gateway = ForbiddenGateway("tool", "success")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="step requested a forbidden capability")
    checkpoint = await runtime.save_checkpoint()
    assert len(gateway.requests) == 2 and len(harness.calls) == 1
    assert checkpoint.state["structured_repairs"] == {}
    assert draft_models(checkpoint)[-1]["failure_reason"] == "step requested a forbidden capability"


@pytest.mark.parametrize("budget", ["tokens", "cost"])
async def test_source_receipt_exhaustion_cannot_purchase_a_correction(budget: str) -> None:
    dispatch = tool_plan()
    dispatch = dispatch.model_copy(update={"steps": (
        dispatch.steps[0].model_copy(update={
            "token_budget": 34 if budget == "tokens" else 30_000,
            "cost_budget_usd": Decimal("0.02") if budget == "cost" else Decimal(1),
        }), *dispatch.steps[1:],
    )})
    gateway = InvalidToolGateway("tool", "invalid_tool")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    await collect(runtime, tokens=dispatch.total_token_budget, fail="budget exhausted")
    assert len(gateway.requests) == 2 and len(harness.calls) == 1


@pytest.mark.parametrize("change", ["receipt", "request", "correction", "mode", "version", "link"])
async def test_tampered_correction_link_never_submits_or_executes(change: str) -> None:
    repository = InMemoryArtifactRepository()
    runtime = subject(InvalidToolGateway("tool", "invalid_tool", "success", "final"),
                      tool_plan(), repository, RecordingHarnessToolGateway())
    events = await collect(runtime)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and len(draft_models(event.checkpoint)) == 3
                      and draft_models(event.checkpoint)[-1]["status"] == "prepared")
    payload = checkpoint.to_payload()
    state = mapping(payload["state"])
    repair = mapping(state["structured_repairs"])["draft"]
    field = {"receipt": "source_receipt_sha256", "request": "source_request_sha256",
             "correction": "correction_request_sha256", "mode": "mode", "version": "version",
             "link": "source_key"}[change]
    repair[field] = 1 if change == "version" else "a" * 64
    payload["state_sha256"] = ""
    boundary = RuntimeCheckpoint.from_payload(payload)
    gateway = InvalidToolGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, tool_plan(), repository, harness)
    with pytest.raises(RuntimeExecutionError, match="checkpoint.*(invalid|incompatible)"):
        await restored.restore_checkpoint(boundary)
        await collect(restored, checkpoint=boundary)
    assert gateway.requests == [] and harness.calls == []


@pytest.mark.parametrize("change", ["prepared", "running", "missing_artifact", "wrong_hash"])
async def test_partial_or_uncertain_tool_ledger_cannot_enter_correction(
    change: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = adapter.CrewDispatchRuntime._invalid_tool_history_quiet

    def checked(step: Any, ledger: Any) -> bool:
        assert ledger is not None
        states = {key: dict(row) for key, row in ledger.states.items()}
        key = next(iter(states))
        artifacts = dict(ledger.artifacts)
        if change in {"prepared", "running"}:
            states[key]["status"] = change
        elif change == "wrong_hash":
            states[key]["sha256"] = "a" * 64
        else:
            artifacts.pop(key)
        return original(step, adapter._ToolLedger(states=cast(Any, states), artifacts=artifacts))

    monkeypatch.setattr(adapter.CrewDispatchRuntime, "_invalid_tool_history_quiet", staticmethod(checked))
    gateway = InvalidToolGateway("tool", "invalid_tool")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="structured output invalid")
    assert len(gateway.requests) == 2 and len(harness.calls) == 1


@pytest.mark.parametrize("rejected_text", [PRIVATE, None], ids=["text", "tool-only"])
async def test_actual_chat_parser_gateway_and_bridge_preserve_completed_tool_results(
    rejected_text: str | None,
) -> None:
    """The OpenAI SDK has only a MockTransport; no sockets or provider calls."""
    wire: list[dict[str, Any]] = []
    clients: list[AsyncOpenAI] = []
    receipts: list[GatewayRejectedOutput] = []

    def handle(request: httpx.Request) -> httpx.Response:
        wire.append(json.loads(request.content))
        index = len(wire)
        assert index <= 4
        message: dict[str, Any] = {"role": "assistant", "content": (
            rejected_text if index == 2 else '{"summary":"complete handoff"}' if index == 3
            else "actual final answer" if index == 4 else None)}
        if index <= 2:
            message["tool_calls"] = [{
                "id": "approved" if index == 1 else "rejected-never-execute",
                "type": "function", "function": {"name": "web_search", "arguments": (
                    '{"q":"safe"}' if index == 1 else '{"q":INVALID_JSON')},
            }]
        return httpx.Response(200, json={
            "id": f"chat-{index}", "object": "chat.completion", "created": 1, "model": "mock-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if index <= 2 else "stop",
                         "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 16, "total_tokens": 17},
        })

    def factory(*, api_key: str, base_url: str, max_retries: int) -> AsyncOpenAI:
        assert max_retries == 0
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        clients.append(client)
        return client

    class RecordingGateway(ModelGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as receipt:
                receipts.append(receipt)
                raise

    registry = ModelRegistry([deployment(
        "primary", logical_model="general", capabilities=frozenset({
            ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING,
        }), request_model="mock-model", api_base="https://offline.example/v1",
        input_per_million_usd=Decimal(1), output_per_million_usd=Decimal(2),
    )])
    capacity = CapacityStub([lease("primary") for _ in range(4)])
    await capacity.initialize()
    gateway = RecordingGateway(registry, capacity, SecretStub([]),
        LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)))
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    try:
        events = await collect(runtime)
        assert len(wire) == 4 and len(harness.calls) == 1
        assert len(receipts) == 1
        evidence = receipts[0].evidence
        assert evidence is not None and evidence.reason == "invalid_tool"
        assert evidence.final_text == rejected_text and evidence.usage_status == "known"
        scope = get_gateway_scope_diagnostic(receipts[0])
        assert scope is not None and scope.transport_entered_count == 1
        assert not wire[2].get("tools")
        assert "rejected-never-execute" not in json.dumps(wire[2])
        assert PRIVATE not in json.dumps(wire[2])
        assert wire[2]["messages"][:-1] == wire[1]["messages"]
        assert_private_rejection(events, PRIVATE)
    finally:
        for client in clients:
            await client.close()


async def test_native_workspace_correction_cannot_bypass_missing_bundle_or_repeat_write() -> None:
    from agent_hub.models.types import ModelResponse
    from tests.unit.runtime.crew.test_adapter_failure_reason import (
        WorkspaceDeliverySequenceGateway,
        _workspace_delivery_bundle_response,
        _workspace_delivery_sequence_context,
    )
    from tests.unit.runtime.crew.test_meaningful_workspace_progress import (
        FINAL,
        Observations,
        write,
    )
    from tests.unit.runtime.crew.test_native_tool_continuation import native
    from tests.unit.runtime.crew.test_native_tool_continuation import subject as native_subject

    class NativeGateway(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == 1:
                # Issue the same bounded receipt via a fake gateway, preserving request identity.
                rejected = InvalidToolGateway("invalid_tool")
                self.requests.append(request)
                return await rejected.complete_with_context(request)
            return await super().complete_with_context(request)

    gateway = NativeGateway((
        native(write()), FINAL,
        ModelResponse(text="Bundle is still missing; work is incomplete.", usage=TokenUsage(1, 1, 2)),
        native(_workspace_delivery_bundle_response()), FINAL,
    ))
    harness = Observations()
    runtime = native_subject(gateway, harness, InMemoryProviderContinuationRepository())
    runtime._plan = runtime._plan.model_copy(update={
        'total_token_budget': 100_000,
        'total_cost_usd': Decimal('0.10'),
        'steps': (runtime._plan.steps[0].model_copy(update={
            'token_budget': 100_000, 'cost_budget_usd': Decimal('0.10'),
        }),),
    })
    context = _workspace_delivery_sequence_context().model_copy(update={'token_budget': 100_000})
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", "workspace.bundle"]
    assert len(gateway.requests) == 5
    rejected, correction = gateway.requests[1:3]
    assert rejected.response_schema is correction.response_schema is None
    assert correction.tools == () and not correction.allow_fallback
    assert correction.messages[:-1] == rejected.messages
    assert any(message.native_assistant is not None for message in correction.messages)
    assert any(message.tool_call_id == "write-business" for message in correction.messages)
    assert gateway.requests[3].tools  # Existing delivery guard demands the still-missing bundle.
    saved = await runtime.save_checkpoint()
    assert "PRIVATE_NATIVE_CONTINUATION" not in json.dumps(saved.to_payload())
    assert_private_rejection(events, PRIVATE)
