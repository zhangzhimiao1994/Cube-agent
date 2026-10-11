"""Owned, offline ordinary-incomplete budgets and immutable reservations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    GatewayResponseCancelled,
    ModelGateway,
)
from agent_hub.models.litellm_client import ModelResponseError
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    RejectedOutputEvidence,
    TokenUsage,
)
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, CrewRunStream, RuntimeExecutionError
from agent_hub.runtime.crew.plan import DispatchPlan
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    RecordingHarnessToolGateway,
    _context,
)
from tests.unit.runtime.crew.test_known_incomplete_recovery import (
    IncompleteGateway,
    collect,
    plan,
    runtime,
)
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping


class SaturatedGateway(IncompleteGateway):
    def __init__(self, *outcomes: str, completion_tokens: int = 6144) -> None:
        super().__init__(*outcomes)
        self.completion_tokens = completion_tokens

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        try:
            return await super().complete_with_context(request)
        except GatewayRejectedOutput as error:
            assert error.evidence is not None
            error.evidence = replace(error.evidence, usage=TokenUsage(
                2234, self.completion_tokens, 2234 + self.completion_tokens,
            ))
            raise


@pytest.mark.parametrize("completion,expected", [
    (6144, 12288), (6083, 12288), (6082, 6144), (16, 6144),
])
async def test_only_saturated_ordinary_recovery_grows_and_reserves(
    completion: int, expected: int,
) -> None:
    gateway = SaturatedGateway(completion_tokens=completion)
    subject = runtime(gateway)
    events = await collect(subject)
    original, recovered, _ = gateway.requests
    assert original.max_output_tokens == 6144
    assert recovered.max_output_tokens == expected
    assert recovered.logical_model == "selected" and not recovered.allow_fallback
    assert recovered.response_schema == original.response_schema
    assert recovered.tools == original.tools == ()
    assert "Do not continue" in " ".join(str(message.content) for message in recovered.messages)
    assert recovered.timeout_seconds <= original.timeout_seconds
    checkpoint = await subject.save_checkpoint()
    assert checkpoint.runtime_version == "11"
    assert checkpoint.state["incomplete_recovery_output_growth"] is True
    rows = mapping(checkpoint.state["models"])
    recovery = next(mapping(row) for row in rows.values()
                    if mapping(row)["step_id"] == "draft" and mapping(row)["attempt"] == 1)
    assert recovery["reserved_max_output_tokens"] == expected
    assert recovery["request_sha256"] == subject._model_request_sha256(recovered)
    assert mapping(checkpoint.state["usage"])["tokens"] == 2234 + completion + 28
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.03"
    assert sum(event.kind is EventKind.STEP_RETRYING for event in events) == 1


@pytest.mark.parametrize("remaining", [9000, 605, 0])
async def test_growth_clamps_remaining_and_never_calls_with_zero(remaining: int) -> None:
    gateway = SaturatedGateway()
    tokens = 8378 + remaining
    subject = runtime(gateway, plan(tokens=tokens))
    await collect(subject, tokens=tokens, fail="budget exhausted" if remaining == 0 else None)
    if remaining:
        assert gateway.requests[1].max_output_tokens == remaining
    else:
        assert len(gateway.requests) == 1


async def test_task_context_independently_clamps_grown_recovery() -> None:
    dispatch = plan(tokens=30000)
    context = _context(
        token_budget=17378, routing_decision={"runtime_plan_token_budget": 30000},
    )
    assert dispatch.total_token_budget == dispatch.steps[0].token_budget == 30000
    assert context.token_budget == 17378
    gateway = SaturatedGateway()
    subject = runtime(gateway, dispatch)
    events = [event async for event in subject.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    source = next(event.checkpoint for event in events if event.checkpoint is not None
                  and mapping(event.checkpoint.state["usage"])["tokens"] == 8378)
    assert len(mapping(source.state["models"])) == 1
    original, recovered, _ = gateway.requests
    assert original.max_output_tokens == 6144
    assert recovered.max_output_tokens == 9000
    checkpoint = await subject.save_checkpoint()
    row = recovery_row(checkpoint)
    assert row["reserved_max_output_tokens"] == 9000
    assert row["request_sha256"] == subject._model_request_sha256(recovered)
    assert mapping(checkpoint.state["usage"])["tokens"] == 8406
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.03"
    assert checkpoint.state["tools"] == {}
    assert cast(RecordingHarnessToolGateway, subject._tool_gateway).calls == []


async def test_grown_second_incomplete_keeps_accounting_without_third_call() -> None:
    gateway = SaturatedGateway("incomplete", "incomplete")
    subject = runtime(gateway)
    await collect(subject, fail="model response incomplete")
    assert len(gateway.requests) == 2
    assert gateway.requests[1].max_output_tokens == 12288
    checkpoint = await subject.save_checkpoint()
    assert mapping(checkpoint.state["usage"])["tokens"] == 2 * 8378
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.02"
    assert checkpoint.state["tools"] == {} and checkpoint.state["structured_repairs"] == {}


def parallel_plan(tokens: int) -> DispatchPlan:
    base = plan(tokens=tokens)
    writer = next(agent for agent in base.agents if agent.id == "writer")
    return base.model_copy(update={
        "max_parallelism": 2,
        "agents": (*base.agents, writer.model_copy(update={
            "id": "other", "logical_model": "other", "fallback_models": (),
        })),
        "steps": (base.steps[0], base.steps[0].model_copy(update={
            "id": "other", "agent": "other", "task": "Owned parallel result",
        }), base.steps[1].model_copy(update={"depends_on": ("draft", "other")})),
    })


class ParallelGateway:
    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.release_other = asyncio.Event()
        self.other_paid = asyncio.Event()
        self.requests: list[ModelRequest] = []
        self.draft = SaturatedGateway(
            "incomplete", "incomplete" if phase in {"rejected", "exhausted"} else "success", "final",
        )

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if request.logical_model == "other":
            await asyncio.wait_for(self.release_other.wait(), 5)
            return GatewayCompletion(
                response=ModelResponse(text='{"summary":"owned parallel result"}', usage=TokenUsage(1, 999, 1000)),
                logical_model="other", deployment_id="other", provider_id="provider",
                provider_model="provider/model", cost_usd=Decimal("0.01"),
            )
        if request.logical_model == "selected" and self.phase != "prepared":
            self.release_other.set()
            await asyncio.wait_for(self.other_paid.wait(), 5)
        try:
            return await self.draft.complete_with_context(request)
        except GatewayRejectedOutput as error:
            if request.logical_model == "selected" and self.phase == "rejected":
                assert error.evidence is not None
                error.evidence = replace(error.evidence, usage=TokenUsage(1, 998, 999))
            raise


class ParallelRuntime(CrewDispatchRuntime):
    def __init__(self, gateway: ParallelGateway, dispatch: DispatchPlan,
                 repository: InMemoryArtifactRepository) -> None:
        self.fixture_gateway = gateway
        super().__init__(
            cast(ModelGateway, gateway), dispatch, artifact_repository=repository,
            crew_factory=FastFactory(), harness_tool_gateway=RecordingHarnessToolGateway(),
        )

    def _make_checkpoint(self, *args: Any, **kwargs: Any) -> RuntimeCheckpoint:
        checkpoint = super()._make_checkpoint(*args, **kwargs)
        if any(mapping(row)["step_id"] == "other" and mapping(row)["status"] == "succeeded"
               for row in mapping(checkpoint.state["models"]).values()):
            self.fixture_gateway.other_paid.set()
        return checkpoint

    async def _execute_model_request(self, *args: Any, **kwargs: Any) -> tuple[GatewayCompletion, Artifact]:
        if args[1].id == "draft" and kwargs["attempt"] == 1 and self.fixture_gateway.phase == "prepared":
            original = kwargs["model_boundary"]

            async def boundary(key: str, state: Any, **changes: Any) -> None:
                await original(key, state, **changes)
                if state["status"] == "prepared":
                    self.fixture_gateway.release_other.set()
                    await asyncio.wait_for(self.fixture_gateway.other_paid.wait(), 5)

            kwargs["model_boundary"] = boundary
        return await super()._execute_model_request(*args, **kwargs)


def parallel_runtime(gateway: ParallelGateway, tokens: int,
                     repository: InMemoryArtifactRepository) -> ParallelRuntime:
    return ParallelRuntime(gateway, parallel_plan(tokens), repository)


def recovery_row(checkpoint: RuntimeCheckpoint) -> Mapping[str, Any]:
    return next(mapping(row) for row in mapping(checkpoint.state["models"]).values()
                if mapping(row)["step_id"] == "draft" and mapping(row)["attempt"] == 1)


@pytest.mark.parametrize("phase", ["succeeded", "rejected", "exhausted"])
async def test_paid_parallel_receipt_keeps_reserved_cap_hash_and_accounting(phase: str) -> None:
    repository = InMemoryArtifactRepository()
    gateway = ParallelGateway(phase)
    subject = parallel_runtime(gateway, 17378, repository)
    failure = {"rejected": "model response incomplete", "exhausted": "dispatch budget exhausted"}.get(phase)
    events = await collect(subject, tokens=17378, fail=failure)
    status = "rejected" if phase == "exhausted" else phase
    checkpoint = (await subject.save_checkpoint() if phase == "exhausted" else next(
                      event.checkpoint for event in events if event.checkpoint is not None
                      and event.checkpoint.state["phase"] == ("failed" if phase == "exhausted" else "running")
                      and any(mapping(row)["attempt"] == 1 and mapping(row)["step_id"] == "draft"
                              and mapping(row)["status"] == status
                              for row in mapping(event.checkpoint.state["models"]).values())))
    row = recovery_row(checkpoint)
    assert row["reserved_max_output_tokens"] == 9000
    assert gateway.draft.requests[1].max_output_tokens == 9000
    assert row["request_sha256"] == subject._model_request_sha256(gateway.draft.requests[1])
    replay_gateway = IncompleteGateway("final")
    replay = runtime(replay_gateway, parallel_plan(17378), repository)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, tokens=17378, fail=failure)
    saved = await replay.save_checkpoint()
    assert recovery_row(saved) == row
    assert len(replay_gateway.requests) == (1 if phase == "succeeded" else 0)
    assert mapping(saved.state["usage"])["tokens"] == (
        mapping(checkpoint.state["usage"])["tokens"] + (11 if phase == "succeeded" else 0)
    )
    assert Decimal(mapping(saved.state["usage"])["cost_usd"]) == (
        Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) + (Decimal("0.01") if phase == "succeeded" else 0)
    )
    assert saved.state["tools"] == {}


async def test_prepared_parallel_usage_exhaustion_never_lowers_cap_or_dispatches() -> None:
    repository = InMemoryArtifactRepository()
    gateway = ParallelGateway("prepared")
    subject = parallel_runtime(gateway, 17378, repository)
    events = await collect(subject, tokens=17378, fail="incomplete recovery budget exhausted")
    assert len(gateway.draft.requests) == 1
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and any(mapping(row)["step_id"] == "other" and mapping(row)["status"] == "succeeded"
                              for row in mapping(event.checkpoint.state["models"]).values())
                      and recovery_row(event.checkpoint)["status"] == "prepared")
    assert recovery_row(checkpoint)["reserved_max_output_tokens"] == 9000
    before = checkpoint.to_payload()
    replay_gateway = IncompleteGateway("success")
    replay = runtime(replay_gateway, parallel_plan(17378), repository)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, tokens=17378, fail="incomplete recovery budget exhausted")
    assert replay_gateway.requests == []
    assert (await replay.save_checkpoint()).to_payload() == before


async def natural_checkpoint(phase: str = "prepared", *, legacy: bool = False
                             ) -> tuple[InMemoryArtifactRepository, RuntimeCheckpoint, SaturatedGateway]:
    repository = InMemoryArtifactRepository()
    gateway = SaturatedGateway("incomplete", "incomplete") if phase == "rejected" else SaturatedGateway()

    class LegacyRuntime(CrewDispatchRuntime):
        def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            stream = cast(CrewRunStream, super().run(context))
            stream._state.incomplete_recovery_output_growth = False
            return stream

    subject = (LegacyRuntime(
        cast(ModelGateway, gateway), plan(), artifact_repository=repository, crew_factory=FastFactory(),
        harness_tool_gateway=RecordingHarnessToolGateway(),
    ) if legacy else runtime(gateway, repository=repository))
    events = await collect(subject, fail="model response incomplete" if phase == "rejected" else None)
    if phase == "rejected":
        return repository, await subject.save_checkpoint(), gateway
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and event.checkpoint.state["phase"] == "running"
                      and any(mapping(row)["step_id"] == "draft" and mapping(row)["attempt"] == 1
                              and mapping(row)["status"] == phase
                              for row in mapping(event.checkpoint.state["models"]).values()))
    return repository, checkpoint, gateway


@pytest.mark.parametrize("field,value", [
    ("flag", None), ("flag", False), ("flag", 1), ("flag", "true"),
    ("cap", None), ("cap", False), ("cap", 0), ("cap", -1), ("cap", 12289),
    ("cap", 1000001), ("cap", 12288.0), ("cap", "12288"),
    ("actor", "reviewer"), ("purpose", "review"), ("step_id", "final_response"),
    ("source_actor", "reviewer"), ("source_status", "failed"),
    ("source_marker", "structured output rejected"), ("receipt_reason", "invalid_output"),
    ("receipt_status", "completed"), ("receipt_usage", "missing"),
    ("initial_cap", 6144), ("extra", 1),
])
async def test_reservation_strict_schema_and_source_binding(field: str, value: Any) -> None:
    repository, checkpoint, _ = await natural_checkpoint()
    payload: dict[str, Any] = checkpoint.to_payload()
    state = payload["state"]
    source = next(row for row in state["models"].values() if row["attempt"] == 0)
    recovery = next(row for row in state["models"].values() if row["attempt"] == 1)
    receipt = next(iter(state["rejected_outputs"].values()))
    if field == "flag":
        if value is None:
            state.pop("incomplete_recovery_output_growth")
        else:
            state["incomplete_recovery_output_growth"] = value
    elif field == "cap":
        if value is None:
            recovery.pop("reserved_max_output_tokens")
        else:
            recovery["reserved_max_output_tokens"] = value
    elif field.startswith("source_"):
        source[{"source_actor": "actor", "source_status": "status", "source_marker": "failure_reason"}[field]] = value
    elif field.startswith("receipt_"):
        receipt[{"receipt_reason": "reason", "receipt_status": "output_status", "receipt_usage": "usage_status"}[field]] = value
    elif field == "initial_cap":
        source["reserved_max_output_tokens"] = value
    else:
        recovery[field] = value
    payload["state_sha256"] = ""
    corrupted = RuntimeCheckpoint.from_payload(payload)
    gateway = IncompleteGateway("success")
    replay = runtime(gateway, repository=repository)
    with pytest.raises(RuntimeExecutionError, match="checkpoint.*(invalid|incompatible)"):
        await replay.restore_checkpoint(corrupted)
        await collect(replay, checkpoint=corrupted)
    assert gateway.requests == []


@pytest.mark.parametrize("phase", ["prepared", "succeeded", "rejected"])
@pytest.mark.parametrize("field,value", [("reserved_max_output_tokens", 12287), ("request_sha256", "a" * 64)])
async def test_valid_numeric_cap_or_hash_tamper_never_replays(phase: str, field: str, value: Any) -> None:
    repository, checkpoint, _ = await natural_checkpoint(phase)
    payload: dict[str, Any] = checkpoint.to_payload()
    next(row for row in payload["state"]["models"].values() if row["attempt"] == 1)[field] = value
    payload["state_sha256"] = ""
    corrupted = RuntimeCheckpoint.from_payload(payload)
    gateway = IncompleteGateway("success")
    replay = runtime(gateway, repository=repository)
    await replay.restore_checkpoint(corrupted)
    await collect(replay, checkpoint=corrupted, fail="model request changed after checkpoint")
    assert gateway.requests == []
    assert (await replay.save_checkpoint()).to_payload() == corrupted.to_payload()


@pytest.mark.parametrize("version", ["9", "10"])
async def test_older_checkpoint_rejects_true_growth_flag(version: str) -> None:
    repository = InMemoryArtifactRepository()
    subject = runtime(IncompleteGateway("success", "final"), repository=repository)
    await collect(subject)
    checkpoint = await subject.save_checkpoint()
    assert checkpoint.state["incomplete_recovery_output_growth"] is True
    assert all("reserved_max_output_tokens" not in mapping(row)
               for row in mapping(checkpoint.state["models"]).values())
    payload: dict[str, Any] = checkpoint.to_payload()
    payload["runtime_version"] = version
    if version == "9":
        for key in ("remaining_timeout_seconds", "remaining_absolute_timeout_seconds",
                    "timeout_progress_units"):
            payload["state"].pop(key, None)
    payload["state_sha256"] = ""
    legacy = RuntimeCheckpoint.from_payload(payload)
    gateway = IncompleteGateway()
    replay = runtime(gateway, repository=repository)
    with pytest.raises(RuntimeExecutionError, match="checkpoint"):
        await replay.restore_checkpoint(legacy)
    assert gateway.requests == []
    assert cast(RecordingHarnessToolGateway, replay._tool_gateway).calls == []


@pytest.mark.parametrize("phase", ["prepared", "succeeded", "rejected", "running"])
async def test_old_v11_missing_flag_keeps_old_cap_hash_and_no_replay(phase: str) -> None:
    repository, checkpoint, original_gateway = await natural_checkpoint(phase, legacy=True)
    assert original_gateway.requests[1].max_output_tokens == 6144
    row = recovery_row(checkpoint)
    assert "reserved_max_output_tokens" not in row
    payload: dict[str, Any] = checkpoint.to_payload()
    payload["state"].pop("incomplete_recovery_output_growth")
    payload["state_sha256"] = ""
    legacy = RuntimeCheckpoint.from_payload(payload)
    gateway = IncompleteGateway(*(("final",) if phase == "succeeded" else ("success", "final")))
    replay = runtime(gateway, repository=repository)
    await replay.restore_checkpoint(legacy)
    failure = {"running": "model outcome requires confirmation", "rejected": "model response incomplete"}.get(phase)
    await collect(replay, checkpoint=legacy, fail=failure)
    saved = await replay.save_checkpoint()
    assert recovery_row(saved)["request_sha256"] == row["request_sha256"]
    assert "reserved_max_output_tokens" not in recovery_row(saved)
    if phase in {"running", "rejected"}:
        assert gateway.requests == [] and saved.to_payload() == legacy.to_payload()
    else:
        assert saved.state["incomplete_recovery_output_growth"] is False
        assert mapping(saved.state["usage"])["tokens"] == (16756 if phase == "rejected" else 8406)
        assert len(gateway.requests) == (0 if phase == "rejected" else 1 if phase == "succeeded" else 2)
        if phase == "prepared":
            assert gateway.requests[0].max_output_tokens == 6144
            assert replay._model_request_sha256(gateway.requests[0]) == row["request_sha256"]


async def test_sufficient_prepared_parallel_budget_resumes_exact_reserved_request() -> None:
    repository = InMemoryArtifactRepository()
    gateway = ParallelGateway("prepared")
    subject = parallel_runtime(gateway, 30000, repository)
    events = await collect(subject)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and any(mapping(row)["step_id"] == "other" and mapping(row)["status"] == "succeeded"
                              for row in mapping(event.checkpoint.state["models"]).values())
                      and recovery_row(event.checkpoint)["status"] == "prepared")
    row = recovery_row(checkpoint)
    assert row["reserved_max_output_tokens"] == 12288
    replay_gateway = IncompleteGateway("success", "final")
    replay = runtime(replay_gateway, parallel_plan(30000), repository)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint)
    assert len(replay_gateway.requests) == 2 and replay_gateway.requests[0].max_output_tokens == 12288
    assert replay._model_request_sha256(replay_gateway.requests[0]) == row["request_sha256"]
    saved = await replay.save_checkpoint()
    assert mapping(saved.state["usage"])["tokens"] == 9406
    assert mapping(saved.state["usage"])["cost_usd"] == "0.04"


@pytest.mark.parametrize("phase", ["running", "received_cancelled"])
async def test_grown_uncertain_receipts_never_replay(phase: str) -> None:
    class CancelledRecoveryGateway(SaturatedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                if len(self.requests) == 2:
                    raise GatewayResponseCancelled(receipt=error) from None
                raise

    if phase == "running":
        repository, checkpoint, gateway = await natural_checkpoint("running")
    else:
        repository = InMemoryArtifactRepository()
        gateway = CancelledRecoveryGateway("incomplete", "incomplete")
        subject = runtime(gateway, repository=repository)
        with pytest.raises(asyncio.CancelledError):
            await collect(subject)
        checkpoint = await subject.save_checkpoint()
        assert mapping(checkpoint.state["usage"])["tokens"] == 16756
        assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.02"
    assert recovery_row(checkpoint)["status"] == phase
    assert recovery_row(checkpoint)["reserved_max_output_tokens"] == 12288
    assert recovery_row(checkpoint)["request_sha256"] == CrewDispatchRuntime._model_request_sha256(gateway.requests[1])
    replay_gateway = IncompleteGateway("success")
    replay = runtime(replay_gateway, repository=repository)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail="model outcome requires confirmation")
    assert replay_gateway.requests == []
    assert (await replay.save_checkpoint()).to_payload() == checkpoint.to_payload()
    assert cast(RecordingHarnessToolGateway, replay._tool_gateway).calls == []


@pytest.mark.parametrize("hard_cap,expected", [(8192, 8192), (None, 8500)])
async def test_grown_runtime_request_traverses_owned_gateway_hard_and_context_caps(
    hard_cap: int | None, expected: int,
) -> None:
    class Estimator:
        def estimate_input(self, request: ModelRequest) -> int:
            return 11500

        def estimate(self, request: ModelRequest) -> int:
            return self.estimate_input(request) + request.max_output_tokens

    class RecordingTransport:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete(self, selected: Deployment, request: ModelRequest, api_key: str) -> ModelResponse:
            del selected, api_key
            self.requests.append(request)
            assert len(self.requests) <= 3
            if len(self.requests) == 1:
                raise ModelResponseError("owned incomplete response", evidence=RejectedOutputEvidence(
                    final_text=None, usage=TokenUsage(2234, 6144, 8378),
                    usage_status="known", status="incomplete", reason="incomplete",
                ))
            return ModelResponse(
                text='{"summary":"owned handoff"}' if request.response_schema else "owned final answer",
                usage=TokenUsage(1, 16, 17) if request.response_schema else TokenUsage(1, 10, 11),
            )

    class RecordingGateway(ModelGateway):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.requests: list[ModelRequest] = []

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            return await super().complete_with_context(request)

    registry = ModelRegistry([deployment(
        "owned", logical_model="general", max_output_tokens=hard_cap, context_window_tokens=20000,
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT}),
    )])
    capacity = CapacityStub([lease("owned") for _ in range(3)])
    transport = RecordingTransport()
    gateway = RecordingGateway(registry, capacity, SecretStub([]), transport, token_estimator=Estimator())
    await capacity.initialize()
    subject = runtime(cast(IncompleteGateway, gateway))
    await collect(subject)
    assert len(gateway.requests) == len(transport.requests) == len(capacity.releases) == 3
    assert gateway.requests[1].max_output_tokens == 12288
    assert transport.requests[1].max_output_tokens == expected
    assert transport.requests[1].allow_fallback is False
    assert transport.requests[1].logical_model == "general" and transport.requests[1].tools == ()
    checkpoint = await subject.save_checkpoint()
    assert recovery_row(checkpoint)["reserved_max_output_tokens"] == 12288
    assert mapping(checkpoint.state["usage"])["tokens"] == 8406


@pytest.mark.parametrize("field", ["remaining_timeout_seconds", "remaining_absolute_timeout_seconds"])
async def test_expired_prepared_rejects_before_any_artifact_or_gateway_access(
    field: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, checkpoint, _ = await natural_checkpoint()
    payload: dict[str, Any] = checkpoint.to_payload()
    payload["state"][field] = 0.0
    payload["state_sha256"] = ""
    expired = RuntimeCheckpoint.from_payload(payload)
    counters = {"reads": 0, "writes": 0}

    get_many = repository.get_many

    async def slow_reads(*args: Any, **kwargs: Any) -> tuple[Artifact, ...]:
        counters["reads"] += 1
        await asyncio.sleep(0.01)
        return await get_many(*args, **kwargs)

    async def no_writes(*args: Any, **kwargs: Any) -> None:
        counters["writes"] += 1
        raise AssertionError("expired prepared wrote artifacts")

    monkeypatch.setattr(repository, "get_many", slow_reads)
    monkeypatch.setattr(repository, "reserve_write", no_writes)
    monkeypatch.setattr(repository, "put", no_writes)
    gateway = IncompleteGateway("success")
    replay = runtime(gateway, repository=repository)
    await replay.restore_checkpoint(expired)
    await collect(replay, checkpoint=expired, fail="incomplete recovery deadline exhausted")
    assert counters == {"reads": 0, "writes": 0} and gateway.requests == []
    assert (await replay.save_checkpoint()).to_payload() == expired.to_payload()
    assert cast(RecordingHarnessToolGateway, replay._tool_gateway).calls == []


async def test_prepared_boundary_expiry_never_calls_or_extends_deadline() -> None:
    class ExpiringRuntime(CrewDispatchRuntime):
        async def _execute_model_request(self, *args: Any, **kwargs: Any) -> tuple[GatewayCompletion, Artifact]:
            if kwargs.get("reserved_max_output_tokens") is not None:
                original = kwargs["model_boundary"]

                async def boundary(key: str, state: Mapping[str, JsonValue], **changes: Any) -> None:
                    await original(key, state, **changes)
                    if state["status"] == "prepared":
                        kwargs["run_state"].deadline = asyncio.get_running_loop().time() - 1

                kwargs["model_boundary"] = boundary
            return await super()._execute_model_request(*args, **kwargs)

    gateway = SaturatedGateway("incomplete")
    subject = ExpiringRuntime(
        cast(ModelGateway, gateway), plan(), artifact_repository=InMemoryArtifactRepository(),
        crew_factory=FastFactory(), harness_tool_gateway=RecordingHarnessToolGateway(),
    )
    await collect(subject, fail="incomplete recovery deadline exhausted")
    assert len(gateway.requests) == 1
    checkpoint = await subject.save_checkpoint()
    assert recovery_row(checkpoint)["status"] == "prepared"
    assert recovery_row(checkpoint)["reserved_max_output_tokens"] == 12288
    assert mapping(checkpoint.state["usage"])["tokens"] == 8378


async def test_prepared_exact_step_cost_boundary_never_dispatches_reserved_request() -> None:
    repository, original, _ = await natural_checkpoint()
    dispatch = plan()
    # Derive a strictly audited cost-boundary fixture; do not claim a new natural receipt.
    payload: dict[str, Any] = original.to_payload()
    cost = str(dispatch.steps[0].cost_budget_usd)
    next(iter(payload["state"]["rejected_outputs"].values()))["cost_usd"] = cost
    payload["state"]["usage"]["cost_usd"] = cost
    payload["state"]["step_usage"]["draft"]["cost_usd"] = cost
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    assert recovery_row(checkpoint)["reserved_max_output_tokens"] == 12288
    replay_gateway = IncompleteGateway("success")
    replay = runtime(replay_gateway, dispatch, repository)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail="incomplete recovery budget exhausted")
    assert replay_gateway.requests == [] and cast(RecordingHarnessToolGateway, replay._tool_gateway).calls == []
    assert (await replay.save_checkpoint()).to_payload() == checkpoint.to_payload()


@pytest.mark.parametrize("phase", ["succeeded", "rejected"])
async def test_paid_step_cost_at_exact_limit_reads_cached_receipt_without_rebilling(phase: str) -> None:
    base = plan()
    dispatch = base.model_copy(update={"steps": (
        base.steps[0].model_copy(update={"cost_budget_usd": Decimal("0.02")}), base.steps[1],
    )})
    repository = InMemoryArtifactRepository()
    gateway = SaturatedGateway("incomplete", "incomplete" if phase == "rejected" else "success", "final")
    subject = runtime(gateway, dispatch, repository)
    events = await collect(subject, fail="model response incomplete" if phase == "rejected" else None)
    checkpoint = (await subject.save_checkpoint() if phase == "rejected" else next(
        event.checkpoint for event in events if event.checkpoint is not None
        and event.checkpoint.state["phase"] == "running"
        and any(mapping(row)["attempt"] == 1 and mapping(row)["status"] == phase
                for row in mapping(event.checkpoint.state["models"]).values())
    ))
    assert mapping(mapping(checkpoint.state["step_usage"])["draft"])["cost_usd"] == "0.02"
    assert recovery_row(checkpoint)["reserved_max_output_tokens"] == 12288
    replay_gateway = IncompleteGateway("final")
    replay = runtime(replay_gateway, dispatch, repository)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail="model response incomplete" if phase == "rejected" else None)
    saved = await replay.save_checkpoint()
    assert recovery_row(saved) == recovery_row(checkpoint)
    assert mapping(mapping(saved.state["step_usage"])["draft"])["cost_usd"] == "0.02"
    assert len(replay_gateway.requests) == (1 if phase == "succeeded" else 0)
    assert cast(RecordingHarnessToolGateway, replay._tool_gateway).calls == []


@pytest.mark.parametrize("phase", ["prepared", "succeeded"])
@pytest.mark.parametrize("field", ["run_id", "tenant_id"])
async def test_reservation_rejects_cross_context_without_new_calls(phase: str, field: str) -> None:
    repository, checkpoint, _ = await natural_checkpoint(phase)
    gateway = IncompleteGateway("success")
    replay = runtime(gateway, repository=repository)
    await replay.restore_checkpoint(checkpoint)
    with pytest.raises(ValidationError, match="checkpoint run, tenant, or mode"):
        async for _ in replay.run(_context(checkpoint=checkpoint, **{field: UUID(int=7)})):
            pass
    assert gateway.requests == [] and cast(RecordingHarnessToolGateway, replay._tool_gateway).calls == []


async def test_prepared_context_same_id_different_valid_state_is_not_authority() -> None:
    repository, checkpoint, _ = await natural_checkpoint()
    payload: dict[str, Any] = checkpoint.to_payload()
    payload["state"]["remaining_timeout_seconds"] *= 0.8
    payload["state_sha256"] = ""
    substituted = RuntimeCheckpoint.from_payload(payload)
    assert substituted.id == checkpoint.id and substituted.state_sha256 != checkpoint.state_sha256
    gateway = IncompleteGateway("success")
    replay = runtime(gateway, repository=repository)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=substituted, fail="runtime checkpoint mismatch")
    assert gateway.requests == [] and cast(RecordingHarnessToolGateway, replay._tool_gateway).calls == []
    assert (await replay.save_checkpoint()).to_payload() == checkpoint.to_payload()
