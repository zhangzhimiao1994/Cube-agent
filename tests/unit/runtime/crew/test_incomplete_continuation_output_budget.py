"""Offline continuation reservations after a qualified first-tool recovery."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    GatewayResponseCancelled,
)
from agent_hub.models.types import ModelRequest, TokenUsage
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    RecordingHarnessToolGateway,
    _context,
)
from tests.unit.runtime.crew.test_first_tool_incomplete_output_budget import SaturatedToolGateway
from tests.unit.runtime.crew.test_known_incomplete_recovery import collect
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_recovery import (
    NonReplayCapabilities,
    draft_models,
    subject,
    tool_plan,
)

FLAG = "incomplete_continuation_output_growth"


def boundary(events: list[RunEvent], index: int, status: str) -> RuntimeCheckpoint:
    return next(event.checkpoint for event in events if event.checkpoint is not None
                and event.checkpoint.state["phase"] == "running"
                and len(draft_models(event.checkpoint)) == index + 2
                and draft_models(event.checkpoint)[-1]["attempt"] == 1
                and draft_models(event.checkpoint)[-1]["call_index"] == index
                and draft_models(event.checkpoint)[-1]["status"] == status)


async def natural_boundary(index: int = 1, status: str = "prepared", tokens: int = 30_000) -> tuple[
    InMemoryArtifactRepository, RuntimeCheckpoint,
]:
    repository = InMemoryArtifactRepository()
    gateway = SaturatedToolGateway("incomplete", "tool", "tool", "success", "final")
    runtime = subject(gateway, tool_plan(tokens), repository, RecordingHarnessToolGateway())
    return repository, boundary(await collect(runtime, tokens=tokens), index, status)


@pytest.mark.parametrize("completion,expected", [(6144, 12288), (6083, 12288), (6082, 6144)])
async def test_safe_tool_continuations_retain_first_reservation(
    completion: int, expected: int,
) -> None:
    gateway = SaturatedToolGateway("incomplete", "tool", "tool", "success", "final",
                                   completion=completion)
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    events = await collect(runtime)
    assert [request.max_output_tokens for request in gateway.requests[:4]] == [
        6144, expected, expected, expected,
    ]
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.runtime_version == "11" and checkpoint.state[FLAG] is True
    rows = draft_models(checkpoint)
    assert "reserved_max_output_tokens" not in rows[0]
    for row, request in zip(rows[1:], gateway.requests[1:4], strict=True):
        assert row["reserved_max_output_tokens"] == expected
        assert row["request_sha256"] == runtime._model_request_sha256(request)
        assert request.logical_model == "selected" and not request.allow_fallback
        assert request.tools == gateway.requests[0].tools
        assert request.response_schema == gateway.requests[0].response_schema
        assert request.timeout_seconds <= gateway.requests[0].timeout_seconds
    assert len(harness.calls) == 2
    assert checkpoint.state["structured_repairs"] == {}
    assert mapping(checkpoint.state["usage"])["tokens"] == 2234 + completion + 62
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.05"
    assert sum(event.kind is EventKind.STEP_RETRYING for event in events) == 1


@pytest.mark.parametrize("scope", ["step", "plan", "context"])
@pytest.mark.parametrize("remaining", [9000, 622])
async def test_every_continuation_clamps_remaining_budget(scope: str, remaining: int) -> None:
    tokens = 8378 + remaining
    dispatch = tool_plan(tokens if scope == "plan" else 30_000)
    if scope == "step":
        dispatch = dispatch.model_copy(update={"steps": (
            dispatch.steps[0].model_copy(update={"token_budget": tokens}), dispatch.steps[1],
        )})
    gateway = SaturatedToolGateway("incomplete", "tool", "tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    context = _context(token_budget=tokens if scope == "context" else 30_000,
                       routing_decision={"runtime_plan_token_budget": dispatch.total_token_budget})
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [request.max_output_tokens for request in gateway.requests[1:4]] == [
        remaining, remaining - 17, remaining - 34,
    ]
    assert [row["reserved_max_output_tokens"]
            for row in draft_models(await runtime.save_checkpoint())[1:]] == [
        remaining, remaining - 17, remaining - 34,
    ]
    assert len(harness.calls) == 2


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize("status", ["prepared", "running", "succeeded"])
@pytest.mark.parametrize("tokens", [30_000, 17378, 9000])
async def test_restore_reuses_each_paid_or_pending_round_without_duplicate_effects(
    index: int, status: str, tokens: int,
) -> None:
    repository, checkpoint = await natural_boundary(index, status, tokens)
    before = checkpoint.to_payload()
    outcomes = (("tool", "success", "final") if index == 1 and status == "prepared"
                else ("final",) if index == 2 and status == "succeeded" else ("success", "final"))
    gateway = SaturatedToolGateway(*outcomes)
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(tokens), repository, harness)
    await runtime.restore_checkpoint(checkpoint)
    await collect(runtime, checkpoint=checkpoint, tokens=tokens,
                  fail="outcome requires confirmation" if status == "running" else None)
    saved = await runtime.save_checkpoint()
    if status == "running":
        assert gateway.requests == [] and harness.calls == []
        assert saved.to_payload() == before
    else:
        assert len(gateway.requests) == len(outcomes)
        assert len(harness.calls) == int(index == 1)
        assert all(call.arguments != {"q": "safe-2"} for _, call in harness.calls)
        start_index = index + int(status == "succeeded")
        for offset, request in enumerate(gateway.requests[:-1]):
            assert request.max_output_tokens == min(12288, tokens - 8378 - 17 * (start_index + offset))
        for old, new in zip(draft_models(checkpoint), draft_models(saved), strict=False):
            assert old["request_sha256"] == new["request_sha256"]
            if old["status"] == "succeeded":
                assert old == new
        assert mapping(saved.state["usage"])["tokens"] == 8440
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("change", [
    "missing_flag", "false_flag", "typed_flag", "version", "missing_cap", "small_cap",
    "large_cap", "bool_cap", "hash", "first_cap", "source_key", "source_ids",
])
async def test_corrupt_continuation_cannot_dispatch(change: str) -> None:
    repository, checkpoint = await natural_boundary()
    payload: dict[str, Any] = checkpoint.to_payload()
    state = payload["state"]
    rows = sorted(state["models"].values(), key=lambda row: (row["attempt"], row["call_index"]))
    if change == "missing_flag":
        state.pop(FLAG, None)
    elif change == "false_flag":
        state[FLAG] = False
    elif change == "typed_flag":
        state[FLAG] = 1
    elif change == "version":
        payload["runtime_version"] = "10"
    elif change == "missing_cap":
        rows[-1].pop("reserved_max_output_tokens", None)
    elif change in {"small_cap", "large_cap", "bool_cap"}:
        rows[-1]["reserved_max_output_tokens"] = {
            "small_cap": 12287, "large_cap": 12289, "bool_cap": True,
        }[change]
    elif change == "hash":
        rows[-1]["request_sha256"] = "a" * 64
    elif change == "first_cap":
        rows[1]["reserved_max_output_tokens"] = 6144
    elif change == "source_key":
        first_key = next(key for key, row in state["models"].items()
                         if row["attempt"] == 1 and row["call_index"] == 0)
        state["models"]["a" * 64] = state["models"].pop(first_key)
    else:
        next(iter(state["rejected_outputs"].values()))["source_ids"] = [
            rows[1]["artifact_id"],
        ]
    payload["state_sha256"] = ""
    corrupted = RuntimeCheckpoint.from_payload(payload)
    gateway = SaturatedToolGateway("tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), repository, harness)
    with pytest.raises(RuntimeExecutionError, match="checkpoint|request changed"):
        await runtime.restore_checkpoint(corrupted)
        await collect(runtime, checkpoint=corrupted)
    assert gateway.requests == [] and harness.calls == []


def reserved_row(checkpoint: RuntimeCheckpoint, index: int) -> Mapping[str, Any]:
    return next(row for row in draft_models(checkpoint)
                if row["attempt"] == 1 and row["call_index"] == index)


@pytest.mark.parametrize("phase", ["prepared", "running", "terminal", "rejected"])
async def test_later_incomplete_is_paid_once_without_another_recovery(phase: str) -> None:
    repository = InMemoryArtifactRepository()
    gateway = SaturatedToolGateway("incomplete", "tool", "incomplete")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), repository, harness)
    events = await collect(runtime, fail="structured output invalid")
    assert len(gateway.requests) == 3 and len(harness.calls) == 1
    assert gateway.requests[-1].max_output_tokens == 12288
    assert sum(event.kind is EventKind.STEP_RETRYING for event in events) == 1
    if phase == "terminal":
        checkpoint = await runtime.save_checkpoint()
    else:
        checkpoint = boundary(events, 1, phase)
    replay_gateway = SaturatedToolGateway("success", "final")
    replay_harness = RecordingHarnessToolGateway()
    replay = subject(replay_gateway, tool_plan(), repository, replay_harness)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail=(
        None if phase == "prepared" else "outcome requires confirmation" if phase == "running"
        else "structured output invalid|checkpoint is terminal"
    ))
    assert replay_harness.calls == []
    assert len(replay_gateway.requests) == (2 if phase == "prepared" else 0)
    assert (await runtime.save_checkpoint()).state["structured_repairs"] == {}
    if phase == "rejected":
        assert mapping(checkpoint.state["usage"])["tokens"] == 16773
        assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.03"


@pytest.mark.parametrize("scope", ["step", "plan", "tokens"])
async def test_exhaustion_after_safe_tool_stops_before_continuation(scope: str) -> None:
    dispatch = tool_plan(8395 if scope == "tokens" else 30_000)
    if scope == "step":
        dispatch = dispatch.model_copy(update={"steps": (
            dispatch.steps[0].model_copy(update={"cost_budget_usd": Decimal("0.02")}),
            dispatch.steps[1],
        )})
    elif scope == "plan":
        dispatch = dispatch.model_copy(update={"total_cost_usd": Decimal("0.02"), "steps": (
            dispatch.steps[0].model_copy(update={"cost_budget_usd": Decimal("0.02")}),
            dispatch.steps[1].model_copy(update={"cost_budget_usd": Decimal(0)}),
        )})
    gateway = SaturatedToolGateway("incomplete", "tool")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    await collect(runtime, tokens=dispatch.total_token_budget, fail="budget exhausted")
    assert len(gateway.requests) == 2 and len(harness.calls) == 1
    assert len(draft_models(await runtime.save_checkpoint())) == 2


@pytest.mark.parametrize("field", ["remaining_timeout_seconds", "remaining_absolute_timeout_seconds"])
async def test_expired_prepared_continuation_never_reads_artifacts_or_dispatches(
    field: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, checkpoint = await natural_boundary()
    payload: dict[str, Any] = checkpoint.to_payload()
    payload["state"][field] = 0.0
    payload["state_sha256"] = ""
    expired = RuntimeCheckpoint.from_payload(payload)

    async def no_reads(*args: Any, **kwargs: Any) -> tuple[Artifact, ...]:
        raise AssertionError("expired reservation read artifacts")

    monkeypatch.setattr(repository, "get_many", no_reads)
    gateway = SaturatedToolGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), repository, harness)
    await runtime.restore_checkpoint(expired)
    await collect(runtime, checkpoint=expired, fail="incomplete recovery deadline exhausted")
    assert gateway.requests == [] and harness.calls == []
    assert (await runtime.save_checkpoint()).to_payload() == expired.to_payload()


@pytest.mark.parametrize("remaining", [0, 1000])
async def test_stale_prepared_cap_cannot_shrink_to_a_new_context_budget(remaining: int) -> None:
    repository, checkpoint = await natural_boundary()
    gateway = SaturatedToolGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), repository, harness)
    await runtime.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match="budget exhausted"):
        _ = [event async for event in runtime.run(_context(
            checkpoint=checkpoint, token_budget=8395 + remaining,
            routing_decision={"runtime_plan_token_budget": 30_000},
        ))]
    assert gateway.requests == [] and harness.calls == []
    assert (await runtime.save_checkpoint()).to_payload() == checkpoint.to_payload()


@pytest.mark.parametrize("field", ["run_id", "tenant_id"])
async def test_continuation_cannot_cross_context_issuer(field: str) -> None:
    repository, checkpoint = await natural_boundary()
    gateway = SaturatedToolGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), repository, harness)
    await runtime.restore_checkpoint(checkpoint)
    with pytest.raises(ValidationError, match="checkpoint run, tenant, or mode"):
        _ = [event async for event in runtime.run(_context(
            checkpoint=checkpoint, token_budget=30_000, **{field: UUID(int=7)},
        ))]
    assert gateway.requests == [] and harness.calls == []


async def test_cancelled_continuation_keeps_receipt_and_never_replays() -> None:
    class CancelledGateway(SaturatedToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                if len(self.requests) == 3:
                    raise GatewayResponseCancelled(receipt=error) from None
                raise

    repository = InMemoryArtifactRepository()
    gateway = CancelledGateway("incomplete", "tool", "incomplete")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), repository, harness)
    with pytest.raises(asyncio.CancelledError):
        await collect(runtime)
    checkpoint = await runtime.save_checkpoint()
    assert reserved_row(checkpoint, 1)["status"] == "received_cancelled"
    assert reserved_row(checkpoint, 1)["reserved_max_output_tokens"] == 12288
    assert mapping(checkpoint.state["usage"])["tokens"] == 16773
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.03"
    replay_gateway = SaturatedToolGateway("success")
    replay_harness = RecordingHarnessToolGateway()
    replay = subject(replay_gateway, tool_plan(), repository, replay_harness)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail="outcome requires confirmation")
    assert replay_gateway.requests == [] and replay_harness.calls == []
    assert (await replay.save_checkpoint()).to_payload() == checkpoint.to_payload()


@pytest.mark.parametrize("phase", ["succeeded", "rejected"])
async def test_paid_continuation_at_exact_step_cost_limit_is_not_rebilled(phase: str) -> None:
    repository = InMemoryArtifactRepository()
    base = tool_plan()
    dispatch = base.model_copy(update={"steps": (
        base.steps[0].model_copy(update={"cost_budget_usd": Decimal("0.03")}), base.steps[1],
    )})
    gateway = SaturatedToolGateway("incomplete", "tool",
                                   "incomplete" if phase == "rejected" else "success", "final")
    runtime = subject(gateway, dispatch, repository, RecordingHarnessToolGateway())
    events = await collect(runtime, fail="structured output invalid" if phase == "rejected" else None)
    checkpoint = boundary(events, 1, phase)
    assert mapping(mapping(checkpoint.state["step_usage"])["draft"])["cost_usd"] == "0.03"
    replay_gateway = SaturatedToolGateway("final")
    harness = RecordingHarnessToolGateway()
    replay = subject(replay_gateway, dispatch, repository, harness)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint,
                  fail="structured output invalid" if phase == "rejected" else None)
    saved = await replay.save_checkpoint()
    assert len(replay_gateway.requests) == int(phase == "succeeded") and harness.calls == []
    assert mapping(saved.state["step_usage"])["draft"] == mapping(checkpoint.state["step_usage"])["draft"]
    assert saved.state["structured_repairs"] == {}
    for old, new in zip(draft_models(checkpoint), draft_models(saved), strict=False):
        assert old == new


@pytest.mark.parametrize("expired_at", ["before_prepared", "after_prepared"])
async def test_continuation_checks_deadline_before_running_boundary(
    expired_at: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = CrewDispatchRuntime._execute_model_request
    transitions: list[str] = []

    async def expire(self: CrewDispatchRuntime, *args: Any, **kwargs: Any) -> tuple[GatewayCompletion, Artifact]:
        if kwargs["attempt"] == 1 and kwargs["cursor"].value == 1:
            original = kwargs["model_boundary"]

            async def record(key: str, row: Any, **changes: Any) -> None:
                transitions.append(row["status"])
                await original(key, row, **changes)
                if row["status"] == "prepared" and expired_at == "after_prepared":
                    kwargs["run_state"].deadline = asyncio.get_running_loop().time() - 1

            kwargs["model_boundary"] = record
            if expired_at == "before_prepared":
                kwargs["run_state"].deadline = asyncio.get_running_loop().time() - 1
        return await execute(self, *args, **kwargs)

    monkeypatch.setattr(CrewDispatchRuntime, "_execute_model_request", expire)
    gateway = SaturatedToolGateway("incomplete", "tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="incomplete recovery deadline exhausted")
    assert transitions == ([] if expired_at == "before_prepared" else ["prepared"])
    assert len(gateway.requests) == 2 and len(harness.calls) == 1
    saved = await runtime.save_checkpoint()
    if expired_at == "after_prepared":
        assert reserved_row(saved, 1)["reserved_max_output_tokens"] == 12288
    else:
        assert len(draft_models(saved)) == 2


async def test_content_limits_follow_each_clamped_round(monkeypatch: pytest.MonkeyPatch) -> None:
    contract = CrewDispatchRuntime._response_contract_messages
    limits_seen: list[Any] = []

    def record(*args: Any, **kwargs: Any) -> Any:
        limits_seen.append(kwargs["limits"])
        return contract(*args, **kwargs)

    monkeypatch.setattr(CrewDispatchRuntime, "_response_contract_messages", staticmethod(record))
    gateway = SaturatedToolGateway("incomplete", "tool", "tool", "success", "final")
    runtime = subject(gateway, tool_plan(17378), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await collect(runtime, tokens=17378)
    context = _context(token_budget=17378)
    for request, limits in zip(gateway.requests, limits_seen, strict=True):
        assert limits == runtime._content_limits(
            context, logical_model=request.logical_model,
            max_output_tokens=request.max_output_tokens,
            source_count=1 if request.logical_model == "general" and request.response_schema is None else 0,
        )


@pytest.fixture
def legacy_runtime_type(monkeypatch: pytest.MonkeyPatch) -> type[CrewDispatchRuntime]:
    source = subprocess.check_output([
        "git", "show", "0ffdcc93421b3c15c9f7fc9c18103a42cec46cc8:src/agent_hub/runtime/crew/adapter.py",
    ], cwd=Path(__file__).resolve().parents[4], encoding="utf-8")
    module = ModuleType("agent_hub.runtime.crew._legacy_continuation_budget")
    module.__package__ = "agent_hub.runtime.crew"
    module.__file__ = adapter.__file__
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "<legacy-0ff-adapter>", "exec"), module.__dict__)  # noqa: S102
    return cast(type[CrewDispatchRuntime], module.CrewDispatchRuntime)


@pytest.mark.parametrize("status", ["prepared", "running", "succeeded", "rejected"])
@pytest.mark.parametrize("remaining", [21622, 622])
async def test_legacy_call_zero_growth_keeps_exact_later_requests_and_hashes(
    legacy_runtime_type: type[CrewDispatchRuntime], status: str, remaining: int,
) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan(8378 + remaining)
    class LegacyGateway(SaturatedToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                if len(self.requests) == 3:
                    assert error.evidence is not None
                    error.evidence = replace(error.evidence, usage=TokenUsage(1, 16, 17))
                raise

    gateway = LegacyGateway("incomplete", "tool",
                            "incomplete" if status == "rejected" else "success", "final")
    runtime = legacy_runtime_type(gateway, dispatch, artifact_repository=repository,
        crew_factory=FastFactory(), capability_gateway=NonReplayCapabilities(),
        harness_tool_gateway=RecordingHarnessToolGateway())
    events: list[RunEvent] = []
    if status == "rejected":
        error_type = cast(type[Exception], sys.modules[legacy_runtime_type.__module__].RuntimeExecutionError)
        with pytest.raises(error_type):
            async for event in runtime.run(_context(token_budget=dispatch.total_token_budget)):
                events.append(event)
    else:
        events = [event async for event in runtime.run(_context(token_budget=dispatch.total_token_budget))]
    checkpoint = boundary(events, 1, status)
    assert FLAG not in checkpoint.state
    assert draft_models(checkpoint)[1]["reserved_max_output_tokens"] == min(12288, remaining)
    assert "reserved_max_output_tokens" not in reserved_row(checkpoint, 1)
    replay_gateway = SaturatedToolGateway(*(("final",) if status == "succeeded" else ("success", "final")))
    harness = RecordingHarnessToolGateway()
    replay = subject(replay_gateway, dispatch, repository, harness)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, tokens=dispatch.total_token_budget, fail=(
        "outcome requires confirmation" if status == "running" else
        "structured output invalid|dispatch budget exhausted" if status == "rejected" else None
    ))
    saved = await replay.save_checkpoint()
    assert harness.calls == []
    for old, new in zip(draft_models(checkpoint), draft_models(saved), strict=False):
        assert old["request_sha256"] == new["request_sha256"]
        assert old.get("reserved_max_output_tokens") == new.get("reserved_max_output_tokens")
    if status in {"running", "rejected"}:
        assert replay_gateway.requests == [] and saved.to_payload() == checkpoint.to_payload()
    else:
        assert saved.state[FLAG] is False
        if status == "prepared":
            assert replay_gateway.requests[0].max_output_tokens == min(6144, remaining - 17)
