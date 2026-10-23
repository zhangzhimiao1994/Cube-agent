"""A provider must not start before the driver consumes the running checkpoint."""

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from agent_hub.capabilities.runtime import _builtin_input_schema
from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    RuntimeExecutionError,
)
from agent_hub.runtime.contracts import EventKind
from tests.unit.runtime.autogen.test_draft_action_progress import (
    action_context,
    draft_case_for_context,
)

POLICY = "engineering_checkpoint_ack_version"


class BlockingGateway:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()

    async def complete_with_context(self, request):
        self.calls.append(request)
        self.entered.set()
        await asyncio.Event().wait()


def fixture():
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
    return context, plan, gateway, AutoGenDiscussionRuntime(gateway, plan, capability_gateway=object())


async def pending_checkpoint(stream):
    async with asyncio.timeout(2):
        async for event in stream:
            if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None:
                return event.checkpoint
    raise AssertionError("running checkpoint was not published before provider entry")


async def test_close_before_checkpoint_ack_never_enters_provider_or_leaves_running_tasks():
    context, _, gateway, runtime = fixture()
    stream = runtime.run(context)
    try:
        checkpoint = await pending_checkpoint(stream)
        assert gateway.calls == []
        assert checkpoint.state[POLICY] == 1
        assert checkpoint.state["model_ledger"][0]["status"] == "running"
    finally:
        await stream.aclose()
    assert gateway.calls == [] and runtime._active_task is None


async def test_driver_resumption_is_the_only_ack_and_pending_restore_is_unknown():
    context, plan, gateway, runtime = fixture()
    stream = runtime.run(context)
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
    restored = AutoGenDiscussionRuntime(other, plan, capability_gateway=object(),
                                       artifact_repository=runtime._repository)
    await restored.restore_checkpoint(checkpoint)
    events = [event async for event in restored.run(context.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "model_outcome_uncertain"
    assert other.calls == []
    route = {key: value for key, value in context.routing_decision.items() if key != POLICY}
    with pytest.raises(RuntimeExecutionError, match="incompatible"):
        restored._validate_checkpoint(checkpoint, context.model_copy(update={"routing_decision": route}))


async def test_multiple_writers_publish_round_before_reselecting_with_checkpoint_ack():
    context, single_plan, _, _ = fixture()
    tools = single_plan.participants[0].allowed_tools
    plan = replace(single_plan, participants=tuple(
        replace(participant, allowed_tools=tools)
        for participant in single_plan.participants
    ), wall_time_seconds=5)

    class Backend:
        def is_available(self, tenant, name):
            return name in tools

        def is_replay_safe(self, name):
            return False

        def manifest(self, tenant):
            return tuple({"name": name, "input_schema": _builtin_input_schema(name)} for name in tools)

        async def invoke(self, tenant, request, **kwargs):
            assert request.tool_name == "project.inspect"
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status="succeeded", payload=draft_case_for_context(context))

    class Model:
        def __init__(self):
            self.selectors = 0

        async def complete_with_context(self, request):
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
    runtime = AutoGenDiscussionRuntime(model, plan, capability_gateway=backend,
                                       harness_tool_gateway=backend)
    stream = runtime.run(context)
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
