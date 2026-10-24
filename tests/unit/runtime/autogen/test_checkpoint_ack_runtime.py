"""A provider must not start before the driver consumes the running checkpoint."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine, Mapping
from dataclasses import replace
from decimal import Decimal
from types import CoroutineType
from typing import cast
from uuid import UUID

import pytest
from autogen_core import SingleThreadedAgentRuntime

from agent_hub.auth.models import Role
from agent_hub.capabilities.runtime import _builtin_input_schema
from agent_hub.db.session import Database
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    CapabilityGateway,
    DiscussionParticipant,
    DiscussionPlan,
    RuntimeExecutionError,
    _DiscussionDurability,
)
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.integration.runs.test_autogen_checkpoint_ack_native import (
    ForbiddenTools,
    InspectingModel,
)
from tests.integration.runs.test_autogen_checkpoint_ack_native import (
    plan as native_plan,
)
from tests.unit.runtime.autogen.test_draft_action_progress import (
    action_context,
    draft_case_for_context,
)

POLICY = "engineering_checkpoint_ack_version"


class BlockingGateway:
    def __init__(self) -> None:
        self.calls: list[ModelRequest] = []
        self.entered = asyncio.Event()

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.calls.append(request)
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking gateway unexpectedly released")


def fixture() -> tuple[TaskContext, DiscussionPlan, BlockingGateway, AutoGenDiscussionRuntime]:
    context = action_context()
    context = context.model_copy(update={"routing_decision": {
        **context.routing_decision, "engineering_writer_continuation_version": 1, POLICY: 1,
    }})
    plan = DiscussionPlan(participants=(
        DiscussionParticipant(id="implementer", role="Engineer", goal="Build", logical_model="writer",
            allowed_tools=("project.inspect", "workspace.list", "workspace.write_text", "project.verify", "workspace.bundle")),
        DiscussionParticipant(id="reviewer", role="Reviewer", goal="Review", logical_model="reviewer"),
    ), selector_model="selector", max_turns=8, token_budget=500, wall_time_seconds=0.15)
    gateway = BlockingGateway()
    return context, plan, gateway, AutoGenDiscussionRuntime(
        gateway, plan, capability_gateway=cast(CapabilityGateway, object()))


async def pending_checkpoint(stream: AsyncIterator[RunEvent]) -> RuntimeCheckpoint:
    async with asyncio.timeout(2):
        async for event in stream:
            if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None:
                return event.checkpoint
    raise AssertionError("running checkpoint was not published before provider entry")


async def test_close_before_checkpoint_ack_never_enters_provider_or_leaves_running_tasks() -> None:
    context, _, gateway, runtime = fixture()
    stream = cast(AsyncGenerator[RunEvent, None], runtime.run(context))
    try:
        checkpoint = await pending_checkpoint(stream)
        assert gateway.calls == []
        assert checkpoint.state[POLICY] == 1
        ledger = checkpoint.state["model_ledger"]
        assert isinstance(ledger, tuple)
        row = ledger[0]
        assert isinstance(row, Mapping)
        assert row["status"] == "running"
    finally:
        await stream.aclose()
    assert gateway.calls == [] and runtime._active_task is None


async def test_driver_resumption_is_the_only_ack_and_pending_restore_is_unknown() -> None:
    context, plan, gateway, runtime = fixture()
    stream = cast(AsyncGenerator[RunEvent, None], runtime.run(context))
    checkpoint = None
    resumed = None
    try:
        checkpoint = await pending_checkpoint(stream)
        assert gateway.calls == []
        resumed = asyncio.create_task(anext(stream))
        await asyncio.wait_for(gateway.entered.wait(), timeout=1)
        assert len(gateway.calls) == 1
    finally:
        if resumed is not None:
            resumed.cancel()
            await asyncio.gather(resumed, return_exceptions=True)
        await stream.aclose()
    assert checkpoint is not None
    other = BlockingGateway()
    restored = AutoGenDiscussionRuntime(other, plan, capability_gateway=cast(CapabilityGateway, object()),
                                       artifact_repository=runtime._repository)
    await restored.restore_checkpoint(checkpoint)
    events = [event async for event in restored.run(context.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "model_outcome_uncertain"
    assert other.calls == []
    route = {key: value for key, value in context.routing_decision.items() if key != POLICY}
    with pytest.raises(RuntimeExecutionError, match="incompatible"):
        restored._validate_checkpoint(checkpoint, context.model_copy(update={"routing_decision": route}))


async def test_multiple_writers_publish_round_before_reselecting_with_checkpoint_ack() -> None:
    context, single_plan, _, _ = fixture()
    tools = single_plan.participants[0].allowed_tools
    plan = replace(single_plan, participants=tuple(
        replace(participant, allowed_tools=tools)
        for participant in single_plan.participants
    ), wall_time_seconds=5)

    class Backend:
        def is_available(self, tenant_id: UUID, name: str) -> bool:
            return name in tools

        def is_replay_safe(self, name: str) -> bool:
            return False

        def manifest(self, tenant_id: UUID) -> tuple[Mapping[str, JsonValue], ...]:
            return tuple({"name": name, "input_schema": _builtin_input_schema(name)} for name in tools)

        async def invoke(
            self, tenant_id: UUID, request: HarnessToolCallRequest, *,
            user_id: UUID | None = None, role: Role | None = None,
        ) -> HarnessToolCallResult:
            assert request.tool_name == "project.inspect"
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status="succeeded", payload=draft_case_for_context(context))

    class Model:
        def __init__(self) -> None:
            self.selectors = 0

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if request.logical_model == "selector":
                self.selectors += 1
                response = ModelResponse(text="implementer", usage=TokenUsage(1, 1, 2))
            else:
                response = ModelResponse(text=None, usage=TokenUsage(1, 1, 2), tool_calls=(
                    ToolCall(id="inspect", name="project_inspect", arguments={}),))
            return GatewayCompletion(response=response, deployment_id="fixed",
                logical_model=request.logical_model, provider_id="fixed", provider_model="fixed",
                cost_usd=Decimal(0))

    backend, model = Backend(), Model()
    runtime = AutoGenDiscussionRuntime(model, plan, capability_gateway=cast(CapabilityGateway, backend),
                                       harness_tool_gateway=backend)
    stream = cast(AsyncGenerator[RunEvent, None], runtime.run(context))
    saved = None
    try:
        async with asyncio.timeout(1):
            async for event in stream:
                if (event.checkpoint is not None
                        and event.checkpoint.state.get("engineering_tool_prefix") == 1):
                    saved = event.checkpoint
                    break
    finally:
        await stream.aclose()
    assert saved is not None and model.selectors == 1


async def test_native_fixture_model_identity_matches_committed_tool_intent_without_io() -> None:
    context, _, _, _ = fixture()
    assert context.actor_id is not None
    model = InspectingModel(cast(Database, object()), context.tenant_id, context.actor_id)
    backend = ForbiddenTools()
    runtime = AutoGenDiscussionRuntime(model, native_plan(), capability_gateway=backend,
                                       harness_tool_gateway=backend)
    stream = cast(AsyncGenerator[RunEvent, None], runtime.run(context))
    saved = None
    framework = None
    dispatch = None
    try:
        async with asyncio.timeout(2):
            async for event in stream:
                if event.checkpoint is not None and event.checkpoint.state.get("tool_ledger"):
                    saved = event.checkpoint
                    assert runtime.last_team is not None
                    framework = cast(SingleThreadedAgentRuntime, runtime.last_team._runtime)
                    assert framework._run_context is not None
                    dispatch = framework._run_context._run_task
                    break
    finally:
        await stream.aclose()
    assert saved is not None and len(model.requests) == 2 and not backend.calls
    assert dispatch is not None and dispatch.done(), "owned AutoGen dispatch loop leaked after close"
    assert framework is not None and not any(not task.done() for task in framework._background_tasks)
    ledger = saved.state["model_ledger"]
    assert isinstance(ledger, tuple)
    for row, request in zip(ledger, model.requests, strict=True):
        assert isinstance(row, Mapping)
        names = {"project.inspect": "project.inspect",
                 "project_inspect": "project.inspect"} if request.tools else None
        assert row["request_hash"] == _DiscussionDurability.request_hash(request, tool_names=names)


async def test_incomplete_ack_cleanup_is_not_reported_as_a_successful_close() -> None:
    context, plan, model, _ = fixture()
    operations: list[str] = []

    class IncompleteCleanup(AutoGenDiscussionRuntime):
        async def _await_autogen_cleanup(self, operation: Coroutine[object, object, object]) -> bool:
            assert isinstance(operation, CoroutineType)
            operations.append(operation.cr_code.co_name)
            complete = await super()._await_autogen_cleanup(operation)
            if operation.cr_code.co_name == "drain_owned_handlers":
                # The fixture simulates a failed fence after real drain; stop its
                # already-idle dispatcher explicitly so the negative test leaks nothing.
                assert self.last_team is not None
                framework = cast(SingleThreadedAgentRuntime, self.last_team._runtime)
                await framework.stop_when_idle()
                await framework.close()
                return False
            return complete

    runtime = IncompleteCleanup(model, plan, capability_gateway=cast(CapabilityGateway, object()))
    stream = cast(AsyncGenerator[RunEvent, None], runtime.run(context))
    await pending_checkpoint(stream)
    with pytest.raises(ProcessCleanupIncomplete, match="cleanup incomplete"):
        await stream.aclose()
    assert not model.calls
    assert "reset" not in operations
