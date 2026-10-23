"""Exercise bootstrap and writer handoff through the real AutoGen team."""

import asyncio
import hashlib
from collections.abc import Mapping
from decimal import Decimal

import pytest

from agent_hub.capabilities.runtime import _builtin_input_schema
from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
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

POLICY = "engineering_writer_continuation_version"
TOOLS = ("project.inspect", "workspace.list", "workspace.write_text", "project.verify", "workspace.bundle")


async def exercise_writer_continuation(*, artifact_repository=None, restore_repository=None, context=None) -> None:
    ctx = context or action_context()
    ctx = ctx.model_copy(update={"routing_decision": {**ctx.routing_decision, POLICY: 1}})

    class Backend:
        def __init__(self):
            self.calls = []

        def is_available(self, tenant_id, name):
            return tenant_id == ctx.tenant_id and name in TOOLS

        def is_replay_safe(self, name):
            return False

        def manifest(self, tenant_id):
            return tuple({"name": name, "input_schema": _builtin_input_schema(name)} for name in TOOLS)

        async def invoke(self, tenant_id, request, *, user_id=None, role=None):
            assert tenant_id == ctx.tenant_id and user_id == ctx.actor_id and role == ctx.actor_role
            self.calls.append(request)
            if request.tool_name == "project.inspect":
                payload = draft_case_for_context(ctx)
            elif request.tool_name == "workspace.list":
                payload = {"workspace_files": ()}
            else:
                assert request.tool_name == "workspace.write_text"
                text = request.arguments["content"]
                payload = {"workspace_files": ({"path": request.arguments["path"],
                    "sha256": hashlib.sha256(text.encode()).hexdigest(), "size_bytes": len(text.encode())},)}
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status="succeeded", payload=payload)

    class Model:
        def __init__(self):
            self.requests: list[ModelRequest] = []
            self.stage = 0

        async def complete_with_context(self, request):
            self.requests.append(request)
            if request.logical_model == "selector":
                response = ModelResponse(text="implementer", usage=TokenUsage(1, 1, 2))
            else:
                stage = self.stage
                self.stage += 1
                if stage >= 4:
                    await asyncio.Event().wait()
                names = ("project.inspect", "workspace.list", "workspace.write_text")
                if stage < 3:
                    name = names[stage]
                    assert request.required_tool == name.replace(".", "_")
                    arguments = ({"path": "ACCEPTANCE.json", "content": "planned executable criteria"}
                                 if stage == 2 else {})
                    calls = (ToolCall(id=f"step-{stage}", name=request.required_tool, arguments=arguments),)
                else:
                    calls = tuple(ToolCall(id=f"file-{index}", name="workspace_write_text",
                        arguments={"path": path, "content": f"model authored {path}"})
                        for index, path in enumerate(("package.json", "tests/app.test.cjs", "src/app.cjs")))
                response = ModelResponse(text=None, tool_calls=calls, usage=TokenUsage(1, 1, 2))
            return GatewayCompletion(response=response, deployment_id="test", logical_model=request.logical_model,
                provider_id="test", provider_model="test/model", cost_usd=Decimal(0))

    backend, model = Backend(), Model()
    runner = AutoGenDiscussionRuntime(model, DiscussionPlan(participants=(
        DiscussionParticipant(id="implementer", role="Engineer", goal="Build", logical_model="writer",
                              allowed_tools=TOOLS),
        DiscussionParticipant(id="reviewer", role="Reviewer", goal="Review", logical_model="reviewer"),
    ), selector_model="selector", max_turns=8, token_budget=500), capability_gateway=backend,
        harness_tool_gateway=backend, artifact_repository=artifact_repository or InMemoryArtifactRepository())
    events = []
    stream = runner.run(ctx)
    try:
        async with asyncio.timeout(10):
            async for event in stream:
                events.append(event)
                if (event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
                    and event.checkpoint.state.get("engineering_tool_prefix") == 6):
                    break
    except TimeoutError:
        raise AssertionError({"calls": [call.tool_name for call in backend.calls],
            "model_stage": model.stage, "prefixes": [event.checkpoint.state.get("engineering_tool_prefix")
            for event in events if event.checkpoint is not None]}) from None
    finally:
        await stream.aclose()
    assert [call.tool_name for call in backend.calls] == [
        "project.inspect", "workspace.list", *(["workspace.write_text"] * 4)]
    assert [call.arguments["path"] for call in backend.calls if call.tool_name == "workspace.write_text"] == [
        "ACCEPTANCE.json", "package.json", "tests/app.test.cjs", "src/app.cjs"]
    assert sum(request.logical_model == "selector" for request in model.requests) == 1
    saved = next(event.checkpoint for event in reversed(events) if event.checkpoint is not None)
    assert saved.state[POLICY] == 1
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert all(call.actor == "implementer" and call.run_id == ctx.run_id for call in backend.calls)
    assert isinstance(saved.state["usage"], Mapping)
    runner._validate_checkpoint(saved, ctx)
    for value in (None, True, "1", 2):
        route = dict(ctx.routing_decision)
        if value is None:
            route.pop(POLICY)
        else:
            route[POLICY] = value
        with pytest.raises(RuntimeExecutionError, match="incompatible"):
            runner._validate_checkpoint(saved, ctx.model_copy(update={"routing_decision": route}))

    class ResumeModel:
        def __init__(self):
            self.requests = []

        async def complete_with_context(self, request):
            self.requests.append(request)
            if request.logical_model == "selector":
                response = ModelResponse(text="implementer", usage=TokenUsage(1, 1, 2))
            elif len([item for item in self.requests if item.logical_model != "selector"]) == 1:
                assert request.required_tool is None, "restoring a partial draft must not freeze it"
                response = ModelResponse(text=None, tool_calls=(ToolCall(id="continued-source",
                    name="workspace_write_text", arguments={"path": "src/extra.cjs", "content": "continued"}),),
                    usage=TokenUsage(1, 1, 2))
            else:
                await asyncio.Event().wait()
            return GatewayCompletion(response=response, deployment_id="test", logical_model=request.logical_model,
                provider_id="test", provider_model="test/model", cost_usd=Decimal(0))

    resumed_model = ResumeModel()
    restored = AutoGenDiscussionRuntime(resumed_model, runner._plan, capability_gateway=backend,
        harness_tool_gateway=backend, artifact_repository=restore_repository or runner._repository)
    await restored.restore_checkpoint(saved)
    stream = restored.run(ctx.model_copy(update={"checkpoint": saved}))
    try:
        async with asyncio.timeout(10):
            async for event in stream:
                if (event.checkpoint is not None
                    and event.checkpoint.state.get("engineering_tool_prefix") == 7):
                    break
    finally:
        await stream.aclose()
    assert len(backend.calls) == 7
    assert backend.calls[-1].arguments["path"] == "src/extra.cjs"


