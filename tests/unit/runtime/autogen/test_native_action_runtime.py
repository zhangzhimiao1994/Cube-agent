"""Real framework runs execute named milestone calls through Harness only."""

import asyncio
from collections.abc import AsyncGenerator, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import cast

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent
from tests.unit.runtime.autogen.test_engineering_actions import action_context, inspection
from tests.unit.runtime.autogen.test_engineering_completion import Capabilities, plan
from tests.unit.runtime.autogen.test_model_milestones import Gateway


class ScopedModel(Gateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.logical_model == "selector":
            self.requests.append(request)
            return GatewayCompletion(response=ModelResponse(text="engineer", usage=TokenUsage(
                prompt_tokens=1, completion_tokens=1, total_tokens=2)),
                deployment_id="shared", logical_model="selector", provider_id="test",
                provider_model="test/model", cost_usd=Decimal(0))
        return await super().complete_with_context(request)


async def test_existing_source_inspect_baseline_verify_and_bundle_without_write() -> None:
    ctx = action_context()
    configured = plan()
    owner = replace(configured.participants[0], allowed_tools=(
        *configured.participants[0].allowed_tools, "project.inspect"))
    configured = replace(configured, participants=(owner, configured.participants[1]), selector_model="selector")
    backend = Capabilities()
    backend.results["project.inspect"] = inspection(ctx)

    gateway = ScopedModel()
    runner = AutoGenDiscussionRuntime(gateway, configured, capability_gateway=backend)
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert backend.calls == ["project.inspect", "project.verify", "workspace.bundle"]
    owner_requests = [request for request in gateway.requests if request.logical_model != "selector"]
    assert [request.required_tool for request in owner_requests] == [
        "project_inspect", "project_verify", None, "workspace_bundle", None,
    ]
    assert all(len(request.tools) == 1 for request in owner_requests if request.required_tool is not None)
    checkpoint = await runner.save_checkpoint()
    assert checkpoint.state["engineering_action_version"] == 1
    restored = AutoGenDiscussionRuntime(ScopedModel(), configured, capability_gateway=backend,
                                        artifact_repository=runner._repository)
    await restored.restore_checkpoint(checkpoint)
    events = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(backend.calls) == 3
    downgraded_model = ScopedModel()
    downgraded = AutoGenDiscussionRuntime(downgraded_model, configured, capability_gateway=backend,
                                          artifact_repository=runner._repository)
    await downgraded.restore_checkpoint(checkpoint)
    routing = {key: value for key, value in ctx.routing_decision.items() if key != "engineering_action_version"}
    events = [event async for event in downgraded.run(ctx.model_copy(update={
        "checkpoint": checkpoint, "routing_decision": routing,
    }))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED and not downgraded_model.requests


async def test_resume_after_writes_requires_verify_without_rereading_or_rewriting() -> None:
    class WritingModel(ScopedModel):
        written = False

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if request.logical_model != "selector" and request.required_tool is None:
                if self.written:
                    await asyncio.Event().wait()
                self.written = True
                self.requests.append(request)
                return GatewayCompletion(response=ModelResponse(text=None, tool_calls=(ToolCall(
                    id="provider_write", name="workspace_write_text", arguments={}),),
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)),
                    deployment_id="shared", logical_model=request.logical_model, provider_id="test",
                    provider_model="test/model", cost_usd=Decimal(0))
            return await super().complete_with_context(request)

    ctx = action_context()
    configured = plan()
    configured = replace(configured, selector_model="selector", participants=(
        replace(configured.participants[0], allowed_tools=(
            *configured.participants[0].allowed_tools, "project.inspect")), configured.participants[1],
    ))
    backend = Capabilities()
    backend.results["project.inspect"] = inspection(ctx, existing=False)
    runner = AutoGenDiscussionRuntime(WritingModel(), configured, capability_gateway=backend)
    stream = cast(AsyncGenerator[RunEvent, None], runner.run(ctx))
    saved = None
    try:
        async for event in stream:
            if event.checkpoint is not None and event.checkpoint.state.get("engineering_tool_prefix") == 2:
                saved = event.checkpoint
                break
    finally:
        await stream.aclose()
    assert saved is not None and backend.calls == ["project.inspect", "workspace.write_text"]
    model = ScopedModel()
    resumed = AutoGenDiscussionRuntime(model, configured, capability_gateway=backend,
                                       artifact_repository=runner._repository)
    await resumed.restore_checkpoint(saved)
    events = [event async for event in resumed.run(ctx.model_copy(update={"checkpoint": saved}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert [request.required_tool for request in model.requests if request.logical_model != "selector"] == [
        "project_verify", None, "workspace_bundle", None,
    ]
    assert backend.calls == ["project.inspect", "workspace.write_text", "project.verify", "workspace.bundle"]


def test_failed_verification_is_not_forced_again_until_acknowledged_source_changes() -> None:
    from agent_hub.runtime.autogen.engineering_delivery import engineering_progress
    from agent_hub.runtime.engineering_actions import required_engineering_action
    from tests.unit.runtime.autogen.test_engineering_delivery import TOOLS, append_result, fixture

    ctx = action_context()
    rows, artifacts = fixture()
    write = artifacts[0].content["result"]
    assert isinstance(write, Mapping)
    rows.clear()
    artifacts.clear()
    append_result(rows, artifacts, "project.inspect", inspection(ctx, existing=False))
    append_result(rows, artifacts, "workspace.write_text", write)
    append_result(rows, artifacts, "project.verify", {"status": "failed"}, status="failed")
    tools = {"engineer": (*TOOLS["engineer"], "project.inspect")}

    def action() -> str | None:
        return required_engineering_action(engineering_progress(
            ctx, tool_entries=rows, artifacts=artifacts, allowed_tools=tools, completion_proposed=True))

    assert action() is None
    append_result(rows, artifacts, "workspace.write_text", write)
    assert action() is None
    changed: JsonValue = {"path": "index.js", "sha256": "e" * 64, "size_bytes": 12}
    append_result(rows, artifacts, "workspace.write_text", {"workspace_files": (changed,)})
    assert action() == "project.verify"
