"""Bounded, tool-free recovery of accounted incomplete model receipts."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Literal, cast

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models.failure_receipt import get_gateway_failure_receipt
from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    GatewayResponseCancelled,
    ModelGateway,
    ScopeIncompleteReason,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.litellm_client import (
    LiteLLMClient,
    ModelResponseError,
    ModelTransportError,
    OpenAIClientFactory,
)
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    ModelCapability,
    ModelRequest,
    ModelResponse,
    RejectedOutputEvidence,
    TokenUsage,
    ToolCall,
)
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.runtime.crew.plan import DispatchPlan
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    FastGeneration,
    RecordingHarnessToolGateway,
    _context,
)
from tests.unit.runtime.crew.test_structured_repair_output_budget import budget_plan, mapping

PRICE = Decimal("0.01")
SOURCE_USAGE = TokenUsage(2234, 6144, 8378)


def plan(*, tokens: int = 30_000) -> DispatchPlan:
    base = budget_plan(output_limit=6144, step_tokens=tokens, total_tokens=tokens)
    return base.model_copy(update={
        "agents": tuple(agent.model_copy(update={
            "fallback_models": ("backup", "last_backup"),
        }) if agent.id == base.steps[0].agent else agent for agent in base.agents),
        "steps": (base.steps[0].model_copy(update={"reviewer": None}), base.steps[1]),
    })


class IncompleteGateway:
    def __init__(self, *outcomes: str, usage_status: Literal["known", "missing", "invalid"] = "known",
                 source_cost: Decimal = PRICE) -> None:
        self.outcomes = outcomes or ("incomplete", "success", "final")
        self.usage_status = usage_status
        self.source_cost = source_cost
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        assert len(self.requests) <= len(self.outcomes), "unexpected paid call"
        outcome = self.outcomes[len(self.requests) - 1]
        cost = self.source_cost if len(self.requests) == 1 else PRICE
        if outcome == "incomplete":
            raise GatewayRejectedOutput(
                evidence=RejectedOutputEvidence(
                    final_text=None, usage=SOURCE_USAGE if self.usage_status == "known" else None,
                    usage_status=self.usage_status, status="incomplete", reason="incomplete",
                ),
                deployment_id="selected", logical_model="selected", provider_id="provider",
                provider_model="provider/model", cost_usd=cost,
            )
        if outcome == "transport":
            raise RuntimeExecutionError("model provider unavailable")
        response = ModelResponse(
            text="actual final answer" if outcome == "final" else (
                "invalid JSON" if outcome == "invalid" else '{"summary":"complete handoff"}'
            ),
            usage=TokenUsage(1, 10, 11) if outcome == "final" else TokenUsage(1, 16, 17),
        )
        if outcome == "tool":
            response = ModelResponse(text=None, usage=response.usage, tool_calls=(
                ToolCall(id="forbidden", name="workspace.write_text", arguments={}),
            ))
        return GatewayCompletion(
            response=response, deployment_id="selected", logical_model=request.logical_model,
            provider_id="provider", provider_model="provider/model", cost_usd=cost,
        )


def runtime(gateway: IncompleteGateway, dispatch: DispatchPlan | None = None,
            repository: InMemoryArtifactRepository | None = None) -> CrewDispatchRuntime:
    return CrewDispatchRuntime(
        gateway, dispatch or plan(), artifact_repository=repository or InMemoryArtifactRepository(),
        crew_factory=FastFactory(), harness_tool_gateway=RecordingHarnessToolGateway(),
    )


async def collect(subject: CrewDispatchRuntime, *, fail: str | None = None,
                  checkpoint: RuntimeCheckpoint | None = None,
                  tokens: int = 30_000) -> list[RunEvent]:
    events: list[RunEvent] = []
    if fail is None:
        async for event in subject.run(_context(token_budget=tokens, checkpoint=checkpoint)):
            events.append(event)
    else:
        with pytest.raises(RuntimeExecutionError, match=fail):
            async for event in subject.run(_context(token_budget=tokens, checkpoint=checkpoint)):
                events.append(event)
    return events


async def test_accounted_incomplete_uses_one_new_key_same_actor_schema_and_selected_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_deadline_extension(*args: object) -> float:
        raise AssertionError("incomplete recovery extended its original deadline")

    monkeypatch.setattr(CrewDispatchRuntime, "_recovery_step_deadline", no_deadline_extension)
    gateway = IncompleteGateway()
    subject = runtime(gateway)
    events = await collect(subject)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    original, recovered, _ = gateway.requests
    assert original.logical_model == "general"
    assert recovered.logical_model == "selected" and recovered.allow_fallback is False
    assert recovered.response_schema == original.response_schema
    assert recovered.tools == original.tools == ()
    assert recovered.max_output_tokens == 6144
    prompt = " ".join(str(message.content) for message in recovered.messages)
    assert "incomplete_response" in prompt and "Do not continue" in prompt
    assert "UNTRUSTED_REJECTED_OUTPUT" not in prompt
    retry = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retry.actor == "writer" and retry.payload["logical_model"] == "selected"
    assert retry.payload["compression_trigger"] == "incomplete_response"
    checkpoint = await subject.save_checkpoint()
    models = mapping(checkpoint.state["models"])
    draft = [(key, mapping(value)) for key, value in models.items()
             if mapping(value)["step_id"] == "draft"]
    assert len(draft) == 2 and draft[0][0] != draft[1][0]
    assert {state["attempt"] for _, state in draft} == {0, 1}
    assert {state["call_index"] for _, state in draft} == {0}
    assert {state["actor"] for _, state in draft} == {"writer"}
    assert {state["status"] for _, state in draft} == {"rejected", "succeeded"}
    assert next(state for _, state in draft if state["status"] == "rejected")["failure_reason"] == (
        "model response incomplete"
    )
    assert checkpoint.state["structured_repairs"] == {} and checkpoint.state["tools"] == {}
    assert mapping(checkpoint.state["usage"])["tokens"] == 8378 + 17 + 11
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.03"


@pytest.mark.parametrize("second,reason", [
    ("incomplete", "model response incomplete"),
    ("invalid", "structured output invalid"),
    ("tool", "step requested a forbidden capability"),
    ("transport", "model provider unavailable"),
])
async def test_recovery_failure_never_starts_a_third_paid_call(second: str, reason: str) -> None:
    gateway = IncompleteGateway("incomplete", second)
    subject = runtime(gateway)
    events = await collect(subject, fail=reason)
    assert len(gateway.requests) == 2
    assert sum(event.kind is EventKind.STEP_RETRYING for event in events) == 1
    checkpoint = await subject.save_checkpoint()
    assert checkpoint.state["tools"] == {} and checkpoint.state["structured_repairs"] == {}


@pytest.mark.parametrize("usage_status", ["missing", "invalid"])
async def test_unknown_usage_is_unaccounted_and_never_recovers(
    usage_status: Literal["missing", "invalid"],
) -> None:
    gateway = IncompleteGateway("incomplete", usage_status=usage_status)
    subject = runtime(gateway)
    await collect(subject, fail="usage|unaccounted")
    assert len(gateway.requests) == 1
    assert (await subject.save_checkpoint()).state["phase"] == "unaccounted"


@pytest.mark.parametrize("scope", ["step_tokens", "plan_tokens", "step_cost", "plan_cost"])
async def test_exhausted_budget_does_not_reserve_a_recovery(scope: str) -> None:
    dispatch = plan(tokens=8378 if scope == "plan_tokens" else 30_000)
    if scope == "step_tokens":
        dispatch = dispatch.model_copy(update={"steps": (
            dispatch.steps[0].model_copy(update={"token_budget": 8378}), dispatch.steps[1],
        )})
    if scope.endswith("cost"):
        dispatch = dispatch.model_copy(update={
            "total_cost_usd": PRICE if scope == "plan_cost" else Decimal(3),
            "steps": tuple(step.model_copy(update={
                "cost_budget_usd": PRICE if index == 0 and scope == "step_cost" else Decimal(0),
            }) for index, step in enumerate(dispatch.steps)),
        })
    gateway = IncompleteGateway("incomplete")
    subject = runtime(gateway, dispatch)
    await collect(subject, fail="budget exhausted", tokens=dispatch.total_token_budget)
    assert len(gateway.requests) == 1
    checkpoint = await subject.save_checkpoint()
    assert len(mapping(checkpoint.state["models"])) == 1
    assert mapping(checkpoint.state["usage"])["tokens"] == 8378


async def test_recovery_output_is_clamped_to_remaining_tokens() -> None:
    gateway = IncompleteGateway()
    subject = runtime(gateway, plan(tokens=9000))
    await collect(subject, tokens=9000)
    assert gateway.requests[1].max_output_tokens == 622


async def test_short_original_deadline_never_extends_for_incomplete() -> None:
    dispatch = plan()
    dispatch = dispatch.model_copy(update={"steps": (
        dispatch.steps[0].model_copy(update={"timeout_seconds": 1}), dispatch.steps[1],
    )})
    gateway = IncompleteGateway("incomplete")
    await collect(runtime(gateway, dispatch), fail="model response incomplete")
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("tokens", [30_000, 9000, 8406])
@pytest.mark.parametrize("phase", ["source", "prepared", "running", "succeeded"])
async def test_restore_resumes_only_safe_boundaries_without_double_accounting(
    phase: str, tokens: int,
) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = plan(tokens=tokens)
    source = runtime(IncompleteGateway(), dispatch, repository)
    events = await collect(source, tokens=tokens)
    def matches(checkpoint: RuntimeCheckpoint) -> bool:
        states = [mapping(value) for value in mapping(checkpoint.state["models"]).values()]
        draft = [value for value in states if value["step_id"] == "draft"]
        return checkpoint.state["phase"] == "running" and (
            len(draft) == 1 and draft[0]["status"] == "rejected" if phase == "source" else
            len(draft) == 2 and draft[-1]["status"] == phase
        ) and all(value["step_id"] == "draft" for value in states)

    checkpoint = next(event.checkpoint for event in events
                      if event.checkpoint is not None and matches(event.checkpoint))
    before = checkpoint.to_payload()
    gateway = IncompleteGateway(*(("final",) if phase == "succeeded" else ("success", "final")))
    restored = runtime(gateway, dispatch, repository)
    await restored.restore_checkpoint(checkpoint)
    if phase == "running":
        await collect(restored, checkpoint=checkpoint, tokens=tokens,
                      fail="model outcome requires confirmation")
        assert gateway.requests == []
        assert (await restored.save_checkpoint()).to_payload() == before
    else:
        await collect(restored, checkpoint=checkpoint, tokens=tokens)
        assert len(gateway.requests) == (1 if phase == "succeeded" else 2)
        saved = await restored.save_checkpoint()
        assert mapping(saved.state["usage"])["tokens"] == 8406
        assert mapping(saved.state["usage"])["cost_usd"] == "0.03"
        for key, value in mapping(checkpoint.state["models"]).items():
            if mapping(value)["status"] in {"rejected", "succeeded"}:
                assert mapping(saved.state["models"])[key] == value
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("phase", ["source", "prepared"])
async def test_restore_changed_hash_refuses_to_send_and_preserves_boundary(phase: str) -> None:
    repository = InMemoryArtifactRepository()
    source = runtime(IncompleteGateway(), repository=repository)
    events = await collect(source)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and any(mapping(value)["attempt"] == (0 if phase == "source" else 1)
                              and mapping(value)["status"] == ("rejected" if phase == "source" else "prepared")
                              for value in mapping(event.checkpoint.state["models"]).values()))
    payload = checkpoint.to_payload()
    models = mapping(payload["state"])["models"]
    for value in models.values():
        if value["attempt"] == (0 if phase == "source" else 1):
            value["request_sha256"] = "a" * 64
    payload["state_sha256"] = ""
    corrupted = RuntimeCheckpoint.from_payload(payload)
    gateway = IncompleteGateway("success")
    restored = runtime(gateway, repository=repository)
    await restored.restore_checkpoint(corrupted)
    await collect(restored, checkpoint=corrupted, fail="model request changed after checkpoint")
    assert gateway.requests == []
    assert (await restored.save_checkpoint()).to_payload() == corrupted.to_payload()


@pytest.mark.parametrize("second,reason", [
    ("incomplete", "model response incomplete"),
    ("transport", "model provider unavailable"),
])
async def test_restore_failed_compact_attempt_never_dispatches_or_double_charges(
    second: str, reason: str,
) -> None:
    repository = InMemoryArtifactRepository()
    source = runtime(IncompleteGateway("incomplete", second), repository=repository)
    await collect(source, fail=reason)
    checkpoint = await source.save_checkpoint()
    replay_gateway = IncompleteGateway("success", "final")
    replay = runtime(replay_gateway, repository=repository)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint,
                  fail="model response incomplete|model provider unavailable|model outcome requires confirmation")
    assert replay_gateway.requests == []
    saved = await replay.save_checkpoint()
    assert saved.state["usage"] == checkpoint.state["usage"]
    assert saved.state["models"] == checkpoint.state["models"]


@pytest.mark.parametrize("field,value", [
    ("failure_reason", "model response incomplete "),
    ("actor", "reviewer"),
    ("output_status", "completed"),
    ("reason", "invalid_output"),
])
async def test_restore_qualification_marker_revalidates_actor_and_receipt(
    field: str, value: str,
) -> None:
    repository = InMemoryArtifactRepository()
    source = runtime(IncompleteGateway(), repository=repository)
    events = await collect(source)
    checkpoint = next(event.checkpoint for event in events
                      if event.checkpoint is not None
                      and len(mapping(event.checkpoint.state["models"])) == 1
                      and next(iter(mapping(event.checkpoint.state["models"]).values()))["status"] == "rejected")
    payload = checkpoint.to_payload()
    section = "models" if field in {"failure_reason", "actor"} else "rejected_outputs"
    next(iter(mapping(mapping(payload["state"])[section]).values()))[field] = value
    payload["state_sha256"] = ""
    corrupted = RuntimeCheckpoint.from_payload(payload)
    replay_gateway = IncompleteGateway("success")
    replay = runtime(replay_gateway, repository=repository)
    with pytest.raises(RuntimeExecutionError, match="checkpoint.*(invalid|incompatible)"):
        await replay.restore_checkpoint(corrupted)
        await collect(replay, checkpoint=corrupted)
    assert replay_gateway.requests == []


async def test_restore_qualification_never_accepts_a_changed_schema() -> None:
    repository = InMemoryArtifactRepository()
    dispatch = plan()
    source = runtime(IncompleteGateway(), dispatch, repository)
    events = await collect(source)
    checkpoint = next(event.checkpoint for event in events
                      if event.checkpoint is not None
                      and len(mapping(event.checkpoint.state["models"])) == 1
                      and next(iter(mapping(event.checkpoint.state["models"]).values()))["status"] == "rejected")
    changed = dispatch.model_copy(update={"agents": (
        dispatch.agents[0].model_copy(update={"output_schema": {"other": "string"}}),
        *dispatch.agents[1:],
    )})
    replay_gateway = IncompleteGateway("success")
    replay = runtime(replay_gateway, changed, repository)
    with pytest.raises(RuntimeExecutionError, match="checkpoint.*incompatible"):
        await replay.restore_checkpoint(checkpoint)
        await collect(replay, checkpoint=checkpoint)
    assert replay_gateway.requests == []


@pytest.mark.parametrize("excluded", [
    "repair", "review", "schema", "step_tools", "request_tools", "missing", "invalid",
    "status", "reason", "evidence", "prepared_effect", "running_effect", "succeeded_effect",
])
def test_eligibility_excludes_corrections_review_tools_and_uncertain_evidence(excluded: str) -> None:
    dispatch = plan()
    step = dispatch.steps[0]
    evidence: RejectedOutputEvidence | None = RejectedOutputEvidence(
        final_text=None, usage=SOURCE_USAGE, usage_status="known",
        status="incomplete", reason="incomplete",
    )
    request = ModelRequest(
        logical_model="general", messages=(),
        response_schema=adapter._agent_response_schema(dispatch.agents[0]),
        required_capabilities=frozenset({ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT}),
    )
    tools = adapter._ToolLedger()
    repair: dict[str, JsonValue] | None = None
    purpose: Literal["step", "review"] = "step"
    if excluded == "repair":
        repair = {}
    elif excluded == "review":
        purpose = "review"
    elif excluded == "schema":
        request = replace(request, response_schema=None)
    elif excluded == "step_tools":
        step = step.model_copy(update={"tools": ("workspace.write_text",)})
    elif excluded == "request_tools":
        from agent_hub.models.types import ToolDefinition

        request = replace(request, tools=(ToolDefinition(
            name="workspace_write_text", description="write", parameters={"type": "object"},
        ),), required_capabilities=frozenset({
            ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING,
        }))
    elif excluded in {"missing", "invalid"}:
        assert evidence is not None
        evidence = replace(evidence, usage=None, usage_status=cast(Literal["missing", "invalid"], excluded))
    elif excluded == "status":
        assert evidence is not None
        evidence = replace(evidence, status="unknown")
    elif excluded == "reason":
        assert evidence is not None
        evidence = replace(evidence, reason="invalid_output")
    elif excluded == "evidence":
        evidence = None
    elif excluded.endswith("effect"):
        tools.states["effect"] = {"step_id": step.id, "status": excluded.split("_")[0]}
    assert not adapter._known_incomplete_recovery_eligible(
        step=step, request=request, purpose=purpose, repair=repair, evidence=evidence,
        tool_ledger=tools,
    )


async def test_cancelled_known_incomplete_is_accounted_but_never_recovered_or_replayed() -> None:
    class CancelledGateway(IncompleteGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as receipt:
                raise GatewayResponseCancelled(receipt=receipt) from None

    gateway = CancelledGateway("incomplete")
    source = runtime(gateway)
    with pytest.raises(asyncio.CancelledError):
        await collect(source)
    assert len(gateway.requests) == 1
    checkpoint = await source.save_checkpoint()
    assert mapping(checkpoint.state["usage"])["tokens"] == 8378
    assert {mapping(value)["status"] for value in mapping(checkpoint.state["models"]).values()} == {
        "received_cancelled",
    }
    replay_gateway = IncompleteGateway("success")
    replay = runtime(replay_gateway)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail="model outcome requires confirmation")
    assert replay_gateway.requests == []
    assert (await replay.save_checkpoint()).to_payload() == checkpoint.to_payload()


def test_plain_incomplete_diagnostic_never_authorizes_generic_or_failed_checkpoint_retry() -> None:
    assert not adapter._can_compact_retry_subagent(
        {"error_code": "model.incomplete_response", "retryable": True},
        recovery_attempt=0, remaining_seconds=60,
    )
    assert not adapter._failed_model_state_can_compact_retry(
        {"failure_reason": "model response incomplete", "attempt": 0}, recovery_limit=2,
    )


async def test_plain_incomplete_framework_message_never_calls_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_without_receipt(*args: object, **kwargs: object) -> str:
        raise RuntimeExecutionError("model response incomplete")

    monkeypatch.setattr(FastGeneration, "execute", fail_without_receipt)
    gateway = IncompleteGateway("success")
    subject = runtime(gateway)
    events = await collect(subject, fail="model response incomplete")
    assert gateway.requests == []
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)


class RealGatewayTransport:
    def __init__(self, prior_unknown: bool, status: int | None = None) -> None:
        self.prior_unknown = prior_unknown
        self.status = status
        self.requests: list[ModelRequest] = []

    async def complete(self, selected: object, request: ModelRequest,
                       api_key: str) -> ModelResponse:
        del selected, api_key
        self.requests.append(request)
        if self.prior_unknown and len(self.requests) == 1:
            raise ModelTransportError("private transport detail", status_code=self.status)
        if len(self.requests) == (2 if self.prior_unknown else 1):
            raise ModelResponseError("model response rejected", evidence=RejectedOutputEvidence(
                final_text=None, usage=SOURCE_USAGE, usage_status="known",
                status="incomplete", reason="incomplete",
            ))
        return ModelResponse(
            text='{"summary":"complete handoff"}' if request.response_schema is not None
            else "actual final answer", usage=TokenUsage(1, 16, 17),
        )


async def real_gateway(transport: RealGatewayTransport) -> ModelGateway:
    capabilities = frozenset({ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT})
    registry = ModelRegistry([
        deployment(identifier, logical_model=logical_model, capabilities=capabilities,
                   provider_model="provider/model", input_per_million_usd=Decimal(1),
                   output_per_million_usd=Decimal(2))
        for identifier, logical_model in (("primary", "general"), ("selected", "selected"))
    ])
    capacity = CapacityStub([
        lease(identifier) for identifier in (
            ("primary", "selected", "selected", "primary") if transport.prior_unknown
            else ("primary", "primary", "primary")
        )
    ])
    gateway = ModelGateway(registry, capacity, SecretStub([]), transport,
                           fallbacks={"general": "selected"})
    await capacity.initialize()
    return gateway


@pytest.mark.parametrize("status", [None, 408])
async def test_real_gateway_prior_unknown_then_known_incomplete_preserves_fee_and_blocks_restore(
    status: int | None,
) -> None:
    transport = RealGatewayTransport(True, status)
    repository = InMemoryArtifactRepository()
    subject = CrewDispatchRuntime(
        await real_gateway(transport), plan(), crew_factory=FastFactory(),
        artifact_repository=repository,
    )
    events = await collect(subject, fail="dispatch usage unaccounted")
    assert len(transport.requests) == 2
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    checkpoint = await subject.save_checkpoint()
    assert checkpoint.state["phase"] == "unaccounted"
    assert mapping(checkpoint.state["usage"])["tokens"] == 8378
    assert Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) == Decimal("0.014522")
    assert len(mapping(checkpoint.state["models"])) == 1
    state = mapping(next(iter(mapping(checkpoint.state["models"]).values())))
    assert state["status"] == "rejected" and state["failure_reason"] == "structured output rejected"
    assert "private transport detail" not in str(checkpoint.to_payload())
    before = checkpoint.to_payload()
    replay = CrewDispatchRuntime(
        await real_gateway(transport), plan(), crew_factory=FastFactory(),
        artifact_repository=repository,
    )
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail="dispatch usage unaccounted")
    assert len(transport.requests) == 2
    assert (await replay.save_checkpoint()).to_payload() == before


async def test_real_gateway_single_known_incomplete_can_compact_once() -> None:
    transport = RealGatewayTransport(False)
    subject = CrewDispatchRuntime(await real_gateway(transport), plan(), crew_factory=FastFactory())
    events = await collect(subject)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(transport.requests) == 3
    original, recovered, _ = transport.requests
    assert recovered.logical_model == original.logical_model == "general"
    assert recovered.allow_fallback is False and recovered.response_schema == original.response_schema


@pytest.fixture
def baseline_runtime_type(monkeypatch: pytest.MonkeyPatch) -> type[CrewDispatchRuntime]:
    # Run the original adapter in memory so legacy receipts are not hand-crafted.
    source = subprocess.check_output([
        "git", "show", "5dd47655cfc1:src/agent_hub/runtime/crew/adapter.py",
    ], cwd=Path(__file__).resolve().parents[4], encoding="utf-8")
    module = ModuleType("agent_hub.runtime.crew._baseline_known_incomplete")
    module.__package__ = "agent_hub.runtime.crew"
    module.__file__ = adapter.__file__
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "<baseline-5dd-adapter>", "exec"), module.__dict__)  # noqa: S102 - pinned local baseline
    return cast(type[CrewDispatchRuntime], module.CrewDispatchRuntime)


@pytest.mark.parametrize("prior_unknown", [False, True])
async def test_baseline_natural_receipt_without_qualification_never_dispatches_on_restore(
    baseline_runtime_type: type[CrewDispatchRuntime], prior_unknown: bool,
) -> None:
    transport = RealGatewayTransport(prior_unknown, 408)
    repository = InMemoryArtifactRepository()
    baseline = baseline_runtime_type(
        await real_gateway(transport), plan(), crew_factory=FastFactory(),
        artifact_repository=repository,
    )
    checkpoints: list[RuntimeCheckpoint] = []
    baseline_error = cast(type[Exception], sys.modules[baseline_runtime_type.__module__].RuntimeExecutionError)
    with pytest.raises(baseline_error, match="^structured output invalid$"):
        async for event in baseline.run(_context(token_budget=30_000)):
            if event.checkpoint is not None:
                checkpoints.append(event.checkpoint)
    checkpoint = next(candidate for candidate in checkpoints
                      if candidate.state["phase"] == "running"
                      and any(mapping(value)["status"] == "rejected"
                              for value in mapping(candidate.state["models"]).values()))
    state = mapping(next(iter(mapping(checkpoint.state["models"]).values())))
    private = mapping(next(iter(mapping(checkpoint.state["rejected_outputs"]).values())))
    assert checkpoint.runtime_version == "11" and checkpoint.state["terminal"] is False
    assert state["failure_reason"] == "structured output rejected"
    assert private["fallback_used"] is prior_unknown
    assert private["usage_status"] == "known" and private["reason"] == "incomplete"
    assert mapping(checkpoint.state["usage"])["tokens"] == 8378
    assert Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) == Decimal("0.014522")
    assert len(transport.requests) == (2 if prior_unknown else 1)
    replay_gateway = IncompleteGateway("success", "final")
    replay = runtime(replay_gateway, repository=repository)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail="structured output invalid")
    assert replay_gateway.requests == []
    saved = await replay.save_checkpoint()
    assert saved.state["usage"] == checkpoint.state["usage"]
    assert saved.state["models"] == checkpoint.state["models"]
    assert saved.state["rejected_outputs"] == checkpoint.state["rejected_outputs"]


async def test_compact_guidance_treats_sizes_as_preferences_subordinate_to_schema() -> None:
    gateway = IncompleteGateway()
    await collect(runtime(gateway))
    original, recovered, _ = gateway.requests
    prompt = " ".join(str(message.content) for message in recovered.messages)
    assert recovered.response_schema == original.response_schema
    assert recovered.max_output_tokens == original.max_output_tokens
    assert "Prefer concise strings" in prompt and "short arrays" in prompt
    assert "only when compatible with the unchanged schema and required facts" in prompt
    assert "Schema minima and mandatory entries take priority" in prompt
    assert "Never mechanically truncate" in prompt


@pytest.mark.parametrize("prior", ["none", "timeout", "408"])
async def test_sdk_mock_native_known_incomplete_preserves_primary_or_blocks_unknown_history(
    prior: str,
) -> None:
    wire: list[dict[str, object]] = []
    requests: list[ModelRequest] = []
    rejections: list[GatewayRejectedOutput] = []
    clients: list[AsyncOpenAI] = []
    unknown = prior != "none"

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1/responses"
        body = json.loads(request.content)
        wire.append(body)
        if unknown and len(wire) == 1:
            if prior == "timeout":
                raise httpx.ReadTimeout("PRIVATE_SCOPE_GAP", request=request)
            return httpx.Response(408, json={"error": {
                "message": "PRIVATE_SCOPE_GAP", "type": "temporary", "code": "timeout",
            }})
        incomplete = len(wire) == (2 if unknown else 1)
        assert len(wire) <= 2, "unexpected provider call"
        text = ' {"summary":"PRIVATE_TRUNCATED' if incomplete else '{"summary":"compact result"}'
        return httpx.Response(200, json={
            "id": "resp-own", "object": "response", "created_at": 1,
            "model": "native-model", "status": "incomplete" if incomplete else "completed",
            "error": None,
            "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
            "output": [{
                "id": "msg-own", "type": "message", "role": "assistant",
                "status": "incomplete" if incomplete else "completed",
                "content": [{"type": "output_text", "annotations": [], "text": text}],
            }],
            "usage": {"input_tokens": 2234, "output_tokens": 6144, "total_tokens": 8378}
            if incomplete else {"input_tokens": 1, "output_tokens": 16, "total_tokens": 17},
        })

    def factory(*, api_key: str, base_url: str, max_retries: int) -> AsyncOpenAI:
        assert max_retries == 0
        client = AsyncOpenAI(
            api_key=api_key, base_url=base_url, max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        clients.append(client)
        return client

    class RecordingModelGateway(ModelGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            requests.append(request)
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                rejections.append(error)
                raise

    registry = ModelRegistry([
        deployment(identifier, logical_model=logical_model,
                   capabilities=frozenset({ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT}),
                   structured_output_api="responses", request_model="native-model",
                   api_base="https://own-native.example/v1", input_per_million_usd=Decimal(1),
                   output_per_million_usd=Decimal(2))
        for identifier, logical_model in (("primary", "general"), ("selected", "selected"))
    ])
    capacity = CapacityStub([lease("primary"), lease("selected" if unknown else "primary")])
    gateway = RecordingModelGateway(
        registry, capacity, SecretStub([]),
        LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)),
        fallbacks={"general": "selected"},
    )
    await capacity.initialize()
    base = plan()
    dispatch = base.model_copy(update={
        "agents": (base.agents[0],),
        "steps": (base.steps[0].model_copy(update={"id": "final", "final_synthesizer": True}),),
    })
    repository = InMemoryArtifactRepository()
    harness = RecordingHarnessToolGateway()
    subject = CrewDispatchRuntime(
        gateway, dispatch, crew_factory=FastFactory(), artifact_repository=repository,
        harness_tool_gateway=harness,
    )
    try:
        events = await collect(subject, fail="dispatch usage unaccounted" if unknown else None)
        assert len(wire) == 2 and len(rejections) == 1 and harness.calls == []
        scope = get_gateway_scope_diagnostic(rejections[0])
        assert scope is not None
        assert scope.transport_entered_count == (2 if unknown else 1)
        assert scope.failure_attempt_count == (1 if unknown else 0)
        assert (get_gateway_failure_receipt(rejections[0]) is not None) is unknown
        checkpoint = await subject.save_checkpoint()
        assert checkpoint.state["structured_repairs"] == {} and checkpoint.state["tools"] == {}
        assert "PRIVATE_TRUNCATED" not in str(checkpoint.to_payload())
        if unknown:
            assert checkpoint.state["phase"] == "unaccounted"
            assert mapping(checkpoint.state["usage"])["tokens"] == 8378
            assert Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) == Decimal("0.014522")
            replay = CrewDispatchRuntime(
                gateway, dispatch, crew_factory=FastFactory(), artifact_repository=repository,
            )
            await replay.restore_checkpoint(checkpoint)
            await collect(replay, checkpoint=checkpoint, fail="dispatch usage unaccounted")
            assert len(wire) == 2 and len(requests) == 1
            assert (await replay.save_checkpoint()).to_payload() == checkpoint.to_payload()
        else:
            assert scope.reason is ScopeIncompleteReason.REJECTED_OUTPUT
            assert events[-1].kind is EventKind.RUNTIME_COMPLETED
            assert len(requests) == 2
            original, recovered = requests
            assert recovered.logical_model == original.logical_model == "general"
            assert recovered.response_schema == original.response_schema
            assert recovered.allow_fallback is False and recovered.tools == ()
            assert recovered.max_output_tokens == original.max_output_tokens == 6144
            assert wire[0]["text"] == wire[1]["text"]
            assert "PRIVATE_TRUNCATED" not in str(recovered.messages)
            assert mapping(checkpoint.state["usage"])["tokens"] == 8395
            assert Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) == Decimal("0.014555")
    finally:
        for client in clients:
            await client.close()
