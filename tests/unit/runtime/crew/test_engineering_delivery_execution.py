"""Engineering completion cannot bypass delivery through a framework return."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities.test_project_verification import Executor, gateway
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file
from tests.unit.harness.test_tool_gateway import FakePolicyGateway
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    WorkspaceDeliverySequenceHarness,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
    _workspace_delivery_write_response,
)

TOOLS = ("workspace.write_text", "project.verify", "workspace.bundle", "project.inspect")
FINAL = ModelResponse(text="Done", usage=TokenUsage(1, 1, 2))


def verify_response(call_id: str = "verify") -> ModelResponse:
    return ModelResponse(text=None, tool_calls=(ToolCall(id=call_id, name="project.verify", arguments={}),),
                         usage=TokenUsage(1, 1, 2))


class ActualCapabilityHarness(WorkspaceDeliverySequenceHarness):
    def __init__(self, tmp_path: Path, executor: Executor) -> None:
        super().__init__()
        self.backend = gateway(tmp_path, executor)
        self.gateway = HarnessToolGateway(self.backend, policy_gateway=FakePolicyGateway(CapabilityStatus.ALLOWED))

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        self.calls.append(request)
        return await self.gateway.invoke(tenant_id, request, user_id=user_id, role=role)


def engineering_runtime(model: WorkspaceDeliverySequenceGateway, harness: WorkspaceDeliverySequenceHarness,
                        repository: InMemoryArtifactRepository) -> CrewDispatchRuntime:
    runner = _workspace_delivery_sequence_runtime(model, harness, artifact_repository=repository, tools=TOOLS)
    runner._plan = runner._plan.model_copy(update={"steps": (runner._plan.steps[0].model_copy(update={
        "task": "Build the requested project with a verified ZIP", "engineering_delivery_contract_version": 1,
    }),)})
    return runner


@pytest.mark.parametrize("version", (0, 1))
async def test_framework_early_return_is_gated_before_publishing(version: int, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _workspace_delivery_sequence_runtime(
        WorkspaceDeliverySequenceGateway(()), WorkspaceDeliverySequenceHarness(),
        tools=("workspace.write_text", "project.verify", "workspace.bundle"),
    )
    runner._plan = runner._plan.model_copy(update={"steps": (runner._plan.steps[0].model_copy(update={
        "task": "Build a project", "engineering_delivery_contract_version": version,
    }),)})

    async def premature(*args: object, **kwargs: object) -> tuple[GatewayCompletion, tuple[object, ...]]:
        return GatewayCompletion(
            response=ModelResponse(text="Done", usage=TokenUsage(1, 1, 2)),
            logical_model="general", deployment_id="primary", provider_id="deepseek",
            provider_model="deepseek/chat", cost_usd=Decimal(0),
        ), ()

    monkeypatch.setattr(runner, "_complete_agent", premature)
    task = _workspace_delivery_sequence_context()
    events = []
    if version == 1:
        with pytest.raises(RuntimeExecutionError, match="engineering delivery"):
            async for event in runner.run(task):
                events.append(event)
        assert not any(event.kind in {EventKind.ARTIFACT_CREATED, EventKind.STEP_COMPLETED,
                                      EventKind.RUNTIME_COMPLETED} for event in events)
    else:
        events = [event async for event in runner.run(task)]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_real_capability_delivery_with_protected_file_and_completed_replay(tmp_path: Path) -> None:
    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    protected = tmp_path / "projects" / project_path() / "protected.test.js"
    write_file(tmp_path / "projects", f"{project_path()}/protected.test.js", "original test")
    model = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("index.js", "module.exports = 42;", "write"), verify_response(),
        ModelResponse(text=None, tool_calls=(ToolCall(id="inspect", name="project.inspect", arguments={}),),
                      usage=TokenUsage(1, 1, 2)), _workspace_delivery_bundle_response(), FINAL,
    ))
    runner = engineering_runtime(model, harness, repository)
    task = _workspace_delivery_sequence_context().model_copy(update={"tenant_id": TENANT, "run_id": RUN})
    events = [event async for event in runner.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert protected.read_text() == "original test"
    checkpoint = await runner.save_checkpoint()
    calls = tuple(harness.calls)
    cached = WorkspaceDeliverySequenceGateway(())
    resumed = engineering_runtime(cached, harness, repository)
    await resumed.restore_checkpoint(checkpoint)
    events = [event async for event in resumed.run(task.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not cached.requests and tuple(harness.calls) == calls


async def test_native_summary_correction_is_bounded() -> None:
    model = WorkspaceDeliverySequenceGateway((FINAL, FINAL))
    harness = WorkspaceDeliverySequenceHarness()
    runner = engineering_runtime(model, harness, InMemoryArtifactRepository())
    with pytest.raises(RuntimeExecutionError, match="delivery stalled|bundle is missing"):
        async for _ in runner.run(_workspace_delivery_sequence_context()):
            pass
    assert len(model.requests) == 2 and not harness.calls
    assert any("write_text" in message.content and "project.verify" in message.content
               and "workspace.bundle" in message.content for message in model.requests[1].messages)


@pytest.mark.parametrize("pause_after_each_stage", (False, True))
async def test_failed_verify_then_real_write_verify_bundle_is_not_poisoned(
    tmp_path: Path, pause_after_each_stage: bool,
) -> None:
    class FailOnceExecutor(Executor):
        async def run(self, invocation: ProjectInvocation) -> SkillResult:
            self.failed = not self.calls
            return await super().run(invocation)

    executor = FailOnceExecutor()
    harness = ActualCapabilityHarness(tmp_path, executor)
    responses = [
        _workspace_delivery_write_response("index.js", "module.exports = 0;", "initial-write"),
        verify_response("failed-check"),
        _workspace_delivery_write_response("index.js", "module.exports = 42;", "repair-write"),
    ]
    if pause_after_each_stage:
        responses.append(FINAL)
    responses.append(verify_response("passed-check"))
    if pause_after_each_stage:
        responses.append(FINAL)
    responses.extend((_workspace_delivery_bundle_response(), FINAL))
    model = WorkspaceDeliverySequenceGateway(tuple(responses))
    runner = engineering_runtime(model, harness, InMemoryArtifactRepository())
    task = _workspace_delivery_sequence_context().model_copy(update={"tenant_id": TENANT, "run_id": RUN})
    events = [event async for event in runner.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(executor.calls) == 2
    assert len([event for event in events if event.kind is EventKind.TOOL_FAILED]) == 1
    assert (tmp_path / "projects" / project_path() / "index.js").read_text() == "module.exports = 42;"


async def test_completed_v1_checkpoint_without_delivery_is_rejected_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.runtime.crew import adapter

    repository = InMemoryArtifactRepository()
    harness = WorkspaceDeliverySequenceHarness()
    runner = engineering_runtime(WorkspaceDeliverySequenceGateway((FINAL,)), harness, repository)
    task = _workspace_delivery_sequence_context()
    # Simulate a completed v1 checkpoint from before the unified guard existed.
    with monkeypatch.context() as patch:
        patch.setattr(adapter, "engineering_delivery_ready", lambda **kwargs: True)
        events = [event async for event in runner.run(task)]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        checkpoint: RuntimeCheckpoint = await runner.save_checkpoint()
    cached = WorkspaceDeliverySequenceGateway(())
    resumed = engineering_runtime(cached, harness, repository)
    events = []
    with pytest.raises(RuntimeExecutionError, match="engineering delivery.*completed checkpoint"):
        await resumed.restore_checkpoint(checkpoint)
        async for event in resumed.run(task.model_copy(update={"checkpoint": checkpoint})):
            events.append(event)
    assert not cached.requests and not harness.calls
    assert not any(event.kind in {EventKind.STEP_COMPLETED, EventKind.ARTIFACT_CREATED,
                                  EventKind.RUNTIME_COMPLETED} for event in events)
