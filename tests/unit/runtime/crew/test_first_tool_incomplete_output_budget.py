"""Offline first-tool growth, continuation reservations, and v11 replay boundaries."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput
from agent_hub.models.types import ModelRequest, TokenUsage
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    RecordingHarnessToolGateway,
    _context,
)
from tests.unit.runtime.crew.test_known_incomplete_recovery import collect
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_recovery import (
    NonReplayCapabilities,
    ToolIncompleteGateway,
    draft_models,
    subject,
    tool_plan,
)

FLAG = "first_tool_incomplete_recovery_output_growth"


class SaturatedToolGateway(ToolIncompleteGateway):
    def __init__(self, *outcomes: str, completion: int = 6144) -> None:
        super().__init__(*outcomes)
        self.completion = completion

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        try:
            result = await super().complete_with_context(request)
        except GatewayRejectedOutput as error:
            assert error.evidence is not None
            error.evidence = replace(error.evidence, usage=TokenUsage(
                2234, self.completion, 2234 + self.completion,
            ))
            raise
        if result.response.tool_calls:
            result = replace(result, response=replace(result.response, tool_calls=tuple(
                replace(call, id=f"approved-{len(self.requests)}",
                        arguments={"q": f"safe-{len(self.requests)}"})
                for call in result.response.tool_calls
            )))
        return result


@pytest.mark.parametrize("completion,expected", [(6144, 12288), (6083, 12288), (6082, 6144)])
async def test_first_tool_growth_only_on_known_saturation(completion: int, expected: int) -> None:
    gateway = SaturatedToolGateway(completion=completion)
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    events = await collect(runtime)
    original, fresh, followup, _ = gateway.requests
    assert original.max_output_tokens == 6144
    assert fresh.max_output_tokens == followup.max_output_tokens == expected
    assert fresh.logical_model == followup.logical_model == "selected"
    assert fresh.tools == followup.tools == original.tools
    assert fresh.response_schema == followup.response_schema == original.response_schema
    assert not fresh.allow_fallback and not followup.allow_fallback
    assert fresh.timeout_seconds <= original.timeout_seconds
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.runtime_version == "11" and checkpoint.state[FLAG] is True
    assert checkpoint.state["incomplete_continuation_output_growth"] is True
    rows = draft_models(checkpoint)
    assert rows[1]["reserved_max_output_tokens"] == expected
    assert "reserved_max_output_tokens" not in rows[0]
    assert rows[2]["reserved_max_output_tokens"] == expected
    assert rows[1]["request_sha256"] == runtime._model_request_sha256(fresh)
    assert rows[2]["request_sha256"] == runtime._model_request_sha256(followup)
    assert len(harness.calls) == 1
    assert mapping(checkpoint.state["usage"])["tokens"] == 2234 + completion + 45
    assert checkpoint.state["structured_repairs"] == {}
    assert sum(event.kind is EventKind.STEP_RETRYING for event in events) == 1


@pytest.mark.parametrize("scope", ["step", "plan", "context"])
@pytest.mark.parametrize("remaining", [9000, 622, 0])
async def test_first_tool_growth_clamps_each_budget_scope(scope: str, remaining: int) -> None:
    dispatch = tool_plan(8378 + remaining if scope == "plan" else 30_000)
    if scope == "step":
        dispatch = dispatch.model_copy(update={"steps": (
            dispatch.steps[0].model_copy(update={"token_budget": 8378 + remaining}),
            dispatch.steps[1],
        )})
    gateway = SaturatedToolGateway()
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    if scope == "context":
        context = _context(token_budget=8378 + remaining,
                           routing_decision={"runtime_plan_token_budget": 30_000})
        if remaining == 0:
            with pytest.raises(RuntimeExecutionError, match="budget exhausted"):
                async for _ in runtime.run(context):
                    pass
        else:
            events = [event async for event in runtime.run(context)]
            assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    else:
        await collect(runtime, tokens=dispatch.total_token_budget,
                      fail="budget exhausted" if remaining == 0 else None)
    if remaining:
        assert gateway.requests[1].max_output_tokens == remaining
        assert gateway.requests[2].max_output_tokens == remaining - 17
        assert len(harness.calls) == 1
    else:
        assert len(gateway.requests) == 1 and harness.calls == []


async def test_multiple_followup_rounds_reserve_within_first_reservation() -> None:
    gateway = SaturatedToolGateway("incomplete", "tool", "tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    await collect(runtime)
    assert [request.max_output_tokens for request in gateway.requests[:4]] == [
        6144, 12288, 12288, 12288,
    ]
    rows = draft_models(await runtime.save_checkpoint())
    assert rows[1]["reserved_max_output_tokens"] == 12288
    assert "reserved_max_output_tokens" not in rows[0]
    assert all(row["reserved_max_output_tokens"] == 12288 for row in rows[2:])
    assert len(harness.calls) == 2


async def test_other_steps_tool_history_does_not_block_first_tool_growth() -> None:
    base = tool_plan()
    dispatch = base.model_copy(update={
        "agents": (*base.agents, base.agents[0].model_copy(update={"id": "preflight"})),
        "steps": (base.steps[0].model_copy(update={"id": "preflight", "agent": "preflight"}),
                  base.steps[0].model_copy(update={"depends_on": ("preflight",)}), base.steps[1]),
    })
    gateway = SaturatedToolGateway("tool", "success", "incomplete", "tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    await collect(runtime)
    assert gateway.requests[3].max_output_tokens == 12288
    assert gateway.requests[4].max_output_tokens == 12288
    assert [call.actor for _, call in harness.calls] == ["preflight", "writer"]
    rows = draft_models(await runtime.save_checkpoint())
    assert rows[1]["reserved_max_output_tokens"] == 12288
    assert rows[2]["reserved_max_output_tokens"] == 12288


async def natural_boundary(phase: str = "fresh_prepared") -> tuple[
    InMemoryArtifactRepository, RuntimeCheckpoint,
]:
    repository = InMemoryArtifactRepository()
    runtime = subject(SaturatedToolGateway(), tool_plan(), repository, RecordingHarnessToolGateway())
    events = await collect(runtime)
    prefix, status = phase.split("_")
    for event in events:
        checkpoint = event.checkpoint
        if checkpoint is None or checkpoint.state["phase"] != "running":
            continue
        rows = draft_models(checkpoint)
        if len(rows) == (2 if prefix == "fresh" else 3) and rows[-1]["status"] == status:
            return repository, checkpoint
    raise AssertionError(f"missing natural boundary: {phase}")


@pytest.mark.parametrize("phase", ["fresh_prepared", "fresh_succeeded", "followup_prepared"])
@pytest.mark.parametrize("change", [
    "missing_flag", "false_flag", "typed_flag", "version", "missing_cap", "cap", "hash",
    "later_cap", "source_usage", "source_provenance", "source_request",
])
async def test_first_tool_reservation_tampering_cannot_dispatch(phase: str, change: str) -> None:
    repository, checkpoint = await natural_boundary(phase)
    payload: dict[str, Any] = checkpoint.to_payload()
    state = payload["state"]
    rows = sorted(state["models"].values(), key=lambda row: (row["attempt"], row["call_index"]))
    receipt = next(iter(state["rejected_outputs"].values()))
    if change == "missing_flag":
        state.pop(FLAG, None)
    elif change == "false_flag":
        state[FLAG] = False
    elif change == "typed_flag":
        state[FLAG] = 1
    elif change == "version":
        payload["runtime_version"] = "10"
    elif change == "missing_cap":
        rows[1].pop("reserved_max_output_tokens", None)
    elif change == "cap":
        rows[1]["reserved_max_output_tokens"] = 12287
    elif change == "hash":
        rows[1]["request_sha256"] = "a" * 64
    elif change == "later_cap":
        rows[-1 if phase == "followup_prepared" else 0]["reserved_max_output_tokens"] = 6144
    elif change == "source_usage":
        receipt["usage"]["completion_tokens"] -= 1
    elif change == "source_provenance":
        receipt["provenance"]["logical_model"] = "backup"
    elif change == "source_request":
        rows[0]["request_sha256"] = "a" * 64
    payload["state_sha256"] = ""
    corrupted = RuntimeCheckpoint.from_payload(payload)
    gateway = SaturatedToolGateway("tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    replay = subject(gateway, tool_plan(), repository, harness)
    with pytest.raises(RuntimeExecutionError, match="checkpoint|request changed"):
        await replay.restore_checkpoint(corrupted)
        await collect(replay, checkpoint=corrupted)
    assert gateway.requests == [] and harness.calls == []


@pytest.fixture
def v11_runtime_type(monkeypatch: pytest.MonkeyPatch) -> type[CrewDispatchRuntime]:
    source = subprocess.check_output([
        "git", "show", "f05b7d6cf5d12ae317c6556142f35b3dce70a0a0:src/agent_hub/runtime/crew/adapter.py",
    ], cwd=Path(__file__).resolve().parents[4], encoding="utf-8")
    module = ModuleType("agent_hub.runtime.crew._v11_first_tool_baseline")
    module.__package__ = "agent_hub.runtime.crew"
    module.__file__ = adapter.__file__
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "<v11-f05-adapter>", "exec"), module.__dict__)  # noqa: S102
    return cast(type[CrewDispatchRuntime], module.CrewDispatchRuntime)


@pytest.mark.parametrize("legacy", [False, True])
async def test_paid_medium_incomplete_receipts_never_replay_or_grow_on_restore(
    v11_runtime_type: type[CrewDispatchRuntime], legacy: bool,
) -> None:
    class MediumGateway(ToolIncompleteGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                assert error.evidence is not None
                prompt = 2465 if len(self.requests) == 1 else 2628
                error.evidence = replace(error.evidence, usage=TokenUsage(prompt, 6144, prompt + 6144))
                error.cost_usd = Decimal(0)
                raise

    repository = InMemoryArtifactRepository()
    dispatch = tool_plan()
    gateway = MediumGateway("incomplete", "incomplete")
    harness = RecordingHarnessToolGateway()
    runtime = (v11_runtime_type(gateway, dispatch, artifact_repository=repository,
        crew_factory=FastFactory(), capability_gateway=NonReplayCapabilities(), harness_tool_gateway=harness)
        if legacy else subject(gateway, dispatch, repository, harness))
    error_type = (cast(type[Exception], sys.modules[v11_runtime_type.__module__].RuntimeExecutionError)
                  if legacy else RuntimeExecutionError)
    checkpoints: list[RuntimeCheckpoint] = []
    with pytest.raises(error_type, match="structured output invalid"):
        async for event in runtime.run(_context(token_budget=30_000)):
            if event.checkpoint is not None:
                checkpoints.append(event.checkpoint)
    checkpoint = next(candidate for candidate in checkpoints
                      if candidate.state["phase"] == "running"
                      and len(draft_models(candidate)) == 2
                      and draft_models(candidate)[-1]["status"] == "rejected")
    assert checkpoint.state["incomplete_recovery_output_growth"] is True
    assert len(gateway.requests) == 2 and harness.calls == []
    assert gateway.requests[1].max_output_tokens == (6144 if legacy else 12288)
    assert mapping(checkpoint.state["usage"])["tokens"] == 17381
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0"
    assert checkpoint.state["structured_repairs"] == {} and checkpoint.state["tools"] == {}
    assert all(mapping(receipt)["usage_status"] == "known" and mapping(receipt)["final_text"] is None
               for receipt in mapping(checkpoint.state["rejected_outputs"]).values())
    before = checkpoint.to_payload()
    replay_gateway = ToolIncompleteGateway("tool", "success", "final")
    replay = subject(replay_gateway, dispatch, repository, harness)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail="structured output invalid")
    assert replay_gateway.requests == [] and harness.calls == []
    assert (await replay.save_checkpoint()).to_payload() == before


@pytest.mark.parametrize("tokens", [30_000, 9000])
@pytest.mark.parametrize("phase", [
    "source", "fresh_prepared", "fresh_running", "tool_prepared", "tool_running",
    "tool_succeeded", "followup_prepared", "followup_running", "followup_succeeded",
])
async def test_natural_v11_checkpoints_keep_original_tool_request_fingerprints(
    v11_runtime_type: type[CrewDispatchRuntime], phase: str, tokens: int,
) -> None:
    dispatch = tool_plan(tokens)
    repository = InMemoryArtifactRepository()
    source_gateway = ToolIncompleteGateway()
    baseline = v11_runtime_type(source_gateway, dispatch, artifact_repository=repository,
        crew_factory=FastFactory(), capability_gateway=NonReplayCapabilities(),
        harness_tool_gateway=RecordingHarnessToolGateway())
    events = [event async for event in baseline.run(_context(token_budget=tokens))]

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
    assert checkpoint.runtime_version == "11" and FLAG not in checkpoint.state
    assert all("reserved_max_output_tokens" not in row for row in draft_models(checkpoint))
    before = checkpoint.to_payload()
    outcomes = (("tool", "success", "final") if phase in {"source", "fresh_prepared"}
                else ("final",) if phase == "followup_succeeded" else ("success", "final"))
    gateway = ToolIncompleteGateway(*outcomes)
    harness = RecordingHarnessToolGateway()
    replay = subject(gateway, dispatch, repository, harness)
    await replay.restore_checkpoint(checkpoint)
    uncertain = phase.endswith("running")
    await collect(replay, checkpoint=checkpoint, tokens=tokens,
                  fail="outcome requires confirmation" if uncertain else None)
    saved = await replay.save_checkpoint()
    if uncertain:
        assert gateway.requests == [] and harness.calls == []
        assert saved.to_payload() == before
    else:
        assert saved.state[FLAG] is False
        assert all("reserved_max_output_tokens" not in row for row in draft_models(saved))
        expected_tools = int(phase in {"source", "fresh_prepared", "tool_prepared"})
        assert len(harness.calls) == expected_tools and len(gateway.requests) == len(outcomes)
        if phase in {"source", "fresh_prepared"}:
            assert gateway.requests[0].max_output_tokens == (6144 if tokens == 30_000 else 622)
        for old, new in zip(draft_models(checkpoint), draft_models(saved), strict=False):
            assert old["request_sha256"] == new["request_sha256"]
            if old["status"] in {"succeeded", "rejected"}:
                assert old == new
        assert mapping(saved.state["usage"])["tokens"] == 8423
        assert mapping(saved.state["usage"])["cost_usd"] == "0.04"
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("field", ["run_id", "tenant_id"])
async def test_first_tool_reservation_rejects_other_context_issuers(field: str) -> None:
    repository, checkpoint = await natural_boundary()
    gateway = SaturatedToolGateway("tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    replay = subject(gateway, tool_plan(), repository, harness)
    await replay.restore_checkpoint(checkpoint)
    with pytest.raises(ValidationError, match="checkpoint run, tenant, or mode"):
        async for _ in replay.run(_context(checkpoint=checkpoint, **{field: UUID(int=7)})):
            pass
    assert gateway.requests == [] and harness.calls == []


@pytest.mark.parametrize("version", ["9", "10", "11"])
@pytest.mark.parametrize("old_growth", [None, False, True])
@pytest.mark.parametrize("first_growth", [None, False, True])
async def test_supported_versions_distinguish_old_and_first_tool_growth_fields(
    v11_runtime_type: type[CrewDispatchRuntime], version: str,
    old_growth: bool | None, first_growth: bool | None,
) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan()
    baseline = v11_runtime_type(ToolIncompleteGateway("success", "final"), dispatch,
        artifact_repository=repository, crew_factory=FastFactory(),
        capability_gateway=NonReplayCapabilities(), harness_tool_gateway=RecordingHarnessToolGateway())
    events = [event async for event in baseline.run(_context(token_budget=30_000))]
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and event.checkpoint.state["phase"] == "running"
                      and draft_models(event.checkpoint)
                      and draft_models(event.checkpoint)[0]["status"] == "prepared")
    payload: dict[str, Any] = checkpoint.to_payload()
    payload["runtime_version"] = version
    for key, value in (("incomplete_recovery_output_growth", old_growth), (FLAG, first_growth)):
        if value is None:
            payload["state"].pop(key, None)
        else:
            payload["state"][key] = value
    if version == "9":
        for key in ("remaining_timeout_seconds", "remaining_absolute_timeout_seconds",
                    "timeout_progress_units"):
            payload["state"].pop(key, None)
    payload["state_sha256"] = ""
    boundary = RuntimeCheckpoint.from_payload(payload)
    gateway = ToolIncompleteGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    replay = subject(gateway, dispatch, repository, harness)
    if version != "11" and (old_growth is True or first_growth is True):
        with pytest.raises(RuntimeExecutionError, match="checkpoint.*(invalid|incompatible)"):
            await replay.restore_checkpoint(boundary)
        assert gateway.requests == []
    else:
        await replay.restore_checkpoint(boundary)
        await collect(replay, checkpoint=boundary)
        saved = await replay.save_checkpoint()
        assert saved.state[FLAG] is (first_growth is True)
        assert saved.state["incomplete_recovery_output_growth"] is (old_growth is True)
        assert draft_models(saved)[0]["request_sha256"] == draft_models(boundary)[0]["request_sha256"]
        assert all("reserved_max_output_tokens" not in row for row in draft_models(saved))
        assert gateway.requests[0].max_output_tokens == 6144
    assert harness.calls == []
