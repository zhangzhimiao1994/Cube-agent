from __future__ import annotations

from pathlib import Path

from agent_hub.models.types import ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
    _workspace_delivery_write_response,
)
from tests.unit.runtime.crew.test_engineering_delivery_execution import (
    FINAL,
    ActualCapabilityHarness,
    engineering_runtime,
    verify_response,
)


async def test_existing_project_must_reach_real_verify_and_bundle_without_cosmetic_writes(tmp_path: Path) -> None:
    model = WorkspaceDeliverySequenceGateway((
        ModelResponse(text=None, tool_calls=(ToolCall(id='inspect', name='project.inspect', arguments={}),),
            usage=TokenUsage(1, 1, 2)),
        verify_response(), FINAL, _workspace_delivery_bundle_response(), FINAL,
    ))
    harness = ActualCapabilityHarness(tmp_path, Executor())
    repository = InMemoryArtifactRepository()
    runner = engineering_runtime(model, harness, repository)
    runner._plan = runner._plan.model_copy(update={'steps': (
        runner._plan.steps[0].model_copy(update={'engineering_action_version': 1}),)})
    task = _workspace_delivery_sequence_context().model_copy(update={'run_id': RUN, 'tenant_id': TENANT})
    events = [event async for event in runner.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == ['project.inspect', 'project.verify', 'workspace.bundle']
    assert [request.required_tool for request in model.requests] == [
        'project_inspect', 'project_verify', None, 'workspace_bundle', None]
    for request in model.requests:
        if request.required_tool:
            assert [tool.name for tool in request.tools] == [request.required_tool]
    checkpoint = await runner.save_checkpoint()
    cached = WorkspaceDeliverySequenceGateway(())
    restored = engineering_runtime(cached, harness, repository)
    restored._plan = runner._plan
    await restored.restore_checkpoint(checkpoint)
    count = len(harness.calls)
    events = [event async for event in restored.run(task.model_copy(update={'checkpoint': checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not cached.requests and len(harness.calls) == count


async def test_actual_failed_check_reaches_model_then_changed_source_is_reverified(tmp_path: Path) -> None:
    source = tmp_path / 'projects' / project_path() / 'index.js'

    class SourceBoundExecutor(Executor):
        async def run(self, invocation: ProjectInvocation) -> SkillResult:
            self.failed = source.read_text(encoding='utf-8') != 'module.exports = 42;'
            return await super().run(invocation)

    executor = SourceBoundExecutor()
    harness = ActualCapabilityHarness(tmp_path, executor)
    write_file(tmp_path / 'projects', f'{project_path()}/index.js', 'module.exports = 0;')
    write_file(tmp_path / 'projects', f'{project_path()}/original.test.js', 'original checks')
    model = WorkspaceDeliverySequenceGateway((
        ModelResponse(text=None, tool_calls=(ToolCall(id='inspect', name='project.inspect', arguments={}),),
            usage=TokenUsage(1, 1, 2)), verify_response(),
        _workspace_delivery_write_response('index.js', 'module.exports = 42;', 'repair'),
        FINAL, verify_response('reverify'), FINAL, _workspace_delivery_bundle_response(), FINAL,
    ))
    runner = engineering_runtime(model, harness, InMemoryArtifactRepository())
    runner._plan = runner._plan.model_copy(update={'steps': (
        runner._plan.steps[0].model_copy(update={'engineering_action_version': 1}),)})
    task = _workspace_delivery_sequence_context().model_copy(update={'run_id': RUN, 'tenant_id': TENANT})
    events = [event async for event in runner.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(executor.calls) == 2
    assert [call.tool_name for call in harness.calls] == [
        'project.inspect', 'project.verify', 'workspace.write_text', 'project.verify', 'workspace.bundle']
    feedback = '\n'.join(message.content for message in model.requests[2].messages if isinstance(message.content, str))
    assert 'AssertionError' in feedback and 'SECRET' not in feedback
    assert (source.parent / 'original.test.js').read_text() == 'original checks'
    assert model.requests[2].required_tool is None
    assert model.requests[4].required_tool == 'project_verify'
    assert model.requests[6].required_tool == 'workspace_bundle'


async def test_successful_baseline_is_not_reused_after_source_changes(tmp_path: Path) -> None:
    model = WorkspaceDeliverySequenceGateway((
        ModelResponse(text=None, tool_calls=(ToolCall(id='inspect', name='project.inspect', arguments={}),),
            usage=TokenUsage(1, 1, 2)), verify_response('baseline'),
        _workspace_delivery_write_response('index.js', 'module.exports = 43;', 'change'),
        FINAL, verify_response('changed-source'), FINAL, _workspace_delivery_bundle_response(), FINAL,
    ))
    executor = Executor()
    harness = ActualCapabilityHarness(tmp_path, executor)
    runner = engineering_runtime(model, harness, InMemoryArtifactRepository())
    runner._plan = runner._plan.model_copy(update={'steps': (
        runner._plan.steps[0].model_copy(update={'engineering_action_version': 1}),)})
    task = _workspace_delivery_sequence_context().model_copy(update={'run_id': RUN, 'tenant_id': TENANT})
    events = [event async for event in runner.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(executor.calls) == 2
    assert [call.tool_name for call in harness.calls] == [
        'project.inspect', 'project.verify', 'workspace.write_text', 'project.verify', 'workspace.bundle']
