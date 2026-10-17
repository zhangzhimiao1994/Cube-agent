"""Versioned, receipt-bound growth after completed capability execution."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import ModelCapability, ModelRequest, TokenUsage
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.models.test_gateway import deployment
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    RecordingHarnessToolGateway,
    _context,
)
from tests.unit.runtime.crew.test_post_tool_incomplete_finalization import PostToolGateway
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_recovery import draft_models, subject, tool_plan

VERSION = "post_tool_finalization_growth_version"
TOKENS = 500_000


class SaturatedGateway(PostToolGateway):
    def __init__(self, *outcomes: str, capacity: int = 200_000, saturated: bool = True,
                 trusted_scope: bool = True) -> None:
        super().__init__(*outcomes, trusted_scope=trusted_scope)
        self.saturated = saturated
        self._registry = ModelRegistry((deployment(
            "selected", logical_model="general", capabilities=frozenset(ModelCapability),
            max_output_tokens=capacity, context_window_tokens=1_000_000,
        ),))

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        try:
            return await super().complete_with_context(request)
        except GatewayRejectedOutput as error:
            assert error.evidence is not None
            cap = request.max_output_tokens if self.saturated else request.max_output_tokens // 2
            error.evidence = replace(error.evidence, usage=TokenUsage(10, cap, 10 + cap))
            raise


async def run(runtime: CrewDispatchRuntime, *, version: object = 1,
              checkpoint: RuntimeCheckpoint | None = None, fail: str | None = None) -> list[Any]:
    context = _context(token_budget=TOKENS, checkpoint=checkpoint,
                       routing_decision={} if version is None else {VERSION: version})
    events = []
    if fail is None:
        async for event in runtime.run(context):
            events.append(event)
    else:
        with pytest.raises(RuntimeExecutionError, match=fail):
            async for event in runtime.run(context):
                events.append(event)
    return events


async def test_v1_grows_repeatedly_not_just_one_extra_and_preserves_root() -> None:
    gateway = SaturatedGateway("tool", "incomplete", "incomplete", "incomplete", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(TOKENS), InMemoryArtifactRepository(), harness)
    events = await run(runtime)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(harness.calls) == 1
    assert [request.max_output_tokens for request in gateway.requests[2:-1]] == [12288, 24576, 49152]
    first, *tail = gateway.requests[2:-1]
    assert all(request.messages == first.messages and request.response_schema == first.response_schema
               and request.tools == () and not request.allow_fallback for request in (first, *tail))
    checkpoint = await runtime.save_checkpoint()
    repair = mapping(mapping(checkpoint.state["structured_repairs"])["draft"])
    assert repair["status"] == "rejected" and repair["max_output_tokens"] == 12288
    rows = draft_models(checkpoint)
    assert [row["status"] for row in rows] == ["succeeded", "rejected", "rejected", "rejected", "succeeded"]
    assert all("post_tool_growth" in row for row in rows[3:])
    assert checkpoint.state[VERSION] == 1


@pytest.mark.parametrize("version", [None, 0])
async def test_legacy_never_grows_or_adds_state_marker(version: object) -> None:
    gateway = SaturatedGateway("tool", "incomplete", "incomplete")
    runtime = subject(gateway, tool_plan(TOKENS), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await run(runtime, version=version, fail="structured output invalid")
    assert len(gateway.requests) == 3
    assert VERSION not in (await runtime.save_checkpoint()).state


@pytest.mark.parametrize("version", [True, "1", 2, -1])
async def test_invalid_route_version_fails_before_gateway(version: object) -> None:
    gateway = SaturatedGateway("tool")
    runtime = subject(gateway, tool_plan(TOKENS), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await run(runtime, version=version, fail="post-tool.*version")
    assert gateway.requests == []


@pytest.mark.parametrize("phase", ["prepared", "running", "rejected", "succeeded", "completed"])
async def test_growth_checkpoint_restore_accounts_once_and_never_repeats_tools(phase: str) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan(TOKENS)
    original = subject(SaturatedGateway("tool", "incomplete", "incomplete", "incomplete", "success", "final"),
                       dispatch, repository, RecordingHarnessToolGateway())
    events = await run(original)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None and (
        event.checkpoint.state["phase"] == "completed" if phase == "completed" else
        event.checkpoint.state["phase"] == "running"
        and len(draft_models(event.checkpoint)) == (5 if phase == "succeeded" else 4)
        and draft_models(event.checkpoint)[-1]["status"] == phase
    ))
    before = checkpoint.to_payload()
    outcomes = {"prepared": ("incomplete", "success", "final"), "running": (),
                "rejected": ("success", "final"), "succeeded": ("final",), "completed": ()}[phase]
    # The succeeded boundary is the second growth, whereas rejected/prepared use the first.
    if phase == "succeeded":
        checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                          and len(draft_models(event.checkpoint)) == 5
                          and draft_models(event.checkpoint)[-1]["status"] == "succeeded")
        before = checkpoint.to_payload()
    gateway = SaturatedGateway(*outcomes)
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, harness)
    await restored.restore_checkpoint(checkpoint)
    await run(restored, checkpoint=checkpoint,
              fail="outcome requires confirmation" if phase == "running" else None)
    assert harness.calls == [] and len(gateway.requests) == len(outcomes)
    assert checkpoint.to_payload() == before
    if phase == "running":
        assert (await restored.save_checkpoint()).to_payload() == before
    else:
        assert (await restored.save_checkpoint()).state["usage"] == (await original.save_checkpoint()).state["usage"]
        assert (await restored.save_checkpoint()).state["structured_repairs"] == checkpoint.state["structured_repairs"]


@pytest.mark.parametrize("change", [
    "root_key", "root_repair_sha256", "previous_key", "previous_request_sha256",
    "previous_receipt_sha256", "previous_cap", "tool_history_sha256", "request_sha256", "version",
    "cap", "marker", "source_ids", "unsaturated", "fallback", "unknown_usage", "actor", "slot", "extra",
    "root_source", "root_correction", "root_slot_link",
])
async def test_forged_growth_chain_is_rejected_before_io(change: str) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan(TOKENS)
    original = subject(SaturatedGateway("tool", "incomplete", "incomplete", "success", "final"),
                       dispatch, repository, RecordingHarnessToolGateway())
    events = await run(original)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and len(draft_models(event.checkpoint)) == 4
                      and draft_models(event.checkpoint)[-1]["status"] == "prepared")
    payload: dict[str, Any] = checkpoint.to_payload()
    state = payload["state"]
    row = next(row for row in state["models"].values() if "post_tool_growth" in row)
    link = row["post_tool_growth"]
    previous = state["models"][link["previous_key"]]
    receipt = state["rejected_outputs"][link["previous_key"]]
    if change in link:
        link[change] = True if change in {"previous_cap", "version"} else "0" * 64
    elif change == "cap":
        row["reserved_max_output_tokens"] = previous["reserved_max_output_tokens"]
    elif change == "marker":
        del state[VERSION]
    elif change == "source_ids":
        receipt["source_ids"] = []
    elif change == "unsaturated":
        receipt["usage"]["completion_tokens"] = 10
        receipt["usage"]["total_tokens"] = receipt["usage"]["prompt_tokens"] + 10
    elif change == "fallback":
        receipt["fallback_used"] = True
    elif change == "unknown_usage":
        receipt["usage_status"] = "missing"
        receipt["usage"] = None
    elif change == "actor":
        row["actor"] = "final"
    elif change == "slot":
        row["call_index"] += 1
    elif change in {"root_source", "root_correction"}:
        state["structured_repairs"]["draft"]["source_key" if change == "root_source" else "correction_key"] = "0" * 64
    elif change == "root_slot_link":
        previous["post_tool_growth"] = dict(link)
    else:
        link["extra"] = 0
    payload["state_sha256"] = ""
    forged = RuntimeCheckpoint.from_payload(payload)
    gateway = SaturatedGateway()
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, harness)
    with pytest.raises(RuntimeExecutionError):
        await restored.restore_checkpoint(forged)
        await run(restored, checkpoint=forged)
    assert gateway.requests == [] and harness.calls == []


@pytest.mark.parametrize("last", ["invalid", "tool", "transport"])
async def test_non_incomplete_growth_failure_does_not_repeat_or_repair(last: str) -> None:
    gateway = SaturatedGateway("tool", "incomplete", "incomplete", last)
    harness = RecordingHarnessToolGateway()
    repository = InMemoryArtifactRepository()
    runtime = subject(gateway, tool_plan(TOKENS), repository, harness)
    await run(runtime, fail="structured|provider unavailable|forbidden")
    assert len(gateway.requests) == 4 and len(harness.calls) == 1
    checkpoint = await runtime.save_checkpoint()
    restored_gateway = SaturatedGateway()
    restored = subject(restored_gateway, tool_plan(TOKENS), repository, RecordingHarnessToolGateway())
    await restored.restore_checkpoint(checkpoint)
    await run(restored, checkpoint=checkpoint, fail="structured|outcome requires confirmation|forbidden")
    assert restored_gateway.requests == []


async def test_undersaturated_rejection_cannot_grow() -> None:
    gateway = SaturatedGateway("tool", "incomplete", "incomplete", saturated=False)
    runtime = subject(gateway, tool_plan(TOKENS), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await run(runtime, fail="structured output invalid")
    assert len(gateway.requests) == 3


async def test_deployment_capacity_prevents_identical_cap_retry() -> None:
    gateway = SaturatedGateway("tool", "incomplete", "incomplete", capacity=12288)
    runtime = subject(gateway, tool_plan(TOKENS), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await run(runtime, fail="growth exhausted")
    assert len(gateway.requests) == 3


@pytest.mark.parametrize("condition", ["missing_scope", "unknown_scope", "missing_usage", "fallback"])
async def test_growth_requires_live_trusted_accounted_receipt(condition: str) -> None:
    class BoundaryGateway(SaturatedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == 2:
                if condition == "missing_scope":
                    self.trusted_scope = False
                elif condition == "unknown_scope":
                    self.scope_entries = 2
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                if len(self.requests) == 3:
                    assert error.evidence is not None
                    if condition == "missing_usage":
                        error.evidence = replace(error.evidence, usage=None, usage_status="missing")
                    elif condition == "fallback":
                        error.fallback_used = True
                raise

    gateway = BoundaryGateway("tool", "incomplete", "incomplete")
    runtime = subject(gateway, tool_plan(TOKENS), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await run(runtime, fail="structured output invalid|unaccounted")
    assert len(gateway.requests) == 3
    assert draft_models(await runtime.save_checkpoint())[-1]["failure_reason"] == "structured output rejected"


async def test_actual_gateway_input_estimate_bounds_transport_not_role_cap() -> None:
    class ContextGateway(SaturatedGateway):
        def _estimated_input_tokens(self, request: ModelRequest, deployments: object) -> int:
            return 10_000

    gateway = ContextGateway("tool", "incomplete", "incomplete")
    gateway._registry = ModelRegistry((replace(
        gateway._registry.deployments[0], max_output_tokens=20_000, context_window_tokens=20_000,
    ),))
    runtime = subject(gateway, tool_plan(TOKENS), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await run(runtime, fail="growth exhausted")
    assert gateway.requests[2].max_output_tokens == 10_000
    assert draft_models(await runtime.save_checkpoint())[2]["reserved_max_output_tokens"] == 10_000


@pytest.mark.parametrize("boundary", ["model_count", "round_count", "input_budget", "deadline"])
async def test_growth_checks_live_limits_before_next_transport(boundary: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class LimitGateway(SaturatedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput:
                if len(self.requests) == 3:
                    if boundary == "model_count":
                        monkeypatch.setattr(adapter, "_MAX_CHECKPOINT_MODEL_LEDGER_ENTRIES", 3)
                    elif boundary == "round_count":
                        budget = adapter._tool_round_budget(_context(), tool_plan(TOKENS).steps[0])
                        monkeypatch.setattr(adapter, "_tool_round_budget", lambda *_: replace(budget, hard_limit=2))
                    elif boundary == "input_budget":
                        monkeypatch.setattr(CrewDispatchRuntime, "_incomplete_recovery_remaining_tokens", lambda *_: 100)
                    else:
                        def expired(*_: object) -> float:
                            raise RuntimeExecutionError("dispatch deadline exhausted")
                        monkeypatch.setattr(CrewDispatchRuntime, "_remaining_timeout", expired)
                raise

    gateway = LimitGateway("tool", "incomplete", "incomplete")
    runtime = subject(gateway, tool_plan(TOKENS), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await run(runtime, fail="exhausted|checkpoint")
    assert len(gateway.requests) == 3


async def test_new_route_never_upgrades_existing_legacy_checkpoint() -> None:
    repository = InMemoryArtifactRepository()
    original = subject(SaturatedGateway("tool", "incomplete", "success", "final"), tool_plan(TOKENS),
                       repository, RecordingHarnessToolGateway())
    await run(original, version=0)
    checkpoint = await original.save_checkpoint()
    gateway = SaturatedGateway()
    restored = subject(gateway, tool_plan(TOKENS), repository, RecordingHarnessToolGateway())
    await restored.restore_checkpoint(checkpoint)
    await run(restored, checkpoint=checkpoint, version=1)
    assert gateway.requests == []
    assert (await restored.save_checkpoint()).to_payload() == checkpoint.to_payload()
    assert VERSION not in checkpoint.state


async def test_growth_never_refreshes_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_refresh(*_: object) -> float:
        raise AssertionError("growth refreshed its deadline")
    monkeypatch.setattr(CrewDispatchRuntime, "_recovery_step_deadline", no_refresh)
    gateway = SaturatedGateway("tool", "incomplete", "incomplete", "incomplete", "success", "final")
    runtime = subject(gateway, tool_plan(TOKENS), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    events = await run(runtime)
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    assert all(request.timeout_seconds <= gateway.requests[1].timeout_seconds for request in gateway.requests[2:-1])


@pytest.mark.parametrize("route", [None, 0, True, "1", 2])
async def test_maintenance_step_v1_cannot_be_enabled_by_legacy_or_invalid_route(route: object) -> None:
    dispatch = tool_plan(TOKENS)
    dispatch = dispatch.model_copy(update={"steps": (
        dispatch.steps[0].model_copy(update={"workspace_maintenance_contract_version": 1}),
        *dispatch.steps[1:],
    )})
    gateway = SaturatedGateway("tool")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    routing = {} if route is None else {"workspace_maintenance_contract_version": route}
    with pytest.raises(RuntimeExecutionError, match="maintenance.*version"):
        async for _ in runtime.run(_context(token_budget=TOKENS, routing_decision=routing)):
            pass
    assert gateway.requests == [] and harness.calls == []
