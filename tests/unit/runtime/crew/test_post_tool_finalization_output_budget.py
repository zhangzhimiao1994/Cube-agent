"""Bounded output growth inside the existing single post-tool finalization."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput, ModelGateway
from agent_hub.models.litellm_client import ModelResponseError
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    RejectedOutputEvidence,
    TokenUsage,
)
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    RecordingHarnessToolGateway,
    _context,
)
from tests.unit.runtime.crew.test_known_incomplete_recovery import collect
from tests.unit.runtime.crew.test_post_tool_incomplete_finalization import PostToolGateway
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_recovery import draft_models, subject, tool_plan


class SaturatedGateway(PostToolGateway):
    _registry: ModelRegistry

    def __init__(self, *outcomes: str, completion_tokens: int = 6144, **kwargs: Any) -> None:
        super().__init__(*outcomes, **kwargs)
        self.completion_tokens = completion_tokens
        self.fallback = False
        self.free_final = False

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        try:
            completion = await super().complete_with_context(request)
        except GatewayRejectedOutput as rejected:
            assert rejected.evidence is not None
            if rejected.evidence.usage_status == "known":
                rejected.evidence = replace(rejected.evidence, usage=TokenUsage(
                    2234, self.completion_tokens, 2234 + self.completion_tokens,
                ))
            if self.fallback:
                rejected.fallback_used = True
                rejected.fallback_from_logical_model = request.logical_model
                rejected.fallback_reason = "transport_retryable"
            raise
        if self.free_final and self.outcomes[len(self.requests) - 1] == "final":
            return replace(completion, cost_usd=Decimal(0))
        return completion


@pytest.mark.parametrize("completion,expected", [
    (6144, 12288), (6083, 12288), (6082, 6144), (16, 6144),
])
async def test_known_saturation_grows_only_the_existing_finalization(
    completion: int, expected: int,
) -> None:
    gateway = SaturatedGateway("tool", "incomplete", "success", "final",
                               completion_tokens=completion)
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    events = await collect(runtime)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4 and len(harness.calls) == 1
    source, correction = gateway.requests[1:3]
    assert source.max_output_tokens == 6144
    assert correction.max_output_tokens == expected
    assert correction.logical_model == source.logical_model
    assert correction.response_schema == source.response_schema
    assert correction.tools == () and not correction.allow_fallback
    assert ModelCapability.TOOL_CALLING not in correction.required_capabilities
    assert correction.timeout_seconds <= source.timeout_seconds
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    saved = await runtime.save_checkpoint()
    repair = mapping(mapping(saved.state["structured_repairs"])["draft"])
    assert repair["max_output_tokens"] == expected and repair["version"] == 2
    assert repair["correction_request_sha256"] == runtime._model_request_sha256(correction)
    assert mapping(saved.state["usage"])["tokens"] == 2234 + completion + 45
    assert mapping(saved.state["usage"])["cost_usd"] == "0.04"


@pytest.mark.parametrize("scope", ["step", "plan", "context"])
@pytest.mark.parametrize("remaining", [9605, 605])
async def test_growth_is_capped_by_each_remaining_token_envelope(
    scope: str, remaining: int,
) -> None:
    tokens = 8395 + remaining
    dispatch = tool_plan(tokens if scope == "plan" else 30000)
    if scope == "step":
        dispatch = dispatch.model_copy(update={"steps": (
            dispatch.steps[0].model_copy(update={"token_budget": tokens}), dispatch.steps[1],
        )})
    context = _context(token_budget=tokens if scope == "context" else 30000,
                       routing_decision={"runtime_plan_token_budget": dispatch.total_token_budget})
    gateway = SaturatedGateway("tool", "incomplete", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert gateway.requests[2].max_output_tokens == min(12288, remaining)
    assert len(gateway.requests) == 4 and len(harness.calls) == 1


@pytest.mark.parametrize("boundary", ["missing", "invalid", "unknown_scope", "no_scope", "fallback"])
async def test_unqualified_receipts_cannot_authorize_output_growth(boundary: str) -> None:
    gateway = SaturatedGateway("tool", "incomplete",
                               usage_status=boundary if boundary in {"missing", "invalid"} else "known",
                               scope_entries=2 if boundary == "unknown_scope" else 1,
                               trusted_scope=boundary != "no_scope")
    gateway.fallback = boundary == "fallback"
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="structured output invalid|usage unaccounted")
    assert len(gateway.requests) == 2 and len(harness.calls) == 1
    assert (await runtime.save_checkpoint()).state["structured_repairs"] == {}


@pytest.mark.parametrize("scope", ["step", "plan", "tokens"])
async def test_exhausted_budget_never_submits_the_finalization(scope: str) -> None:
    dispatch = tool_plan(8395 if scope == "tokens" else 30000)
    if scope == "step":
        dispatch = dispatch.model_copy(update={"steps": (
            dispatch.steps[0].model_copy(update={"cost_budget_usd": Decimal("0.02")}),
            dispatch.steps[1],
        )})
    elif scope == "plan":
        dispatch = dispatch.model_copy(update={
            "total_cost_usd": Decimal("0.02"), "steps": (
                dispatch.steps[0].model_copy(update={"cost_budget_usd": Decimal("0.02")}),
                dispatch.steps[1].model_copy(update={"cost_budget_usd": Decimal(0)}),
            ),
        })
    gateway = SaturatedGateway("tool", "incomplete")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    await collect(runtime, tokens=dispatch.total_token_budget, fail="budget exhausted")
    assert len(gateway.requests) == 2 and len(harness.calls) == 1
    assert (await runtime.save_checkpoint()).state["structured_repairs"] == {}


@pytest.mark.parametrize("last", ["incomplete", "invalid", "tool", "transport"])
async def test_failed_grown_finalization_has_no_third_call_or_tool_replay(last: str) -> None:
    gateway = SaturatedGateway("tool", "incomplete", last)
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="structured|provider unavailable|forbidden")
    assert len(gateway.requests) == 3 and len(harness.calls) == 1
    assert gateway.requests[2].max_output_tokens == 12288 and gateway.requests[2].tools == ()
    checkpoint = await runtime.save_checkpoint()
    assert all(row["attempt"] == 0 for row in draft_models(checkpoint))
    assert mapping(checkpoint.state["usage"])["cost_usd"] == (
        "0.02" if last == "transport" else "0.03"
    )


def _phase_checkpoint(events: list[RunEvent], phase: str) -> RuntimeCheckpoint:
    return next(event.checkpoint for event in events if event.checkpoint is not None
                and event.checkpoint.state["phase"] == "running"
                and len(draft_models(event.checkpoint)) == (2 if phase == "source" else 3)
                and draft_models(event.checkpoint)[-1]["status"] == (
                    "rejected" if phase == "source" else phase
                ))


@pytest.mark.parametrize("phase", ["source", "prepared", "running", "succeeded", "rejected"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_natural_checkpoint_reuses_reserved_cap_hash_and_accounting(
    phase: str, legacy: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan()
    original_gateway = SaturatedGateway("tool", "incomplete",
                                        "incomplete" if phase == "rejected" else "success", "final")
    original = subject(original_gateway, dispatch, repository, RecordingHarnessToolGateway())
    with monkeypatch.context() as old:
        if legacy:
            old.setattr(adapter, "_post_tool_finalization_output_limit",
                        lambda request, evidence, remaining: min(request.max_output_tokens, remaining),
                        raising=False)
        events = await collect(original, fail="structured output invalid" if phase == "rejected" else None)
    checkpoint = _phase_checkpoint(events, phase)
    before = checkpoint.to_payload()
    outcomes = () if phase in {"running", "rejected"} else (
        ("final",) if phase == "succeeded" else ("success", "final")
    )
    for _ in range(2):
        gateway = SaturatedGateway(*outcomes)
        harness = RecordingHarnessToolGateway()
        restored = subject(gateway, dispatch, repository, harness)
        await restored.restore_checkpoint(checkpoint)
        await collect(restored, checkpoint=checkpoint, fail=(
            "outcome requires confirmation" if phase == "running" else
            "structured output invalid" if phase == "rejected" else None
        ))
        assert len(gateway.requests) == len(outcomes) and harness.calls == []
        assert checkpoint.to_payload() == before
        if phase in {"source", "prepared"}:
            expected = 6144 if legacy and phase == "prepared" else 12288
            assert gateway.requests[0].max_output_tokens == expected
            if phase == "prepared":
                repair = mapping(mapping(checkpoint.state["structured_repairs"])["draft"])
                assert restored._model_request_sha256(gateway.requests[0]) == repair["correction_request_sha256"]
        saved = await restored.save_checkpoint()
        if phase in {"running", "rejected"}:
            assert mapping(saved.state["usage"]) == mapping(checkpoint.state["usage"])
        else:
            assert mapping(saved.state["usage"])["tokens"] == 8423
            assert mapping(saved.state["usage"])["cost_usd"] == "0.04"
        for key, row in mapping(checkpoint.state["models"]).items():
            if mapping(row)["status"] in {"succeeded", "rejected"}:
                assert mapping(saved.state["models"])[key] == row


@pytest.mark.parametrize("phase", ["source", "prepared"])
async def test_same_id_different_digest_cannot_replace_recovery_deadline(
    phase: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan()
    original = subject(SaturatedGateway("tool", "incomplete", "success", "final"),
                       dispatch, repository, RecordingHarnessToolGateway())
    payload: dict[str, Any] = _phase_checkpoint(await collect(original), phase).to_payload()
    payload["state"]["remaining_timeout_seconds"] = 60.0
    payload["state"]["remaining_absolute_timeout_seconds"] = 0.0
    payload["state_sha256"] = ""
    validated = RuntimeCheckpoint.from_payload(payload)
    context_payload: dict[str, Any] = validated.to_payload()
    context_payload["state"]["remaining_absolute_timeout_seconds"] = 60.0
    context_payload["state_sha256"] = ""
    substituted = RuntimeCheckpoint.from_payload(context_payload)
    assert substituted.id == validated.id and substituted.state_sha256 != validated.state_sha256
    before = validated.to_payload()
    artifacts_before = dict(repository._artifacts)
    reads: list[tuple[ArtifactReference, ...]] = []
    get_many = repository.get_many

    async def recorded_get_many(
        tenant_id: UUID, run_id: UUID, references: tuple[ArtifactReference, ...],
    ) -> tuple[Artifact, ...]:
        reads.append(references)
        return await get_many(tenant_id, run_id, references)

    monkeypatch.setattr(repository, "get_many", recorded_get_many)
    gateway = SaturatedGateway()
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, harness)
    events = []
    with pytest.raises(RuntimeExecutionError, match="runtime checkpoint mismatch"):
        await restored.restore_checkpoint(validated)
        async for event in restored.run(_context(token_budget=30000, checkpoint=substituted)):
            events.append(event)
    assert reads == [] and gateway.requests == [] and harness.calls == []
    assert all(event.kind is not EventKind.STEP_RETRYING for event in events)
    assert (await restored.save_checkpoint()).state == validated.state
    assert repository._artifacts == artifacts_before
    assert validated.to_payload() == before
    assert substituted.to_payload() == context_payload | {"state_sha256": substituted.state_sha256}


@pytest.mark.parametrize("field", [
    "source_request_sha256", "source_receipt_sha256", "tool_history_sha256",
    "correction_request_sha256", "max_output_tokens", "expired", "insufficient_remaining",
])
async def test_prepared_reservation_tamper_or_expiry_never_replays(field: str) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan()
    original = subject(SaturatedGateway("tool", "incomplete", "success", "final"),
                       dispatch, repository, RecordingHarnessToolGateway())
    checkpoint = _phase_checkpoint(await collect(original), "prepared")
    payload: dict[str, Any] = checkpoint.to_payload()
    repair = payload["state"]["structured_repairs"]["draft"]
    if field == "expired":
        payload["state"]["remaining_timeout_seconds"] = 0.0
        payload["state"]["remaining_absolute_timeout_seconds"] = 0.0
    elif field == "max_output_tokens":
        repair[field] += 1
    elif field != "insufficient_remaining":
        repair[field] = "0" * 64
    payload["state_sha256"] = ""
    altered = RuntimeCheckpoint.from_payload(payload)
    gateway = SaturatedGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, harness)
    context = _context(token_budget=9000 if field == "insufficient_remaining" else 30000,
                       checkpoint=altered,
                       routing_decision={"runtime_plan_token_budget": dispatch.total_token_budget})
    expected_failure = (
        "post-tool finalization deadline exhausted" if field == "expired"
        else "checkpoint|deadline|timeout"
    )
    with pytest.raises(RuntimeExecutionError, match=expected_failure):
        await restored.restore_checkpoint(altered)
        _ = [event async for event in restored.run(context)]
    assert gateway.requests == [] and harness.calls == []


@pytest.mark.parametrize("phase", ["succeeded", "rejected"])
async def test_cached_receipts_at_cost_limit_are_read_back_without_rebilling(phase: str) -> None:
    repository = InMemoryArtifactRepository()
    base = tool_plan()
    dispatch = base.model_copy(update={"total_cost_usd": Decimal("0.03"), "steps": (
        base.steps[0].model_copy(update={"cost_budget_usd": Decimal("0.03")}),
        base.steps[1].model_copy(update={"cost_budget_usd": Decimal(0)}),
    )})
    gateway = SaturatedGateway("tool", "incomplete",
                               "incomplete" if phase == "rejected" else "success", "final")
    gateway.free_final = True
    original = subject(gateway, dispatch, repository, RecordingHarnessToolGateway())
    events = await collect(original, fail="structured output invalid" if phase == "rejected" else None)
    checkpoint = _phase_checkpoint(events, phase)
    # The step is exactly exhausted at the cached receipt; no further model may be purchased.
    assert mapping(checkpoint.state["step_usage"])["draft"]["cost_usd"] == "0.03"
    gateway = SaturatedGateway("final")
    gateway.free_final = True
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, harness)
    await restored.restore_checkpoint(checkpoint)
    await collect(restored, checkpoint=checkpoint,
                  fail="structured output invalid" if phase == "rejected" else None)
    assert len(gateway.requests) == int(phase == "succeeded") and harness.calls == []
    saved = await restored.save_checkpoint()
    assert mapping(saved.state["usage"])["cost_usd"] == "0.03"
    assert mapping(saved.state["usage"])["tokens"] == (16773 if phase == "rejected" else 8423)


@pytest.mark.parametrize("bound,expected", [
    ("provider", 10000), ("context", 8500), ("input_overflow", None),
])
async def test_existing_gateway_caps_the_grown_request_before_transport(
    bound: str, expected: int | None,
) -> None:
    source_cap = 6144 if bound == "provider" else 4500

    class Transport:
        def __init__(self) -> None:
            self.gateway = SaturatedGateway("tool", "incomplete", "success", "final",
                                            completion_tokens=source_cap)

        async def complete(self, selected: Any, request: ModelRequest, api_key: str) -> Any:
            try:
                return (await self.gateway.complete_with_context(request)).response
            except GatewayRejectedOutput as rejected:
                raise ModelResponseError("model response rejected", evidence=rejected.evidence) from None

    class Estimator:
        def __init__(self) -> None:
            self.finalization_inputs: list[int] = []

        def estimate_input(self, request: ModelRequest) -> int:
            added_instruction = any("POST_TOOL_FINALIZATION" in str(message.content)
                                    for message in request.messages)
            tokens = 1000 + (10000 if bound == "input_overflow" else 500) * added_instruction
            if added_instruction:
                self.finalization_inputs.append(tokens)
            return tokens

        def estimate(self, request: ModelRequest) -> int:
            return self.estimate_input(request) + request.max_output_tokens

    capabilities = frozenset({ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT,
                              ModelCapability.TOOL_CALLING})
    registry = ModelRegistry([deployment(
        "selected", logical_model="general", capabilities=capabilities,
        max_output_tokens=10000 if bound == "provider" else None,
        context_window_tokens=10000 if bound != "provider" else None,
    )])
    capacity = CapacityStub([lease("selected") for _ in range(4)])
    await capacity.initialize()
    transport = Transport()
    estimator = Estimator()
    gateway = ModelGateway(registry, capacity, SecretStub([]), transport, token_estimator=estimator)
    harness = RecordingHarnessToolGateway()
    base = tool_plan()
    dispatch = base.model_copy(update={"agents": tuple(
        agent.model_copy(update={"max_output_tokens": source_cap}) for agent in base.agents
    )})
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="structured correction failed" if expected is None else None)
    assert len(transport.gateway.requests) == (2 if expected is None else 4)
    assert len(harness.calls) == 1
    if expected is not None:
        finalization = transport.gateway.requests[2]
        assert finalization.max_output_tokens == expected
        if bound == "context":
            assert estimator.finalization_inputs
            assert finalization.max_output_tokens + max(estimator.finalization_inputs) == 10000
    else:
        assert estimator.finalization_inputs and max(estimator.finalization_inputs) > 10000
    repair = mapping(mapping((await runtime.save_checkpoint()).state["structured_repairs"])["draft"])
    assert repair["max_output_tokens"] == source_cap * 2


async def test_grown_finalization_never_extends_original_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid_extension(*args: object) -> float:
        raise AssertionError("post-tool finalization extended the original deadline")

    monkeypatch.setattr(CrewDispatchRuntime, "_recovery_step_deadline", forbid_extension)
    gateway = SaturatedGateway("tool", "incomplete", "success", "final")
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await collect(runtime)
    assert gateway.requests[2].max_output_tokens == 12288
    assert gateway.requests[2].timeout_seconds <= gateway.requests[1].timeout_seconds


@pytest.mark.parametrize("cap,used,remaining,expected", [
    (128, 127, 30000, 256), (128, 126, 30000, 128),
    (600000, 600000, 1500000, 1000000), (1000000, 1000000, 900000, 900000),
])
def test_integer_threshold_and_existing_protocol_ceiling(
    cap: int, used: int, remaining: int, expected: int,
) -> None:
    request = ModelRequest(logical_model="general", messages=(ModelMessage(role="user", content="task"),),
                           max_output_tokens=cap)
    receipt = RejectedOutputEvidence(final_text=None, status="incomplete", reason="incomplete",
                                     usage_status="known", usage=TokenUsage(1, used, used + 1))
    assert adapter._post_tool_finalization_output_limit(request, receipt, remaining) == expected


@pytest.mark.parametrize("expired_at", ["before_prepared", "after_prepared"])
async def test_post_tool_deadline_stops_before_running_boundary(
    expired_at: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = CrewDispatchRuntime._execute_model_request
    snapshots: list[RuntimeCheckpoint] = []
    transitions: list[str] = []

    async def expire_at_boundary(
        self: CrewDispatchRuntime, context: Any, step: Any, actor: str,
        request: ModelRequest, **kwargs: Any,
    ) -> Any:
        if (kwargs.get("repair") or {}).get("mode") == "post_tool_finalization":
            boundary = kwargs["model_boundary"]
            state = kwargs["run_state"]

            async def recorded_boundary(key: str, row: Any, **extra: Any) -> None:
                transitions.append(row["status"])
                await boundary(key, row, **extra)
                if row["status"] == "prepared" and expired_at == "after_prepared":
                    snapshots.append(await self.save_checkpoint())
                    state.deadline = asyncio.get_running_loop().time() - 1

            kwargs["model_boundary"] = recorded_boundary
            if expired_at == "before_prepared":
                snapshots.append(await self.save_checkpoint())
                state.deadline = asyncio.get_running_loop().time() - 1
        return await execute(self, context, step, actor, request, **kwargs)

    monkeypatch.setattr(CrewDispatchRuntime, "_execute_model_request", expire_at_boundary)
    gateway = SaturatedGateway("tool", "incomplete", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    events = []
    with pytest.raises(RuntimeExecutionError) as failure:
        async for event in runtime.run(_context(token_budget=30000)):
            events.append(event)
    assert transitions == ([] if expired_at == "before_prepared" else ["prepared"])
    assert len(gateway.requests) == 2 and len(harness.calls) == 1
    assert all(event.kind is not EventKind.STEP_RETRYING for event in events)
    saved = await runtime.save_checkpoint()
    for field in ("models", "rejected_outputs", "structured_repairs", "usage", "step_usage",
                  "tools", "artifact_registry"):
        assert saved.state[field] == snapshots[0].state[field], field
    assert isinstance(failure.value, adapter._ModelContractFailed)
    assert str(failure.value) == "post-tool finalization deadline exhausted"
    assert adapter._registered_gateway_scope_diagnostic(failure.value) is None


@pytest.mark.parametrize("phase", ["succeeded", "rejected"])
async def test_cached_finalization_reads_receipt_after_deadline_without_rebilling(
    phase: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan()
    original = subject(SaturatedGateway("tool", "incomplete",
                                        "incomplete" if phase == "rejected" else "success", "final"),
                       dispatch, repository, RecordingHarnessToolGateway())
    checkpoint = _phase_checkpoint(await collect(
        original, fail="structured output invalid" if phase == "rejected" else None,
    ), phase)
    finalize = CrewDispatchRuntime._finalize_post_tool_incomplete
    inspected: list[str] = []

    async def expire_only_at_receipt_read(
        self: CrewDispatchRuntime, context: Any, step: Any, actor: str,
        request: ModelRequest, rejected: GatewayRejectedOutput, **kwargs: Any,
    ) -> Any:
        ledger = kwargs["ledger"]
        previous = ledger.structured_repairs[step.id]
        assert ledger.states[previous["correction_key"]]["status"] == phase
        inspected.append(phase)
        state = kwargs["run_state"]
        deadline = state.deadline
        state.deadline = asyncio.get_running_loop().time() - 1
        try:
            return await finalize(self, context, step, actor, request, rejected, **kwargs)
        finally:
            state.deadline = deadline

    monkeypatch.setattr(CrewDispatchRuntime, "_finalize_post_tool_incomplete", expire_only_at_receipt_read)
    gateway = SaturatedGateway(*(("final",) if phase == "succeeded" else ()))
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, harness)
    await restored.restore_checkpoint(checkpoint)
    await collect(restored, checkpoint=checkpoint,
                  fail="structured output invalid" if phase == "rejected" else None)
    assert inspected == [phase]
    assert len(gateway.requests) == int(phase == "succeeded") and harness.calls == []
    saved = await restored.save_checkpoint()
    assert saved.state["structured_repairs"] == checkpoint.state["structured_repairs"]
    assert saved.state["rejected_outputs"] == checkpoint.state["rejected_outputs"]
    for key, row in mapping(checkpoint.state["models"]).items():
        assert mapping(saved.state["models"])[key] == row
    assert mapping(saved.state["step_usage"])["draft"] == mapping(checkpoint.state["step_usage"])["draft"]
    if phase == "rejected":
        assert saved.state["usage"] == checkpoint.state["usage"]
    else:
        assert mapping(saved.state["usage"])["tokens"] == mapping(checkpoint.state["usage"])["tokens"] + 11
        assert Decimal(mapping(saved.state["usage"])["cost_usd"]) == (
            Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) + Decimal("0.01")
        )


def _routing_window_fixture(monkeypatch: pytest.MonkeyPatch, prompt_bytes: int = 10000) -> Any:
    execute = CrewDispatchRuntime._execute_model_request

    async def with_source_facts(
        self: CrewDispatchRuntime, context: Any, step: Any, actor: str,
        request: ModelRequest, **kwargs: Any,
    ) -> Any:
        if step.id == "draft" and kwargs["cursor"].value == 1 and kwargs.get("repair") is None:
            request = replace(request, messages=(ModelMessage(
                role="user", content="REQUIRED_FACTS=" + "x" * (prompt_bytes - 15),
            ),))
        return await execute(self, context, step, actor, request, **kwargs)

    monkeypatch.setattr(CrewDispatchRuntime, "_execute_model_request", with_source_facts)
    base = tool_plan()
    return base.model_copy(update={"agents": tuple(
        agent.model_copy(update={"max_output_tokens": 1024}) for agent in base.agents
    )})


def _profile(gateway: SaturatedGateway, window: int) -> None:
    gateway._registry = ModelRegistry([deployment(
        "selected", logical_model="general", context_window_tokens=window,
    )])


@pytest.mark.parametrize("prompt_bytes", [10000, 8100])
async def test_routing_window_rejects_full_grown_prompt_before_gateway(
    prompt_bytes: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _routing_window_fixture(monkeypatch, prompt_bytes)
    context = _context(routing_decision={"context_window_tokens": 4096}, token_budget=30000)
    gateway = SaturatedGateway("tool", "incomplete", "success", "final", completion_tokens=1024)
    _profile(gateway, 65536)
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    assert runtime._content_limits(context, logical_model="general", max_output_tokens=1024,
                                   source_count=2).prompt_bytes == 12288
    assert runtime._content_limits(context, logical_model="general", max_output_tokens=2048,
                                   source_count=2).prompt_bytes == 8192
    with pytest.raises(RuntimeExecutionError, match="message boundary exceeds limit"):
        _ = [event async for event in runtime.run(context)]
    assert len(gateway.requests) == 2 and len(harness.calls) == 1
    assert sum(len(str(message.content).encode("utf-8"))
               for message in gateway.requests[1].messages) == prompt_bytes
    saved = await runtime.save_checkpoint()
    assert saved.state["structured_repairs"] == {}
    assert mapping(saved.state["usage"])["tokens"] == 3275


@pytest.mark.parametrize("phase", ["prepared", "running", "succeeded", "rejected"])
async def test_routing_guard_only_checks_pending_reserved_requests(
    phase: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _routing_window_fixture(monkeypatch)
    repository = InMemoryArtifactRepository()
    routing = {"context_window_tokens": 8192}
    gateway = SaturatedGateway("tool", "incomplete",
                               "incomplete" if phase == "rejected" else "success", "final",
                               completion_tokens=1024)
    _profile(gateway, 65536)
    original = subject(gateway, dispatch, repository, RecordingHarnessToolGateway())
    events: list[Any] = []
    failure = "structured output invalid" if phase == "rejected" else None
    if failure is None:
        events = [event async for event in original.run(_context(
            token_budget=30000, routing_decision=routing,
        ))]
    else:
        with pytest.raises(RuntimeExecutionError, match=failure):
            async for event in original.run(_context(token_budget=30000, routing_decision=routing)):
                events.append(event)
    checkpoint = _phase_checkpoint(events, phase)
    before = checkpoint.to_payload()
    repair = mapping(mapping(checkpoint.state["structured_repairs"])["draft"])
    assert repair["max_output_tokens"] == 2048
    restored_gateway = SaturatedGateway("final", completion_tokens=1024)
    _profile(restored_gateway, 4096)
    harness = RecordingHarnessToolGateway()
    restored = subject(restored_gateway, dispatch, repository, harness)
    await restored.restore_checkpoint(checkpoint)
    expected_failure = {
        "prepared": "message boundary exceeds limit",
        "running": "outcome requires confirmation",
        "rejected": "structured output invalid",
    }.get(phase)
    context = _context(token_budget=30000, routing_decision=routing, checkpoint=checkpoint)
    if expected_failure is None:
        _ = [event async for event in restored.run(context)]
    else:
        with pytest.raises(RuntimeExecutionError, match=expected_failure):
            _ = [event async for event in restored.run(context)]
    assert len(restored_gateway.requests) == int(phase == "succeeded") and harness.calls == []
    saved = await restored.save_checkpoint()
    assert mapping(mapping(saved.state["structured_repairs"])["draft"]) == repair
    assert mapping(saved.state["usage"])["tokens"] == (
        mapping(checkpoint.state["usage"])["tokens"] + (11 if phase == "succeeded" else 0)
    )
    assert checkpoint.to_payload() == before


async def test_routing_guard_preserves_legacy_prepared_cap_and_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _routing_window_fixture(monkeypatch)
    repository = InMemoryArtifactRepository()
    routing = {"context_window_tokens": 4096}
    gateway = SaturatedGateway("tool", "incomplete", "success", "final", completion_tokens=1024)
    _profile(gateway, 65536)
    original = subject(gateway, dispatch, repository, RecordingHarnessToolGateway())
    with monkeypatch.context() as legacy:
        legacy.setattr(adapter, "_post_tool_finalization_output_limit",
                       lambda request, evidence, remaining: min(request.max_output_tokens, remaining))
        events = [event async for event in original.run(_context(
            token_budget=30000, routing_decision=routing,
        ))]
    checkpoint = _phase_checkpoint(events, "prepared")
    before = checkpoint.to_payload()
    repair = mapping(mapping(checkpoint.state["structured_repairs"])["draft"])
    restored_gateway = SaturatedGateway("success", "final", completion_tokens=1024)
    _profile(restored_gateway, 65536)
    harness = RecordingHarnessToolGateway()
    restored = subject(restored_gateway, dispatch, repository, harness)
    await restored.restore_checkpoint(checkpoint)
    _ = [event async for event in restored.run(_context(
        token_budget=30000, routing_decision=routing, checkpoint=checkpoint,
    ))]
    request = restored_gateway.requests[0]
    assert request.max_output_tokens == repair["max_output_tokens"] == 1024
    assert request.messages == gateway.requests[2].messages
    assert restored._model_request_sha256(request) == repair["correction_request_sha256"]
    assert len(restored_gateway.requests) == 2 and harness.calls == []
    assert checkpoint.to_payload() == before