async def test_new_policy_authors_acceptance_before_multiple_files_without_reselecting_each_tool() -> None:
    await exercise_writer_continuation()


@pytest.mark.parametrize("reserved_writer", [False, True])
@pytest.mark.parametrize("multiple_writers", [False, True])
async def test_reserved_user_participant_is_rejected_before_any_model_or_publication_wait(
    reserved_writer, multiple_writers,
):
    ctx = action_context()
    ctx = ctx.model_copy(update={"routing_decision": {**ctx.routing_decision, POLICY: 1}})

    class NoModel:
        async def complete_with_context(self, request):
            raise AssertionError("reserved participants must be rejected before model dispatch")

    plan = DiscussionPlan(participants=(
        DiscussionParticipant(id="user" if reserved_writer else "implementer", role="Engineer",
            goal="Build", logical_model="writer", allowed_tools=TOOLS),
        DiscussionParticipant(id="reviewer" if reserved_writer else "user", role="Reviewer",
            goal="Review", logical_model="reviewer", allowed_tools=TOOLS if multiple_writers else ()),
    ), selector_model="selector", max_turns=8, token_budget=500, wall_time_seconds=0.1)
    runner = AutoGenDiscussionRuntime(NoModel(), plan, capability_gateway=object())
    async with asyncio.timeout(2):
        events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "engineering continuation participant is reserved"
    assert not any(event.checkpoint is not None for event in events)
    assert runner._active_task is None
