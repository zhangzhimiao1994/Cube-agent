"""Bounded real-framework regressions for engineering completion and restoration."""

import asyncio
from collections.abc import AsyncGenerator, Mapping
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from autogen_agentchat.messages import TextMessage

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.models.types import ToolCall as GatewayToolCall
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
)
from agent_hub.runtime.autogen.termination import CompositeDiscussionTermination, DiscussionUsage
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from tests.unit.runtime.autogen.test_engineering_delivery import TOOLS, fixture


class Capabilities:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.results = {artifact.content["tool_name"]: artifact.content["result"]
                        for artifact in fixture()[1]}

    def is_replay_safe(self, name: str) -> bool:
        return False

    async def execute(self, *, tenant_id: UUID, run_id: UUID, actor: str, name: str,
                      arguments: Mapping[str, JsonValue], idempotency_key: str) -> Mapping[str, JsonValue]:
        self.calls.append(name)
        result = self.results[name]
        assert isinstance(result, Mapping)
        return result


class Gateway:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions.copy()
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        text: str | None = "engineer"
        calls: tuple[GatewayToolCall, ...] = ()
        if request.logical_model != "selector":
            action = self.actions.pop(0) if self.actions else "[COMPLETE] no delivery"
            if action in TOOLS["engineer"]:
                text = None
                calls = (GatewayToolCall(id=f"call_{len(self.requests)}",
                                         name=action, arguments={}),)
            else:
                text = action
        return GatewayCompletion(response=ModelResponse(
            text=text, tool_calls=calls,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        ), deployment_id="shared", logical_model=request.logical_model,
            provider_id="test", provider_model="test/model", cost_usd=Decimal(0))


def plan(*, writer: bool = True) -> DiscussionPlan:
    return DiscussionPlan(participants=(
        DiscussionParticipant(id="engineer", role="Engineer", goal="Implement", logical_model="shared",
                              allowed_tools=TOOLS["engineer"] if writer else ()),
        DiscussionParticipant(id="reviewer", role="Reviewer", goal="Review", logical_model="shared"),
    ), selector_model="selector", max_turns=8, token_budget=100, wall_time_seconds=10)


def context(version: int = 1) -> TaskContext:
    return TaskContext(run_id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role=Role.OPERATOR,
                       mode=TaskMode.DISCUSS, request="Build a project", token_budget=100,
                       routing_decision={"engineering_delivery_contract_version": version,
                                         "sandbox_profile": "workspace_write"})


async def test_complete_marker_cannot_finish_without_delivery_even_at_resource_stop() -> None:
    gateway, backend = Gateway(["[COMPLETE] done"]), Capabilities()
    runner = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=backend)
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "engineering_delivery_missing"
    assert 2 < len(gateway.requests) <= 50
    assert backend.calls == []


async def test_real_framework_write_verify_bundle_survives_message_compaction_and_restore() -> None:
    backend = Capabilities()
    gateway = Gateway(["[COMPLETE] premature", "workspace.write_text", "written",
                       "project.verify", "checked", "workspace.bundle", "[COMPLETE] delivered"])
    runner = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=backend)
    ctx = context()
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, (events[-1].reason, backend.calls)
    assert backend.calls == ["workspace.write_text", "project.verify", "workspace.bundle"]
    checkpoint = await runner.save_checkpoint()
    assert len(cast(tuple[JsonValue, ...], checkpoint.state["tool_ledger"])) == 3
    assert checkpoint.state["engineering_tool_prefix"] == 3
    assert any("write" in str(message.content) and "bundle" in str(message.content)
               for request in gateway.requests for message in request.messages)
    unused = Gateway([])
    restored = AutoGenDiscussionRuntime(unused, plan(), capability_gateway=backend,
                                       artifact_repository=runner._repository)
    await restored.restore_checkpoint(checkpoint)
    resumed = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": checkpoint}))]
    assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED
    assert unused.requests == [] and len(backend.calls) == 3


