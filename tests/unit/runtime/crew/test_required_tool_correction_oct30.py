"""Required milestones keep their tool grant through one paid-output correction."""
from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    ModelGateway,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.litellm_client import LiteLLMClient, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    ModelCapability,
    ModelRequest,
    ModelResponse,
    StructuredResponseSchema,
    TokenUsage,
    ToolCall,
)
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.capabilities.test_scoped_read import RUN, TENANT
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
)
from tests.unit.runtime.crew.test_engineering_delivery_execution import (
    FINAL,
    ActualCapabilityHarness,
    engineering_runtime,
    verify_response,
)
from tests.unit.runtime.crew.test_invalid_tool_correction import PRIVATE, InvalidToolGateway
from tests.unit.runtime.crew.test_meaningful_workspace_progress import offline_only  # noqa: F401
from tests.unit.runtime.crew.test_native_tool_continuation import native
from tests.unit.runtime.crew.test_structured_handoff_repair import assert_private_rejection


class RequiredGateway(WorkspaceDeliverySequenceGateway):
    change = "no_text"
    reject_at = frozenset({1})

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if len(self.requests) in self.reject_at:
            self.requests.append(request)
            rejected = InvalidToolGateway("invalid_tool")
            rejected.change = self.change
            return await rejected.complete_with_context(request)
        return await super().complete_with_context(request)


def subject(model: Any, harness: Any, artifacts: Any, provider: Any) -> CrewDispatchRuntime:
    base = engineering_runtime(model, harness, artifacts)
    plan = base._plan.model_copy(update={"total_token_budget": 100_000, "total_cost_usd": Decimal(1), "steps": (
        base._plan.steps[0].model_copy(update={
            "engineering_action_version": 1, "token_budget": 100_000, "cost_budget_usd": Decimal(1),
        }),
    )})
    return CrewDispatchRuntime(
        model, plan, capability_gateway=base._capabilities, harness_tool_gateway=harness,
        crew_factory=base._factory, artifact_repository=artifacts,
        provider_continuation_repository=provider,
    )


def gateway(*, native_history: bool = True) -> RequiredGateway:
    inspect = ModelResponse(text=None, tool_calls=(
        ToolCall(id="inspect-once", name="project.inspect", arguments={}),
    ), usage=TokenUsage(1, 1, 2))
    responses = (inspect, FINAL, verify_response(), FINAL, _workspace_delivery_bundle_response(), FINAL)
    return RequiredGateway(tuple(native(response) if native_history else response for response in responses))


def context() -> Any:
    return _workspace_delivery_sequence_context().model_copy(update={
        "tenant_id": TENANT, "run_id": RUN, "token_budget": 100_000,
    })


