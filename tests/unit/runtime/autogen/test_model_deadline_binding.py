"""New AutoGen calls use live deadlines without reopening durable outcomes."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from decimal import Decimal
from typing import TypedDict, cast

import pytest
from autogen_core import CancellationToken
from autogen_core.models import UserMessage

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.adaptive_budget import AdaptiveDeadline
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    GatewayChatCompletionClient,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
    _DiscussionDurability,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent
from tests.integration.runtime.test_autogen_adapter import ScriptedGateway
from tests.unit.runtime.autogen.test_dynamic_limits import _context, _plan
from tests.unit.runtime.autogen.test_model_milestones import Gateway
from tests.unit.runtime.autogen.test_model_tool_bridge import _durability, _tool


class EngineeringOptions(TypedDict, total=False):
    engineering_action: Callable[[bool], str | None]
    engineering_budget: Callable[[], tuple[int, float]]


async def test_real_runtime_binds_selector_and_participant_to_live_budget() -> None:
    observed: list[tuple[float, float]] = []

    class ObservedGateway(ScriptedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            budget = runtime._wall_budget
            assert budget is not None
            now = asyncio.get_running_loop().time()
            observed.append((request.timeout_seconds, min(
                budget.remaining(now=now), budget.absolute_remaining(now=now))))
            return await super().complete_with_context(request)

    gateway = ObservedGateway([
        ("analyst", 1, Decimal(0)),
        ("[COMPLETE] Explicit conclusion.", 2, Decimal(0)),
    ])
    plan = _plan(wall_time_seconds=450)
    context = _context(timeout_seconds=450)
    runtime = AutoGenDiscussionRuntime(gateway, plan)
    events = [event async for event in runtime.run(context)]

    assert events[-1].kind == EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == len(observed) == 2
    for timeout, remaining in observed:
        assert 60 < timeout <= 450
        assert remaining - 0.1 <= timeout <= remaining + 0.02
    assert gateway.requests[0].messages != gateway.requests[1].messages
    assert context.timeout_seconds == plan.wall_time_seconds == 450


async def test_new_calls_follow_existing_adaptive_progress_and_absolute_fuse() -> None:
    now = 0.0
    budget = AdaptiveDeadline.create(now=now, initial_seconds=120, soft_seconds=120,
                                    absolute_seconds=200, complexity_units=1)
    gateway = Gateway()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
        request_timeout=lambda: min(budget.remaining(now=now), budget.absolute_remaining(now=now)))
    await client.create([UserMessage(content="first", source="user")])
    now = 20.0
    budget.observe(progress_units=1, now=now)
    await client.create([UserMessage(content="second", source="user")])
    assert [request.timeout_seconds for request in gateway.requests] == [120, 180]
    assert budget.deadline == budget.absolute_deadline == 200


@pytest.mark.parametrize("remaining", [0.0, -1.0, float("nan"), float("inf"), True])
async def test_invalid_or_expired_deadline_sends_no_new_request(remaining: float) -> None:
    gateway, durable = Gateway(), _durability()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
        durability=durable, request_timeout=lambda: remaining)
    with pytest.raises(RuntimeExecutionError, match="deadline"):
        await client.create([UserMessage(content="new", source="user")])
    assert not gateway.requests
    assert not durable._model_lock.locked()


@pytest.mark.parametrize("after_checkpoint", [19.5, 0.0])
async def test_required_call_rechecks_deadline_after_durable_checkpoint(
    after_checkpoint: float,
) -> None:
    remaining = 450.0
    gateway, durable = Gateway(), _durability()
    publish = durable._publish

    async def delayed_publish(
        artifacts: tuple[Artifact, ...], model_entries: tuple[dict[str, JsonValue], ...],
        tool_entries: tuple[dict[str, JsonValue], ...],
    ) -> None:
        nonlocal remaining
        remaining = after_checkpoint
        await publish(artifacts, model_entries, tool_entries)

    durable._publish = delayed_publish
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
        durability=durable, request_timeout=lambda: remaining,
        engineering_action=lambda proposed: "project.inspect",
        engineering_budget=lambda: (100, remaining))
    if after_checkpoint:
        await client.create([UserMessage(content="build", source="user")],
                            tools=[_tool("project.inspect")])
        assert gateway.requests[0].timeout_seconds == after_checkpoint
        assert durable.model_entries[0]["timeout_seconds"] == 450
    else:
        with pytest.raises(RuntimeExecutionError, match="deadline"):
            await client.create([UserMessage(content="build", source="user")],
                                tools=[_tool("project.inspect")])
        assert not gateway.requests
    assert not durable._model_lock.locked()


async def test_followup_uses_remaining_deadline_without_rebilling_first_completion() -> None:
    remaining = 450.0

    class ConsumingGateway(Gateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            nonlocal remaining
            completion = await super().complete_with_context(request)
            remaining = 179.0
            return completion

    gateway, durable, usage = ConsumingGateway(), _durability(), DiscussionUsage()
    client = GatewayChatCompletionClient(gateway, "shared", usage, durability=durable,
        request_timeout=lambda: remaining,
        engineering_action=lambda proposed: "project.verify" if proposed else None,
        engineering_budget=lambda: (100 - usage.tokens, remaining))
    await client.create([UserMessage(content="build", source="user")], tools=[_tool("project.verify")])
    assert [(r.required_tool, r.timeout_seconds) for r in gateway.requests] == [
        (None, 450), ("project_verify", 179)]
    assert usage.tokens == 10
    assert all(row["status"] == "succeeded" for row in durable.model_entries)


@pytest.mark.parametrize("required", [False, True])
async def test_old_success_replays_even_when_current_deadline_is_expired(required: bool) -> None:
    gateway, durable, usage = Gateway(), _durability(), DiscussionUsage()
    options: EngineeringOptions = ({"engineering_action": lambda proposed: "project.inspect",
                "engineering_budget": lambda: (100, 5.0)} if required else {})
    messages = [UserMessage(content="build", source="user")]
    tools = [_tool("project.inspect")] if required else []
    original = GatewayChatCompletionClient(gateway, "shared", usage, durability=durable, **options)
    result = await original.create(messages, tools=tools)
    entries = tuple(dict(row) for row in durable.model_entries)
    restored = _durability(entries=entries, artifacts=durable.artifacts)
    resumed = GatewayChatCompletionClient(gateway, "shared", usage, durability=restored,
                                         request_timeout=lambda: 0.0, **options)
    replay = await resumed.create(messages, tools=tools)
    assert replay.content == result.content
    assert len(gateway.requests) == 1 and usage.tokens == 5
    assert tuple(restored.model_entries) == entries
    if required:
        assert restored.model_entries[0]["timeout_seconds"] == 5


@pytest.mark.parametrize("status", ["running", "UNKNOWN"])
async def test_unknown_outcome_is_rejected_before_new_deadline_evaluation(status: str) -> None:
    gateway, durable = Gateway(), _durability()
    messages = [UserMessage(content="build", source="user")]
    await GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
                                     durability=durable).create(messages)
    durable.model_entries[0]["status"] = status
    restored = _durability(entries=tuple(durable.model_entries), artifacts=durable.artifacts)
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), durability=restored,
                                        request_timeout=lambda: 0.0)
    with pytest.raises(ModelOutcomeUncertain if status == "running" else RuntimeExecutionError,
                       match="uncertain|ledger"):
        await client.create(messages)
    assert len(gateway.requests) == 1


async def test_optional_callback_preserves_legacy_default_and_checkpoint_hash() -> None:
    gateway = Gateway()
    await GatewayChatCompletionClient(gateway, "shared", DiscussionUsage()).create(
        [UserMessage(content="legacy", source="user")])
    request = gateway.requests[0]
    assert request.timeout_seconds == 60
    assert _DiscussionDurability.request_hash(request) == _DiscussionDurability.request_hash(
        replace(request, timeout_seconds=450))


async def test_cold_runtime_uses_tighter_budget_without_repeating_old_messages() -> None:
    repository = InMemoryArtifactRepository()
    plan = _plan(wall_time_seconds=450)
    context = _context(timeout_seconds=450)
    first_gateway = ScriptedGateway([
        ("analyst", 1, Decimal(0)), ("Retained observation", 2, Decimal(0)),
    ])
    first = AutoGenDiscussionRuntime(first_gateway, plan, artifact_repository=repository)
    stream = cast(AsyncGenerator[RunEvent, None], first.run(context))
    checkpoint = None
    try:
        while checkpoint is None:
            event = await anext(stream)
            if event.kind is EventKind.CHECKPOINT_SAVED:
                checkpoint = event.checkpoint
    finally:
        await stream.aclose()
    assert checkpoint is not None and checkpoint.state["terminal"] is False
    frozen = checkpoint.to_payload()
    gateway = ScriptedGateway([
        ("critic", 1, Decimal(0)), ("[COMPLETE] Final observation", 2, Decimal(0)),
    ])
    fresh = AutoGenDiscussionRuntime(gateway, plan, artifact_repository=repository)
    await fresh.restore_checkpoint(checkpoint)
    resumed = context.model_copy(update={"checkpoint": checkpoint, "timeout_seconds": 90.0})
    events = [event async for event in fresh.run(resumed)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert len(gateway.requests) == 2
    assert all(60 < request.timeout_seconds <= 90 for request in gateway.requests)
    assert any("Retained observation" in str(message.content)
               for message in gateway.requests[0].messages)
    assert checkpoint.to_payload() == frozen


async def test_cancellation_releases_lock_and_preserves_uncertain_outcome() -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    class BlockingGateway(Gateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("unreachable")

    gateway, durable = BlockingGateway(), _durability()
    token = CancellationToken()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
        durability=durable, request_timeout=lambda: 450.0)
    messages = [UserMessage(content="new", source="user")]
    pending = asyncio.create_task(client.create(messages, cancellation_token=token))
    await asyncio.wait_for(started.wait(), timeout=2)
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cancelled.is_set() and not durable._model_lock.locked()
    assert durable.model_entries[0]["status"] == "running"
    restored = _durability(entries=tuple(durable.model_entries), artifacts=durable.artifacts)
    resumed = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
        durability=restored, request_timeout=lambda: 30.0)
    with pytest.raises(ModelOutcomeUncertain):
        await resumed.create(messages)
    assert len(gateway.requests) == 1