async def test_terminal_restore_cannot_promote_legacy_text_only_completion_to_v1() -> None:
    backend = Capabilities()
    runner = AutoGenDiscussionRuntime(Gateway(["[COMPLETE] old"]), plan(), capability_gateway=backend)
    ctx = context(0)
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runner.save_checkpoint()
    unused = Gateway([])
    restored = AutoGenDiscussionRuntime(unused, plan(), capability_gateway=backend,
                                       artifact_repository=runner._repository)
    await restored.restore_checkpoint(checkpoint)
    ctx = ctx.model_copy(update={"routing_decision": {"engineering_delivery_contract_version": 1},
                                 "checkpoint": checkpoint})
    resumed = [event async for event in restored.run(ctx)]
    assert resumed[-1].kind is EventKind.RUNTIME_FAILED
    assert resumed[-1].reason == "engineering_delivery_missing"
    assert unused.requests == [] and backend.calls == []


@pytest.mark.parametrize("writer,version", [(True, 0), (False, 1)])
async def test_v0_and_planning_only_discussion_preserve_completion(writer: bool, version: int) -> None:
    gateway = Gateway(["[COMPLETE] plan"])
    runner = AutoGenDiscussionRuntime(gateway, plan(writer=writer), capability_gateway=Capabilities())
    events = [event async for event in runner.run(context(version))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 2


@pytest.mark.parametrize("marker", ["[COMPLETE]", "[CONSENSUS]"])
async def test_completion_and_consensus_wait_for_ready(marker: str) -> None:
    ready = False
    termination = CompositeDiscussionTermination(
        usage=DiscussionUsage(), max_turns=20, token_budget=100,
        cost_budget_usd=Decimal(1), wall_time_seconds=10, consensus_votes=2,
        cancelled=lambda: False, delivery_ready=lambda: ready,
    )
    assert await termination([TextMessage(content=marker, source="engineer"),
                              TextMessage(content=marker, source="reviewer")]) is None
    ready = True
    assert await termination([TextMessage(content=marker, source="engineer")]) is not None


@pytest.mark.parametrize("limit", ["cancel", "tokens", "cost", "wall", "turns"])
async def test_delivery_gate_does_not_relax_resource_or_cancellation_stops(limit: str) -> None:
    termination = CompositeDiscussionTermination(
        usage=DiscussionUsage(tokens=100 if limit == "tokens" else 0,
                              cost_usd=Decimal(1) if limit == "cost" else Decimal(0)),
        max_turns=1 if limit == "turns" else 20, token_budget=100,
        cost_budget_usd=Decimal(1), wall_time_seconds=10, consensus_votes=2,
        cancelled=lambda: limit == "cancel", delivery_ready=lambda: False,
    )
    if limit == "wall":
        termination._started_at -= 20
    assert await termination([TextMessage(content="[COMPLETE]", source="engineer")]) is not None


async def test_nonterminal_compacted_prefix_resumes_without_reexecuting_previous_write() -> None:
    backend = Capabilities()
    runner = AutoGenDiscussionRuntime(Gateway(["workspace.write_text"]), plan(), capability_gateway=backend)
    ctx = context()
    saved = None
    stream = cast(AsyncGenerator[RunEvent, None], runner.run(ctx))
    try:
        async for event in stream:
            if (event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
                and event.checkpoint.state.get("engineering_tool_prefix") == 1):
                saved = event.checkpoint
                break
    finally:
        await stream.aclose()
    assert saved is not None and backend.calls == ["workspace.write_text"]
    gateway = Gateway(["project.verify", "checked", "workspace.bundle", "[COMPLETE] delivered"])
    restored = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=backend,
                                       artifact_repository=runner._repository)
    await restored.restore_checkpoint(saved)
    resumed = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": saved}))]
    assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED, resumed[-1].reason
    assert backend.calls == ["workspace.write_text", "project.verify", "workspace.bundle"]