@pytest.mark.parametrize("native_history", [False, True])
async def test_required_verify_rejection_corrects_once_without_replaying_inspect(
    tmp_path: Path, native_history: bool,
) -> None:
    model = gateway(native_history=native_history)
    harness = ActualCapabilityHarness(tmp_path, Executor())
    artifacts = InMemoryArtifactRepository()
    provider = InMemoryProviderContinuationRepository() if native_history else None
    runtime = subject(model, harness, artifacts, provider)
    events = [event async for event in runtime.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == ["project.inspect", "project.verify", "workspace.bundle"]
    rejected, correction = model.requests[1:3]
    assert rejected.required_tool == correction.required_tool == "project_verify"
    assert rejected.tools == correction.tools
    assert rejected.response_schema == correction.response_schema
    assert rejected.native_tool_continuation == correction.native_tool_continuation
    assert correction.messages[:-1] == rejected.messages
    assert not correction.allow_fallback
    assert correction.max_output_tokens <= rejected.max_output_tokens
    assert correction.timeout_seconds <= rejected.timeout_seconds
    assert PRIVATE not in str(correction.messages)
    assert len(model.requests) == 6
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    saved = await runtime.save_checkpoint()
    assert saved.state["required_tool_correction_version"] == 1
    repair = saved.state["structured_repairs"]["implementer_step"]
    assert repair["version"] == 4 and repair["status"] == "succeeded"
    assert "PRIVATE_NATIVE_CONTINUATION" not in json.dumps(saved.to_payload())
    tools = list(saved.state["tools"].values())
    assert sorted(row["round"] for row in tools) == [0, 2, 4]
    cached = RequiredGateway(())
    restored = subject(cached, harness, artifacts, provider)
    await restored.restore_checkpoint(saved)
    replay = [event async for event in restored.run(context().model_copy(update={"checkpoint": saved}))]
    assert replay[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not cached.requests and len(harness.calls) == 3


async def completed(tmp_path: Path) -> tuple[Any, ...]:
    model = gateway()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    artifacts = InMemoryArtifactRepository()
    provider = InMemoryProviderContinuationRepository()
    runtime = subject(model, harness, artifacts, provider)
    events = [event async for event in runtime.run(context())]
    return runtime, model, harness, artifacts, provider, events


def boundary(events: Any, phase: str) -> RuntimeCheckpoint:
    for event in events:
        checkpoint = event.checkpoint
        if checkpoint is None or checkpoint.state["phase"] != "running":
            continue
        models = list(checkpoint.state["models"].values())
        if phase == "source":
            if len(models) == 2 and any(row["status"] == "rejected" for row in models):
                return checkpoint
        else:
            repair = checkpoint.state["structured_repairs"].get("implementer_step")
            if repair and repair["version"] == 4:
                row = checkpoint.state["models"][repair["correction_key"]]
                if row["status"] == phase:
                    return checkpoint
    raise AssertionError(f"missing {phase} boundary")


@pytest.mark.parametrize("phase", ["source", "prepared", "running", "succeeded"])
async def test_only_a_durable_reserved_slot_resumes_without_replaying_the_prefix(
    tmp_path: Path, phase: str,
) -> None:
    _, _, _, artifacts, provider, events = await completed(tmp_path)
    checkpoint = boundary(events, phase)
    before = checkpoint.to_payload()
    # Reproduce the crash boundary, not the later snapshots from the fixture's completed run.
    prefix_provider = InMemoryProviderContinuationRepository()
    for row in checkpoint.state["models"].values():
        reference = row.get("provider_continuation")
        if reference:
            await prefix_provider.put(await provider.get(
                checkpoint.tenant_id, checkpoint.run_id, UUID(reference["id"]), reference["sha256"],
            ))
    responses = (native(verify_response()), native(FINAL), native(_workspace_delivery_bundle_response()), native(FINAL))
    model = WorkspaceDeliverySequenceGateway(responses if phase == "prepared" else responses[1:])
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runtime = subject(model, harness, artifacts, prefix_provider)
    await runtime.restore_checkpoint(checkpoint)
    task = context().model_copy(update={"checkpoint": checkpoint})
    if phase in {"source", "running"}:
        with pytest.raises(RuntimeExecutionError, match="structured output invalid|outcome requires confirmation"):
            _ = [event async for event in runtime.run(task)]
        assert not model.requests and not harness.calls
    else:
        replay = [event async for event in runtime.run(task)]
        assert replay[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(model.requests) == (4 if phase == "prepared" else 3)
        assert [call.tool_name for call in harness.calls] == ["project.verify", "workspace.bundle"]
        saved = await runtime.save_checkpoint()
        assert saved.state["usage"]["tokens"] == subject_saved(events)["usage"]["tokens"]
    assert checkpoint.to_payload() == before


def subject_saved(events: Any) -> Any:
    return next(event.checkpoint.state for event in reversed(events) if event.checkpoint is not None)


@pytest.mark.parametrize("change", ["untrusted", "multi_transport", "missing", "invalid", "unknown", "fallback", "model"])
async def test_ineligible_receipt_cannot_reserve_or_dispatch_a_correction(tmp_path: Path, change: str) -> None:
    model = gateway()
    model.change = change
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runtime = subject(model, harness, InMemoryArtifactRepository(), InMemoryProviderContinuationRepository())
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(context())]
    assert len(model.requests) == 2
    assert [call.tool_name for call in harness.calls] == ["project.inspect"]
    assert (await runtime.save_checkpoint()).state["structured_repairs"] == {}


async def test_a_second_invalid_batch_never_gets_another_correction(tmp_path: Path) -> None:
    model = gateway()
    model.reject_at = frozenset({1, 2})
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runtime = subject(model, harness, InMemoryArtifactRepository(), InMemoryProviderContinuationRepository())
    with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
        _ = [event async for event in runtime.run(context())]
    assert len(model.requests) == 3
    assert [call.tool_name for call in harness.calls] == ["project.inspect"]


@pytest.mark.parametrize("kind", ["wrong", "duplicate"])
async def test_corrector_cannot_dispatch_an_expanded_or_wrong_tool_batch(tmp_path: Path, kind: str) -> None:
    model = gateway()
    responses = list(model.responses)
    calls = (ToolCall(id="wrong-write", name="workspace.write_text", arguments={"path": "x.py", "content": "x"}),)
    if kind == "duplicate":
        calls = (ToolCall(id="verify-one", name="project.verify", arguments={}),
                 ToolCall(id="verify-two", name="project.verify", arguments={}))
    responses[2] = native(ModelResponse(text=None, tool_calls=calls, usage=TokenUsage(1, 1, 2)))
    model.responses = tuple(responses)
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runtime = subject(model, harness, InMemoryArtifactRepository(), InMemoryProviderContinuationRepository())
    with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
        _ = [event async for event in runtime.run(context())]
    assert len(model.requests) == 3
    assert [call.tool_name for call in harness.calls] == ["project.inspect"]


async def test_source_spends_the_remaining_cost_allowance_before_any_correction(tmp_path: Path) -> None:
    model = gateway()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runtime = subject(model, harness, InMemoryArtifactRepository(), InMemoryProviderContinuationRepository())
    runtime._plan = runtime._plan.model_copy(update={"total_cost_usd": Decimal("0.01"), "steps": (
        runtime._plan.steps[0].model_copy(update={"cost_budget_usd": Decimal("0.01")}),
    )})
    with pytest.raises(RuntimeExecutionError, match="budget exhausted"):
        _ = [event async for event in runtime.run(context())]
    assert len(model.requests) == 2
    assert [call.tool_name for call in harness.calls] == ["project.inspect"]
    assert (await runtime.save_checkpoint()).state["structured_repairs"] == {}


async def test_correction_cannot_buy_a_model_slot_past_the_original_hard_round_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_tool_round_budget", lambda *_: adapter._ToolRoundBudget(1, 1, 1))
    model = gateway()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runtime = subject(model, harness, InMemoryArtifactRepository(), InMemoryProviderContinuationRepository())
    with pytest.raises(RuntimeExecutionError, match="round limit"):
        _ = [event async for event in runtime.run(context())]
    assert len(model.requests) == 2
    assert [call.tool_name for call in harness.calls] == ["project.inspect"]
    assert (await runtime.save_checkpoint()).state["structured_repairs"] == {}


async def test_rejected_body_is_retained_privately_but_never_sent_to_the_corrector(tmp_path: Path) -> None:
    model = gateway()
    model.change = ""
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runtime = subject(model, harness, InMemoryArtifactRepository(), InMemoryProviderContinuationRepository())
    events = [event async for event in runtime.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert PRIVATE not in str(model.requests[2].messages)
    assert_private_rejection(events, PRIVATE)
    saved = await runtime.save_checkpoint()
    assert any(row["final_text"] == PRIVATE for row in saved.state["rejected_outputs"].values())


@pytest.mark.parametrize("reason", ["invalid_json", "schema_mismatch"])
@pytest.mark.parametrize("native_history", [False, True])
async def test_format_rejection_is_not_authority_for_a_tool_enabled_v4_slot(
    tmp_path: Path, reason: str, native_history: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FormatGateway(RequiredGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                assert error.evidence is not None
                error.evidence = replace(error.evidence, reason=cast(Any, reason), final_text=PRIVATE)
                raise

    base = gateway(native_history=native_history)
    model = FormatGateway(base.responses)
    harness = ActualCapabilityHarness(tmp_path, Executor())
    provider = InMemoryProviderContinuationRepository() if native_history else None
    runtime = subject(model, harness, InMemoryArtifactRepository(), provider)
    # Probe the executor's schema-bearing ModelRequest contract. The current workspace
    # loop selects schema=None, but neither format reason may ever authorize a v4 grant.
    execute = runtime._execute_model_request

    async def with_schema(task: Any, step: Any, actor: Any, request: ModelRequest, **kwargs: Any) -> Any:
        return await execute(task, step, actor, replace(
            request, required_capabilities=request.required_capabilities | {ModelCapability.STRUCTURED_OUTPUT},
            response_schema=StructuredResponseSchema(name="Engineering", schema={"type": "object"}),
        ), **kwargs)

    monkeypatch.setattr(runtime, "_execute_model_request", with_schema)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(context())]
    assert len(model.requests) == 2
    assert [call.tool_name for call in harness.calls] == ["project.inspect"]
    assert (await runtime.save_checkpoint()).state["structured_repairs"] == {}


@pytest.mark.parametrize("change", ["history", "receipt", "request", "correction", "mode", "policy", "actor"])
async def test_changed_link_or_prefix_is_rejected_before_any_call(tmp_path: Path, change: str) -> None:
    _, _, _, artifacts, provider, events = await completed(tmp_path)
    payload = boundary(events, "prepared").to_payload()
    state = payload["state"]
    repair = state["structured_repairs"]["implementer_step"]
    if change == "policy":
        state.pop("required_tool_correction_version")
    else:
        field = {"history": "tool_history_sha256", "receipt": "source_receipt_sha256",
                 "request": "source_request_sha256", "correction": "correction_request_sha256",
                 "mode": "mode", "actor": "actor"}[change]
        repair[field] = "a" * 64
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    model = WorkspaceDeliverySequenceGateway(())
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runtime = subject(model, harness, artifacts, provider)
    with pytest.raises(RuntimeExecutionError):
        await runtime.restore_checkpoint(checkpoint)
        _ = [event async for event in runtime.run(context().model_copy(update={"checkpoint": checkpoint}))]
    assert not model.requests and not harness.calls


@pytest.mark.parametrize(("bad_batch", "negotiate"), [
    ("wrong", False), ("malformed", False), ("wrong", True),
    ("duplicate_verify", False), ("duplicate_bundle", False),
])
async def test_actual_named_tool_native_client_corrects_without_dispatching_the_invalid_batch(
    tmp_path: Path, bad_batch: str, negotiate: bool,
) -> None:
    wire: list[dict[str, Any]] = []
    clients: list[AsyncOpenAI] = []
    receipts: list[GatewayRejectedOutput] = []
    requests: list[ModelRequest] = []
    index = 0
    negotiated = False
    duplicate = bad_batch.startswith("duplicate_")
    bundle = bad_batch == "duplicate_bundle"
    source_index = 3 if bundle else 1
    required_name = "workspace_bundle" if bundle else "project_verify"
    inspect = ModelResponse(text=None, tool_calls=(ToolCall(
        id="inspect-once", name="project.inspect", arguments={},
    ),), usage=TokenUsage(1, 1, 2))
    responses = ((inspect, verify_response(), FINAL, FINAL, _workspace_delivery_bundle_response(), FINAL)
                 if bundle else (inspect, FINAL, verify_response("corrected-verify"), FINAL,
                                 _workspace_delivery_bundle_response(), FINAL))

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal index, negotiated
        data = json.loads(request.content)
        wire.append(data)
        assert len(wire) <= 7
        if negotiate and index == 1 and not negotiated:
            negotiated = True
            assert data["tool_choice"] == {"type": "function", "function": {"name": "project_verify"}}
            return httpx.Response(400, json={"error": {"message": (
                "The tool_choice parameter does not support being set to required or object in thinking mode"
            )}})
        response = responses[index]
        message: dict[str, Any] = {"role": "assistant", "content": response.text,
                                   "reasoning_content": "PRIVATE_NATIVE_CONTINUATION"}
        if index == source_index:
            message["content"] = None
            message["tool_calls"] = [{"id": "invalid-never-execute", "type": "function", "function": {
                "name": "project_verify" if bad_batch == "malformed" else "workspace_write_text",
                "arguments": '{"x":' + PRIVATE if bad_batch == "malformed" else "{}",
            }}]
        elif response.tool_calls:
            message["tool_calls"] = [{"id": call.id, "type": "function", "function": {
                "name": call.name.replace(".", "_"), "arguments": json.dumps(dict(call.arguments)),
            }} for call in response.tool_calls]
        if duplicate and index == source_index + 1:
            first = message["tool_calls"][0]
            message["tool_calls"].append({**first, "id": "duplicate-never-execute"})
        index += 1
        return httpx.Response(200, json={
            "id": f"response-{index}", "object": "chat.completion", "created": 1, "model": "mock-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
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
            requests.append(request)
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
    capacity = CapacityStub([lease("primary") for _ in range(6)])
    await capacity.initialize()
    model = RecordingGateway(registry, capacity, SecretStub([]),
                             LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)))
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runtime = subject(model, harness, InMemoryArtifactRepository(), InMemoryProviderContinuationRepository())
    try:
        if duplicate:
            with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
                _ = [event async for event in runtime.run(context())]
            assert [call.tool_name for call in harness.calls] == (
                ["project.inspect", "project.verify"] if bundle else ["project.inspect"]
            )
        else:
            events = [event async for event in runtime.run(context())]
            assert events[-1].kind is EventKind.RUNTIME_COMPLETED
            assert [call.tool_name for call in harness.calls] == ["project.inspect", "project.verify", "workspace.bundle"]
        expected_calls = source_index + 2 if duplicate else 6
        assert len(requests) == expected_calls and len(wire) == expected_calls + int(negotiate)
        assert len(receipts) == 1
        evidence = receipts[0].evidence
        assert evidence is not None and evidence.reason == "invalid_tool"
        assert evidence.status == "completed" and evidence.usage_status == "known" and evidence.final_text is None
        scope = get_gateway_scope_diagnostic(receipts[0])
        assert scope is not None and scope.transport_entered_count == 1 and scope.failure_attempt_count == 0
        original, corrected = requests[source_index:source_index + 2]
        assert original.required_tool == corrected.required_tool == required_name
        assert corrected.tools == original.tools and corrected.response_schema == original.response_schema
        assert corrected.native_tool_continuation and not corrected.allow_fallback
        correction_wire = wire[source_index + 1 + int(negotiate)]
        assert correction_wire["tool_choice"] == {"type": "function", "function": {"name": required_name}}
        assert correction_wire["tools"] == wire[source_index]["tools"]
        assert "invalid-never-execute" not in json.dumps(correction_wire) and PRIVATE not in json.dumps(correction_wire)
        assert any(message.get("tool_call_id") == "inspect-once" for message in correction_wire["messages"])
        saved = await runtime.save_checkpoint()
        assert saved.state["usage"]["tokens"] == expected_calls * 17
        if duplicate:
            assert saved.state["structured_repairs"]["implementer_step"]["status"] == "rejected"
            assert len(saved.state["rejected_outputs"]) == 2
        assert "PRIVATE_NATIVE_CONTINUATION" not in json.dumps(saved.to_payload())
    finally:
        for client in clients:
            await client.close()


async def test_legacy_missing_policy_is_not_upgraded_by_a_fresh_engineering_plan(tmp_path: Path) -> None:
    _, _, _, artifacts, provider, events = await completed(tmp_path)
    payload = boundary(events, "source").to_payload()
    payload["state"].pop("required_tool_correction_version")
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    model = gateway()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runtime = subject(model, harness, artifacts, provider)
    await runtime.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
        _ = [event async for event in runtime.run(context().model_copy(update={"checkpoint": checkpoint}))]
    assert not model.requests and not harness.calls
