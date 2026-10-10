"""First-call recovery must never replay an executed or uncertain capability."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal, cast

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models.failure_receipt import get_gateway_failure_receipt
from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    GatewayResponseCancelled,
    ModelGateway,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.litellm_client import LiteLLMClient, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import ModelCapability, ModelRequest, RejectedOutputEvidence, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.runtime.crew.plan import DispatchPlan
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FakeCapabilities,
    FastFactory,
    RecordingHarnessToolGateway,
)
from tests.unit.runtime.crew.test_known_incomplete_recovery import (
    SOURCE_USAGE,
    IncompleteGateway,
    baseline_runtime_type,
    collect,
    plan,
)
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping

__all__ = ["baseline_runtime_type"]


def tool_plan(tokens: int = 30_000) -> DispatchPlan:
    base = plan(tokens=tokens)
    return base.model_copy(update={
        "allowed_tools": ("web.search",),
        "agents": (base.agents[0].model_copy(update={"allowed_tools": ("web.search",)}),
                   *base.agents[1:]),
        "steps": (base.steps[0].model_copy(update={"tools": ("web.search",)}),
                  *base.steps[1:]),
    })


class ToolIncompleteGateway(IncompleteGateway):
    def __init__(self, *outcomes: str) -> None:
        super().__init__(*(outcomes or ("incomplete", "tool", "success", "final")))

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        outcome = self.outcomes[len(self.requests)]
        completion = await super().complete_with_context(request)
        if outcome == "tool":
            return replace(completion, response=replace(completion.response, tool_calls=(
                ToolCall(id="fresh-approved", name="web_search", arguments={"q": "safe"}),
            )))
        return completion


class NonReplayCapabilities(FakeCapabilities):
    def is_replay_safe(self, name: str) -> bool:
        return False


def subject(gateway: ToolIncompleteGateway | ModelGateway, dispatch: DispatchPlan,
            repository: InMemoryArtifactRepository,
            harness: RecordingHarnessToolGateway) -> CrewDispatchRuntime:
    return CrewDispatchRuntime(
        gateway, dispatch, artifact_repository=repository, crew_factory=FastFactory(),
        capability_gateway=NonReplayCapabilities(), harness_tool_gateway=harness,
    )


def draft_models(checkpoint: RuntimeCheckpoint) -> list[Mapping[str, Any]]:
    return sorted((mapping(value) for value in mapping(checkpoint.state["models"]).values()
                   if mapping(value)["step_id"] == "draft"),
                  key=lambda value: (value["attempt"], value["call_index"]))


async def test_first_tool_call_incomplete_regenerates_before_one_approved_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_extension(*args: object) -> float:
        raise AssertionError("incomplete recovery extended the original deadline")

    monkeypatch.setattr(CrewDispatchRuntime, "_recovery_step_deadline", no_extension)
    gateway = ToolIncompleteGateway()
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    events = await collect(runtime)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4 and len(harness.calls) == 1
    original, fresh, followup, _ = gateway.requests
    assert fresh.logical_model == followup.logical_model == "selected"
    assert fresh.response_schema == followup.response_schema == original.response_schema
    assert fresh.tools == followup.tools == original.tools
    assert not fresh.allow_fallback and not followup.allow_fallback
    assert fresh.max_output_tokens == original.max_output_tokens == 6144
    assert harness.calls[0][1].tool_name == "web.search"
    assert harness.calls[0][1].actor == "writer"
    prompt = " ".join(str(message.content) for message in fresh.messages)
    assert "authorized tool if needed" in prompt and "without calling tools" not in prompt
    assert sum(event.kind is EventKind.STEP_RETRYING for event in events) == 1
    checkpoint = await runtime.save_checkpoint()
    assert [(row["attempt"], row["call_index"], row["status"])
            for row in draft_models(checkpoint)] == [
        (0, 0, "rejected"), (1, 0, "succeeded"), (1, 1, "succeeded"),
    ]
    assert draft_models(checkpoint)[0]["failure_reason"] == "model response incomplete"
    assert checkpoint.state["structured_repairs"] == {}
    assert mapping(checkpoint.state["usage"])["tokens"] == 8423


@pytest.mark.parametrize("tokens", [30_000, 9000])
@pytest.mark.parametrize("phase", [
    "source", "fresh_prepared", "fresh_running", "tool_prepared", "tool_running",
    "tool_succeeded", "followup_prepared", "followup_running", "followup_succeeded",
])
async def test_natural_restore_distinguishes_recovery_from_normal_tool_loop(
    phase: str, tokens: int,
) -> None:
    dispatch = tool_plan(tokens)
    repository = InMemoryArtifactRepository()
    source_harness = RecordingHarnessToolGateway()
    source = subject(ToolIncompleteGateway(), dispatch, repository, source_harness)
    events = await collect(source, tokens=tokens)

    def matches(checkpoint: RuntimeCheckpoint) -> bool:
        rows = draft_models(checkpoint)
        tools = [mapping(value) for value in mapping(checkpoint.state["tools"]).values()]
        if checkpoint.state["phase"] != "running" or not rows:
            return False
        if phase == "source":
            return len(rows) == 1 and rows[0]["status"] == "rejected" and not tools
        prefix, status = phase.split("_")
        if prefix == "fresh":
            return len(rows) == 2 and rows[-1]["status"] == status and not tools
        if prefix == "tool":
            return len(rows) == 2 and len(tools) == 1 and tools[0]["status"] == status
        return len(rows) == 3 and rows[-1]["status"] == status and bool(tools)

    checkpoint = next(event.checkpoint for event in events
                      if event.checkpoint is not None and matches(event.checkpoint))
    before = checkpoint.to_payload()
    uncertain = phase.endswith("running")
    outcomes = (("tool", "success", "final") if phase in {"source", "fresh_prepared"}
                else ("final",) if phase == "followup_succeeded" else ("success", "final"))
    gateway = ToolIncompleteGateway(*outcomes)
    restored_harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, restored_harness)
    await restored.restore_checkpoint(checkpoint)
    await collect(restored, checkpoint=checkpoint, tokens=tokens,
                  fail="outcome requires confirmation" if uncertain else None)
    if uncertain:
        assert gateway.requests == [] and restored_harness.calls == []
        assert (await restored.save_checkpoint()).to_payload() == before
    else:
        assert len(gateway.requests) == len(outcomes)
        expected_tools = int(phase in {"source", "fresh_prepared", "tool_prepared"})
        assert len(restored_harness.calls) == expected_tools
        saved = await restored.save_checkpoint()
        assert mapping(saved.state["usage"])["tokens"] == 8423
        assert mapping(saved.state["usage"])["cost_usd"] == "0.04"
        assert saved.state["structured_repairs"] == {}
        for key, row in mapping(checkpoint.state["models"]).items():
            if row["status"] in {"succeeded", "rejected"}:
                assert mapping(saved.state["models"])[key] == row
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("second", ["incomplete", "invalid", "transport"])
async def test_fresh_failure_never_invokes_a_tool_or_starts_another_recovery(second: str) -> None:
    gateway = ToolIncompleteGateway("incomplete", second)
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    events = await collect(runtime, fail="incomplete|structured output invalid|provider unavailable")
    assert len(gateway.requests) == 2 and harness.calls == []
    assert sum(event.kind is EventKind.STEP_RETRYING for event in events) == 1
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["structured_repairs"] == {}
    assert checkpoint.state["tools"] == {}


async def test_incomplete_after_first_tool_execution_does_not_recover() -> None:
    gateway = ToolIncompleteGateway("tool", "incomplete")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    events = await collect(runtime, fail="structured output invalid")
    assert len(gateway.requests) == 2 and len(harness.calls) == 1
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    assert not any(row["failure_reason"] == "model response incomplete"
                   for row in draft_models(await runtime.save_checkpoint()))


@pytest.mark.parametrize("prior_unknown", [False, True])
async def test_real_gateway_sdk_mock_first_call_tools_or_unknown_scope(prior_unknown: bool) -> None:
    wire: list[dict[str, Any]] = []
    requests: list[ModelRequest] = []
    rejections: list[GatewayRejectedOutput] = []
    clients: list[AsyncOpenAI] = []

    def handle(request: httpx.Request) -> httpx.Response:
        wire.append(json.loads(request.content))
        index = len(wire)
        assert index <= (2 if prior_unknown else 4), "unexpected provider call"
        assert request.method == "POST"
        assert request.url.path == ("/v1/chat/completions" if index == 4 else "/v1/responses")
        if index == 4:
            return httpx.Response(200, json={
                "id": "chat-own", "object": "chat.completion", "created": 1,
                "model": "native-model", "choices": [{
                    "index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "final answer"},
                }], "usage": {"prompt_tokens": 1, "completion_tokens": 16, "total_tokens": 17},
            })
        if prior_unknown and index == 1:
            raise httpx.ReadTimeout("PRIVATE_SCOPE_GAP", request=request)
        incomplete = index == (2 if prior_unknown else 1)
        if incomplete:
            output: list[dict[str, object]] = [{
                "id": "call-discarded", "type": "function_call", "status": "incomplete",
                "call_id": "discarded", "name": "web_search", "arguments": '{"q":"PRIVATE_PARTIAL',
            }]
        elif index == 2:
            output = [{
                "id": "call-own", "type": "function_call", "status": "completed",
                "call_id": "fresh-approved", "name": "web_search", "arguments": '{"q":"safe"}',
            }]
        else:
            output = [{
                "id": "msg-own", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "annotations": [], "text":
                             '{"summary":"complete handoff"}' if index == 3 else "final answer"}],
            }]
        return httpx.Response(200, json={
            "id": f"resp-{index}", "object": "response", "created_at": 1, "model": "native-model",
            "status": "incomplete" if incomplete else "completed", "error": None,
            "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
            "output": output,
            "usage": {"input_tokens": 2234, "output_tokens": 6144, "total_tokens": 8378}
            if incomplete else {"input_tokens": 1, "output_tokens": 16, "total_tokens": 17},
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
            except GatewayRejectedOutput as error:
                rejections.append(error)
                raise

    capabilities = frozenset({ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT,
                              ModelCapability.TOOL_CALLING})
    registry = ModelRegistry([
        deployment(identifier, logical_model=logical_model, capabilities=capabilities,
                   structured_output_api="responses", request_model="native-model",
                   api_base="https://own-native.example/v1", input_per_million_usd=Decimal(1),
                   output_per_million_usd=Decimal(2))
        for identifier, logical_model in (("primary", "general"), ("selected", "selected"))
    ])
    capacity = CapacityStub([lease(identifier) for identifier in (
        ("primary", "selected") if prior_unknown else ("primary",) * 4
    )])
    gateway = RecordingGateway(registry, capacity, SecretStub([]),
        LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)),
        fallbacks={"general": "selected"})
    await capacity.initialize()
    repository = InMemoryArtifactRepository()
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), repository, harness)
    try:
        events = await collect(runtime, fail="dispatch usage unaccounted" if prior_unknown else None)
        assert len(rejections) == 1
        scope = get_gateway_scope_diagnostic(rejections[0])
        assert scope is not None
        assert scope.transport_entered_count == (2 if prior_unknown else 1)
        assert scope.failure_attempt_count == int(prior_unknown)
        assert (get_gateway_failure_receipt(rejections[0]) is not None) is prior_unknown
        checkpoint = await runtime.save_checkpoint()
        assert checkpoint.state["structured_repairs"] == {}
        assert "PRIVATE_PARTIAL" not in str(checkpoint.to_payload())
        if prior_unknown:
            assert len(wire) == 2 and len(requests) == 1 and harness.calls == []
            assert checkpoint.state["phase"] == "unaccounted"
            assert mapping(checkpoint.state["usage"])["tokens"] == 8378
            assert draft_models(checkpoint)[0]["failure_reason"] == "structured output rejected"
            replay = subject(gateway, tool_plan(), repository, harness)
            await replay.restore_checkpoint(checkpoint)
            await collect(replay, checkpoint=checkpoint, fail="dispatch usage unaccounted")
            assert len(wire) == 2 and harness.calls == []
            assert (await replay.save_checkpoint()).to_payload() == checkpoint.to_payload()
        else:
            assert events[-1].kind is EventKind.RUNTIME_COMPLETED
            assert len(wire) == 4 and len(requests) == 4 and len(harness.calls) == 1
            original, fresh, followup, _ = requests
            assert original.logical_model == fresh.logical_model == followup.logical_model == "general"
            assert fresh.response_schema == followup.response_schema == original.response_schema
            assert fresh.tools == followup.tools == original.tools
            assert not fresh.allow_fallback and not followup.allow_fallback
            assert fresh.max_output_tokens == original.max_output_tokens == 6144
            assert wire[0]["tools"] == wire[1]["tools"] == wire[2]["tools"]
            assert wire[0]["text"] == wire[1]["text"] == wire[2]["text"]
            assert harness.calls[0][1].arguments == {"q": "safe"}
            assert mapping(checkpoint.state["usage"])["tokens"] == 8429
            assert Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) == Decimal("0.014621")
    finally:
        for client in clients:
            await client.close()


@pytest.mark.parametrize("excluded", [
    "attempt", "call", "schema", "review", "repair", "tools", "missing", "partial",
    "prepared", "running", "succeeded", "failed", "uncertain", "source_tool_on_restore",
])
def test_tool_first_call_qualification_refuses_noninitial_or_effectful_source(excluded: str) -> None:
    dispatch = tool_plan()
    step = dispatch.steps[0]
    request = ModelRequest(
        logical_model="general", messages=(), tools=adapter._tool_definitions(step.tools),
        response_schema=adapter._agent_response_schema(dispatch.agents[0]),
        required_capabilities=frozenset({ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT,
                                         ModelCapability.TOOL_CALLING}),
    )
    evidence = RejectedOutputEvidence(final_text=None, usage=SOURCE_USAGE, usage_status="known",
                                      status="incomplete", reason="incomplete")
    tools = adapter._ToolLedger()
    attempt = int(excluded == "attempt")
    call_index = int(excluded == "call")
    purpose: Literal["step", "review"] = "review" if excluded == "review" else "step"
    if excluded == "schema":
        request = replace(request, response_schema=None)
    elif excluded == "tools":
        request = replace(request, tools=adapter._tool_definitions(("workspace.write_text",)))
    elif excluded == "missing":
        evidence = replace(evidence, usage=None, usage_status="missing")
    elif excluded == "partial":
        evidence = replace(evidence, status="completed", reason="invalid_json",
                           final_text='{"summary":"partial')
    elif excluded in {"prepared", "running", "succeeded", "failed", "uncertain",
                      "source_tool_on_restore"}:
        tools.states["effect"] = {"step_id": step.id, "attempt": 0, "status": excluded}
    assert not adapter._known_incomplete_recovery_eligible(
        step=step, request=request, purpose=purpose, repair={} if excluded == "repair" else None,
        evidence=evidence, tool_ledger=tools, attempt=attempt, call_index=call_index,
        allow_later_tools=excluded == "source_tool_on_restore",
    )


@pytest.mark.parametrize("change", [
    "source_hash", "fresh_hash", "actor", "receipt", "sources", "schema", "budget", "legacy",
    "source_tool",
])
async def test_natural_restore_tool_recovery_refuses_changed_or_unqualified_boundary(
    change: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = tool_plan(9000)
    repository = InMemoryArtifactRepository()
    source = subject(ToolIncompleteGateway(), dispatch, repository, RecordingHarnessToolGateway())
    events = await collect(source, tokens=9000)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and len(draft_models(event.checkpoint)) == (2 if change in {
                          "fresh_hash", "budget", "source_tool",
                      } else 1)
                      and draft_models(event.checkpoint)[-1]["status"] == (
                          "succeeded" if change == "source_tool" else "prepared"
                          if change in {"fresh_hash", "budget"} else "rejected")
                      and (bool(event.checkpoint.state["tools"]) if change == "source_tool" else
                           not event.checkpoint.state["tools"]))
    before = checkpoint.to_payload()
    payload = checkpoint.to_payload()
    state = mapping(payload["state"])
    rows = sorted(state["models"].values(), key=lambda row: (row["attempt"], row["call_index"]))
    if change in {"source_hash", "fresh_hash"}:
        rows[int(change == "fresh_hash")]["request_sha256"] = "a" * 64
    elif change == "actor":
        rows[0]["actor"] = "reviewer"
    elif change == "receipt":
        next(iter(state["rejected_outputs"].values()))["reason"] = "invalid_output"
    elif change == "sources":
        next(iter(state["rejected_outputs"].values()))["source_ids"] = [
            "00000000-0000-0000-0000-000000000001",
        ]
    elif change == "legacy":
        rows[0]["failure_reason"] = "structured output rejected"
    elif change == "source_tool":
        next(iter(state["tools"].values()))["attempt"] = 0
    elif change == "schema":
        dispatch = dispatch.model_copy(update={"agents": (
            dispatch.agents[0].model_copy(update={"output_schema": {"other": "string"}}),
            *dispatch.agents[1:],
        )})
    payload["state_sha256"] = ""
    boundary = RuntimeCheckpoint.from_payload(payload)
    gateway = ToolIncompleteGateway("tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    replay = subject(gateway, dispatch, repository, harness)
    if change == "budget":
        original_budget = replay._incomplete_recovery_remaining_tokens

        def lower_reserved_budget(*args: Any, **kwargs: Any) -> int:
            return original_budget(*args, **kwargs) - 1

        monkeypatch.setattr(replay, "_incomplete_recovery_remaining_tokens", lower_reserved_budget)
    expected = ("model request changed after checkpoint" if change in {
        "source_hash", "fresh_hash", "budget",
    } else "structured output invalid" if change == "legacy" else
                "checkpoint.*(invalid|incompatible|unavailable)")
    with pytest.raises(RuntimeExecutionError, match=expected):
        await replay.restore_checkpoint(boundary)
        await collect(replay, checkpoint=boundary, tokens=9000)
    assert gateway.requests == [] and harness.calls == []
    assert checkpoint.to_payload() == before
    if change in {"source_hash", "fresh_hash", "budget"}:
        assert (await replay.save_checkpoint()).to_payload() == boundary.to_payload()


async def test_baseline_natural_tool_receipt_without_marker_never_recovers(
    baseline_runtime_type: type[CrewDispatchRuntime],
) -> None:
    dispatch = tool_plan()
    repository = InMemoryArtifactRepository()
    harness = RecordingHarnessToolGateway()
    gateway = ToolIncompleteGateway("incomplete")
    baseline = baseline_runtime_type(gateway, dispatch, artifact_repository=repository,
        crew_factory=FastFactory(), capability_gateway=NonReplayCapabilities(),
        harness_tool_gateway=harness)
    baseline_error = cast(type[Exception], sys.modules[baseline_runtime_type.__module__].RuntimeExecutionError)
    checkpoints: list[RuntimeCheckpoint] = []
    from tests.unit.runtime.crew.test_adapter_failure_reason import _context

    with pytest.raises(baseline_error, match="^structured output invalid$"):
        async for event in baseline.run(_context(token_budget=30_000)):
            if event.checkpoint is not None:
                checkpoints.append(event.checkpoint)
    checkpoint = next(candidate for candidate in checkpoints
                      if draft_models(candidate) and draft_models(candidate)[0]["status"] == "rejected"
                      and candidate.state["phase"] == "running")
    assert draft_models(checkpoint)[0]["failure_reason"] == "structured output rejected"
    assert mapping(checkpoint.state["usage"])["tokens"] == 8378
    assert len(gateway.requests) == 1 and harness.calls == []
    replay_gateway = ToolIncompleteGateway("tool", "success", "final")
    replay = subject(replay_gateway, dispatch, repository, harness)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail="structured output invalid")
    assert replay_gateway.requests == [] and harness.calls == []
    saved = await replay.save_checkpoint()
    for key in ("models", "rejected_outputs", "usage", "tools", "structured_repairs"):
        assert saved.state[key] == checkpoint.state[key]


@pytest.mark.parametrize("usage_status", ["missing", "invalid"])
async def test_tool_role_missing_usage_is_terminal_without_recovery(
    usage_status: Literal["known", "missing", "invalid"],
) -> None:
    gateway = ToolIncompleteGateway("incomplete")
    gateway.usage_status = usage_status
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="dispatch usage unaccounted")
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["phase"] == "unaccounted"
    assert len(gateway.requests) == 1 and harness.calls == []
    assert checkpoint.state["tools"] == {} and checkpoint.state["structured_repairs"] == {}


async def test_cancelled_tool_role_known_receipt_cannot_recover_or_replay() -> None:
    class CancelledGateway(ToolIncompleteGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as receipt:
                raise GatewayResponseCancelled(receipt=receipt) from None

    repository = InMemoryArtifactRepository()
    gateway = CancelledGateway("incomplete")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), repository, harness)
    with pytest.raises(asyncio.CancelledError):
        await collect(runtime)
    checkpoint = await runtime.save_checkpoint()
    assert draft_models(checkpoint)[0]["status"] == "received_cancelled"
    assert mapping(checkpoint.state["usage"])["tokens"] == 8378
    replay_gateway = ToolIncompleteGateway("tool")
    replay = subject(replay_gateway, tool_plan(), repository, harness)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail="model outcome requires confirmation")
    assert len(gateway.requests) == 1 and replay_gateway.requests == [] and harness.calls == []
    assert (await replay.save_checkpoint()).to_payload() == checkpoint.to_payload()


@pytest.mark.parametrize("blocked", ["tokens", "cost", "deadline"])
async def test_tool_recovery_keeps_terminal_budget_and_original_deadline(blocked: str) -> None:
    dispatch = tool_plan(8378 if blocked == "tokens" else 30_000)
    if blocked == "cost":
        dispatch = dispatch.model_copy(update={
            "total_cost_usd": Decimal("0.01"),
            "steps": tuple(step.model_copy(update={"cost_budget_usd": Decimal(0)})
                           for step in dispatch.steps),
        })
    elif blocked == "deadline":
        dispatch = dispatch.model_copy(update={"steps": (
            dispatch.steps[0].model_copy(update={"timeout_seconds": 1}), *dispatch.steps[1:],
        )})
    gateway = ToolIncompleteGateway("incomplete")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    events = await collect(runtime, tokens=dispatch.total_token_budget,
                           fail="model response incomplete" if blocked == "deadline" else "budget exhausted")
    assert len(gateway.requests) == 1 and harness.calls == []
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    assert mapping((await runtime.save_checkpoint()).state["usage"])["tokens"] == 8378


@pytest.mark.parametrize("tokens,cap,exhausted", [(9000, 605, False), (8412, 17, True)])
async def test_recovery_tool_loop_clamps_each_round_and_never_calls_after_exhaustion(
    tokens: int, cap: int, exhausted: bool,
) -> None:
    gateway = ToolIncompleteGateway("incomplete", "tool", "tool" if exhausted else "success", "final")
    harness = RecordingHarnessToolGateway()
    repository = InMemoryArtifactRepository()
    runtime = subject(gateway, tool_plan(tokens), repository, harness)
    await collect(runtime, tokens=tokens, fail="budget exhausted" if exhausted else None)
    assert gateway.requests[1].max_output_tokens == tokens - 8378
    assert gateway.requests[2].max_output_tokens == cap
    assert len(gateway.requests) == (3 if exhausted else 4)
    assert len(harness.calls) == 1
    if exhausted:
        checkpoint = await runtime.save_checkpoint()
        assert mapping(checkpoint.state["usage"])["tokens"] == tokens
        assert checkpoint.state["phase"] == "running" and checkpoint.state["terminal"] is False
        replay_gateway = ToolIncompleteGateway("tool")
        replay_harness = RecordingHarnessToolGateway()
        replay = subject(replay_gateway, tool_plan(tokens), repository, replay_harness)
        await replay.restore_checkpoint(checkpoint)
        await collect(replay, checkpoint=checkpoint, tokens=tokens, fail="budget exhausted")
        assert replay_gateway.requests == [] and replay_harness.calls == []
        saved = await replay.save_checkpoint()
        assert saved.state["usage"] == checkpoint.state["usage"]
        assert saved.state["models"] == checkpoint.state["models"]