@pytest.mark.parametrize("defect", ["orphan", "running", "wrong_digest", "prefix_running"])
async def test_terminal_restore_missing_or_uncertain_evidence_fails_without_execution(defect: str) -> None:
    backend = Capabilities()
    runner = AutoGenDiscussionRuntime(Gateway([
        "workspace.write_text", "written", "project.verify", "checked", "workspace.bundle", "[COMPLETE]",
    ]), plan(), capability_gateway=backend)
    ctx = context()
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runner.save_checkpoint()
    rows = [dict(cast(Mapping[str, JsonValue], row))
            for row in cast(tuple[JsonValue, ...], checkpoint.state["tool_ledger"])]
    if defect == "orphan":
        rows.clear()
    elif defect in {"running", "prefix_running"}:
        rows[-1]["status"] = "running"
    else:
        rows[-1]["artifact_sha256"] = "d" * 64
    state = {**checkpoint.state, "tool_ledger": tuple(rows),
             "engineering_tool_prefix": len(rows) if defect == "prefix_running" else 0}
    changed = RuntimeCheckpoint(id=uuid4(), runtime_type=checkpoint.runtime_type,
                                runtime_version=checkpoint.runtime_version, run_id=ctx.run_id,
                                tenant_id=ctx.tenant_id, mode=ctx.mode, state=state)
    unused = Gateway([])
    restored = AutoGenDiscussionRuntime(unused, plan(), capability_gateway=backend,
                                       artifact_repository=runner._repository)
    await restored.restore_checkpoint(changed)
    resumed = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": changed}))]
    assert resumed[-1].kind is EventKind.RUNTIME_FAILED
    assert unused.requests == [] and len(backend.calls) == 3


async def test_partial_model_failure_never_upgrades_undelivered_engineering_text() -> None:
    class BrokenGateway(Gateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) >= 2:
                raise ModelTransportError("unavailable", status_code=503)
            return await super().complete_with_context(request)

    runner = AutoGenDiscussionRuntime(BrokenGateway(["A useful engineering plan"]), plan(),
                                       capability_gateway=Capabilities())
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


async def test_engineering_prefix_cannot_restore_under_nonengineering_context() -> None:
    backend = Capabilities()
    runner = AutoGenDiscussionRuntime(Gateway([
        "workspace.write_text", "written", "project.verify", "checked", "workspace.bundle", "[COMPLETE]",
    ]), plan(), capability_gateway=backend)
    ctx = context()
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runner.save_checkpoint()
    unused = Gateway([])
    restored = AutoGenDiscussionRuntime(unused, plan(), capability_gateway=backend,
                                       artifact_repository=runner._repository)
    await restored.restore_checkpoint(checkpoint)
    downgraded = ctx.model_copy(update={"routing_decision": {}, "checkpoint": checkpoint})
    events = [event async for event in restored.run(downgraded)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert unused.requests == []


async def test_wall_expired_cancelled_error_during_message_storage_cannot_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AutoGenDiscussionRuntime._store_artifact
    entered = False

    async def interrupted_store(runner: AutoGenDiscussionRuntime, ctx: TaskContext,
                                artifact: Artifact) -> UUID:
        nonlocal entered
        if artifact.type == "text":
            entered = True
            assert runner._cancel_event is not None
            await asyncio.wait_for(runner._cancel_event.wait(), timeout=2)
            raise asyncio.CancelledError
        return await original(runner, ctx, artifact)

    monkeypatch.setattr(AutoGenDiscussionRuntime, "_store_artifact", interrupted_store)
    runner = AutoGenDiscussionRuntime(Gateway(["[COMPLETE] no actual project"]), plan(),
                                       capability_gateway=Capabilities())
    ctx = context().model_copy(update={"timeout_seconds": 0.1})
    events = [event async for event in runner.run(ctx)]
    assert entered
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "engineering_delivery_missing"
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


async def test_budget_stop_without_delivery_fails_without_extending_budget() -> None:
    gateway = Gateway(["[COMPLETE] just a plan"])
    runner = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=Capabilities())
    ctx = context().model_copy(update={"token_budget": 4})
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "engineering_delivery_missing"
    assert len(gateway.requests) == 2
