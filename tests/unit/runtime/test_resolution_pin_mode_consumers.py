"""Schema-3 reference pins survive real adapter feedback and checkpoint recovery."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.generated_project_draft import (
    GeneratedDraftRejection,
    validated_generated_draft_payload,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import RuntimeExecutionError
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.hybrid import HybridRuntime
from tests.unit.runtime import test_generated_draft_autogen as discuss
from tests.unit.runtime import test_generated_draft_crew as crew
from tests.unit.runtime.crew.test_adapter_failure_reason import WorkspaceDeliverySequenceGateway
from tests.unit.runtime.test_direct_generated_entry import Harness, Model, response
from tests.unit.runtime.test_generated_project_history import evidence

PIN: Mapping[str, JsonValue] = {
    "name": "flask", "version": "3.1.2", "filename": "flask-3.1.2-py3-none-any.whl",
    "sha256": "c3" * 32, "size_bytes": 103210,
}


def preflight(ctx: TaskContext) -> TaskContext:
    return ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "python_dependency_preflight_version": 1,
    }})


def failed_declaration(ctx: TaskContext, request: HarnessToolCallRequest) -> HarnessToolCallResult:
    project, session = (ctx.routing_decision[key] for key in ("project_id", "workspace_session_id"))
    assert isinstance(project, str) and isinstance(session, str)
    draft = GeneratedDraftRejection(
        tenant_id=str(ctx.tenant_id), user_id=str(ctx.actor_id), run_id=str(ctx.run_id),
        project_id=project, session_id=session,
        request_sha256=hashlib.sha256(ctx.request.encode()).hexdigest(),
        proposer_actor=request.actor, observation_sha256="a" * 64,
        reason="generated_acceptance_python_declaration_invalid", issues=("python_lock_empty",),
        schema_version=3, dependency_pins=(PIN,),
    )
    payload = validated_generated_draft_payload(draft.to_payload())
    assert payload is not None
    return HarnessToolCallResult(
        call_id=request.call_id, tool_name=request.tool_name, status="failed",
        failure_reason="generated project draft requires correction", payload={
            "generated_draft_rejection": payload, "call_id": request.call_id,
            "arguments_sha256": hashlib.sha256(b"{}").hexdigest(),
        },
    )


def assert_next_prompt(request: ModelRequest) -> None:
    text = "\n".join(message.content for message in request.messages
                     if isinstance(message.content, str))
    for value in ("dependency_pins", "python_lock_empty", "before_capture", "failed", "untrusted",
                  str(PIN["filename"]), str(PIN["sha256"]), str(PIN["version"])):
        assert value in text, value
    assert not any("dependency_pins" in message.content for message in request.messages
                   if message.role == "system" and isinstance(message.content, str))
    assert "secret://" not in text and "approval_id" not in text


class PinDirectHarness:
    def __init__(self, ctx: TaskContext, verified: Mapping[str, JsonValue],
                 bundle: Mapping[str, JsonValue]) -> None:
        self.delegate = Harness(ctx, verified, bundle)
        self.verified = verified
        self.calls = self.delegate.calls
        self.ctx = ctx
        self.attempts = 0

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        assert tenant_id == self.ctx.tenant_id and user_id == self.ctx.actor_id
        assert request.run_id == self.ctx.run_id and role is self.ctx.actor_role
        assert request.actor == "main_agent"
        if request.tool_name != "project.verify":
            return await self.delegate.invoke(tenant_id, request, user_id=user_id, role=role)
        self.calls.append(request.tool_name)
        self.attempts += 1
        if self.attempts == 1:
            return failed_declaration(self.ctx, request)
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                     status="succeeded", payload=self.verified)

    async def execute(self, *, name: str, **kwargs: object) -> Mapping[str, JsonValue]:
        return await self.delegate.execute(name=name, **kwargs)

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        return self.delegate.is_available(tenant_id, name)

    def is_replay_safe(self, name: str) -> bool:
        return self.delegate.is_replay_safe(name)


async def test_direct_next_model_request_keeps_schema3_reference_pins(tmp_path: Path) -> None:
    ctx, verified, bundle = await evidence(tmp_path)
    ctx = preflight(ctx.model_copy(update={"token_budget": 50_000, "routing_decision": {
        **ctx.routing_decision, "sandbox_profile": "workspace_write",
    }}))
    route = dict(ctx.routing_decision)
    harness = PinDirectHarness(ctx, verified, bundle)

    def batch(version: int) -> str:
        return json.dumps({"workspace_batch": {"files": {
            "ACCEPTANCE.json": f"draft-{version}", "src/app.py": "def total(): return 12\n",
        }, "complete": True, "continuation": ""}})

    model = Model(response(batch(1)), response(batch(2)))
    runner = DirectRuntime(model, logical_model="fixture", capability_gateway=harness,
                           harness_tool_gateway=harness)
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 2 and harness.attempts == 2
    assert_next_prompt(model.requests[1])
    assert harness.calls.count("project.inspect") == 1
    assert harness.calls[-1] == "workspace.bundle"
    failures = [event.artifact for event in events if event.artifact is not None
                and event.artifact.content.get("status") == "failed"]
    assert len(failures) == 1
    result = failures[0].content["result"]
    assert isinstance(result, Mapping) and set(result) == {
        "status", "untrusted", "generated_draft_rejection",
    }
    assert ctx.routing_decision == route and ctx.token_budget == 50_000


class PinCrewHarness(crew.DraftHarness):
    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        result = await super().invoke(tenant_id, request, user_id=user_id, role=role)
        if "generated_draft_rejection" in result.payload:
            return failed_declaration(self.context, request)
        return result


class UnusedChild:
    """A later Hybrid stage must not run after the real dispatch fails closed."""

    def __init__(self, mode: TaskMode) -> None:
        self.mode = mode

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        raise AssertionError("failed dispatch must not advance to a new child")

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("unused child has no checkpoint")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError("unused child must not restore")

    async def cancel(self) -> None:
        pass


@pytest.mark.parametrize("mode", [TaskMode.DISPATCH, TaskMode.HYBRID])
async def test_crew_next_request_keeps_pins_without_turning_denial_into_success(mode: TaskMode) -> None:
    ctx = preflight(crew.context()).model_copy(update={"mode": mode})
    route = dict(ctx.routing_decision)
    harness = PinCrewHarness(ctx)
    model = WorkspaceDeliverySequenceGateway((crew.verify(), crew.correction(), crew.verify("fresh")))
    dispatch = crew.runtime(model, harness)
    events: list[RunEvent] = []
    if mode is TaskMode.HYBRID:
        hybrid = HybridRuntime(dispatch, UnusedChild(TaskMode.DISCUSS), UnusedChild(TaskMode.DIRECT))
        events = [event async for event in hybrid.run(ctx)]
        assert events[-1].kind is EventKind.RUNTIME_FAILED
    else:
        with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
            async for event in dispatch.run(ctx):
                events.append(event)
    assert len(model.requests) == 3
    assert_next_prompt(model.requests[1])
    assert [call.tool_name for call in harness.calls] == [
        "project.verify", "workspace.write_text", "project.verify",
    ]
    assert harness.calls[0].call_id != harness.calls[2].call_id
    assert all(call.run_id == ctx.run_id and call.sandbox == "workspace_write" for call in harness.calls)
    assert not any(event.kind in {EventKind.STEP_COMPLETED, EventKind.RUNTIME_COMPLETED} for event in events)
    failed = [event for event in events if event.kind is EventKind.TOOL_FAILED]
    assert failed and not any(event.kind is EventKind.TOOL_COMPLETED
                              and event.tool_call_id == failed[0].tool_call_id for event in events)
    assert ctx.routing_decision == route and ctx.token_budget == 10_000


class PinDiscussGateway(discuss.DraftGateway):
    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        result = await super().invoke(tenant_id, request, user_id=user_id, role=role)
        if "generated_draft_rejection" in result.payload:
            result = replace(result, payload=failed_declaration(self.context, request).payload)
            self.rows[-1] = result
        return result


async def test_discuss_restored_failure_keeps_pins_in_next_model_request_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = preflight(discuss.context())
    route = dict(ctx.routing_decision)
    gateway = PinDiscussGateway(ctx)
    previous, checkpoint = await discuss.paused_checkpoint(monkeypatch, ctx, discuss.DraftModel(), gateway)
    assert len(gateway.calls) == 1
    model = discuss.DraftModel(stage=1)
    runner = discuss.runtime(model, gateway)
    runner._repository = previous._repository
    await runner.restore_checkpoint(checkpoint)
    events = await discuss.collect(runner, ctx.model_copy(update={"checkpoint": checkpoint}))
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    participant = next(request for request in model.requests if request.logical_model != "selector")
    assert_next_prompt(participant)
    assert [call.tool_name for call in gateway.calls] == [
        "project.verify", "workspace.write_text", "project.verify",
    ]
    assert gateway.calls[0].call_id != gateway.calls[2].call_id
    assert all(call.run_id == ctx.run_id and call.sandbox == "workspace_write" for call in gateway.calls)
    assert not any(event.kind is EventKind.TOOL_COMPLETED and event.tool_call_id == "draft-0"
                   for event in events)
    assert model.feedback
    for feedback in model.feedback:
        assert set(feedback) == {"status", "untrusted", "generated_draft_rejection"}
        draft = feedback["generated_draft_rejection"]
        assert draft["schema_version"] == 3 and draft["execution_started"] is False
        assert draft["dependency_pins"] == [dict(PIN)]
    assert ctx.routing_decision == route and ctx.token_budget == 500
