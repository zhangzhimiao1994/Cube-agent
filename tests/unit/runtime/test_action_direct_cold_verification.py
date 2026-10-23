"""Real action runtime/gateway/private storage; SQL and native report transport are doubles."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.runtime.worker_execution import worker_runtime_events
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities.test_durable_verification_review import PreparationRuns
from tests.unit.capabilities.test_generated_project_delivery import configured
from tests.unit.capabilities.test_scoped_read import project_path, write_file
from tests.unit.capabilities.test_verification_cold_recovery import Fixture
from tests.unit.runtime.test_direct_project_verification import Model, batch


class Policy:
    def __init__(self) -> None:
        self.allowed: set[str] = set()

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        if request.operation in self.allowed:
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        return CapabilityResult(CapabilityStatus.WAITING_APPROVAL, request.run_id,
            approval_id='approval_'+request.operation, reason='capability requires approval')


@pytest.mark.parametrize('generated', [False, True])
@pytest.mark.parametrize('mutate', [False, True])
async def test_concrete_write_verify_bundle_approvals_survive_fresh_workers_without_reexecution(tmp_path: Path, generated: bool, mutate: bool) -> None:
    fixture = Fixture(tmp_path)
    if generated:
        backend, source_runs = await configured(tmp_path/'generated-source')
        service = backend._project_verification
        assert service is not None and service.executor is not None
        fixture.root = service.root
        fixture.db.row.actor_id = source_runs.row.actor_id
        fixture.db.row.request = source_runs.row.request
        fixture.db.row.routing_decision = dict(source_runs.row.routing_decision)
        fixture.runs = PreparationRuns(fixture.db)
        source_executor = service.executor
        async def native(invocation: ProjectInvocation) -> SkillResult:
            fixture.executor.calls.append(invocation)
            return await source_executor.run(invocation)
        fixture.executor.run = native  # type: ignore[method-assign]
    fixture.db.row.routing_decision.update({'project_scale':'small', 'project_delivery':'workspace',
                                          'artifact_strategy':'workspace_bundle'})
    policy = Policy()
    policy.allowed.add('read')
    model = Model(batch({'README.md': 'Delivered from the original model response.'}))
    checkpoint: RuntimeCheckpoint | None = None
    gateways: list[RuntimeCapabilityGateway] = []
    published: dict[UUID, Artifact] = {}

    async def step() -> list[RunEvent]:
        gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path/'skills',
            project_workspace_dir=fixture.root, generated_artifact_dir=tmp_path/'generated',
            run_repository=fixture.runs, project_executor=fixture.executor,
            receipt_repository=fixture.db.repository(), artifact_repository=fixture.artifacts)
        gateways.append(gateway)
        runtime = ActionApprovedDirectRuntime(model, logical_model='main', capability_gateway=gateway,
            artifact_repository=fixture.artifacts, harness_tool_gateway=HarnessToolGateway(
                gateway, policy_gateway=policy, require_actor_identity=True))
        context = fixture.context().model_copy(update={'checkpoint': checkpoint, 'token_budget':50_000,
                                                       'artifacts':tuple(published.values())})
        if checkpoint is not None:
            await runtime.restore_checkpoint(checkpoint)
        async with worker_runtime_events(runtime, context, worker_id=fixture.db.row.worker_id) as stream:
            events = [event async for event in stream]
        for event in events:
            if event.artifact is not None:
                published[event.artifact.id] = event.artifact
        return events

    for expected, operation in [('workspace.write_text','write'), ('project.verify','run'), ('workspace.bundle','create')]:
        events = await step()
        checkpoint = events[-1].checkpoint
        assert checkpoint is not None and checkpoint.state['tool_name'] == expected, (
            expected, [(event.kind, event.tool_name, event.reason) for event in events])
        assert len(model.requests) == 1
        policy.allowed.add(operation)
        fixture.db.new_lease()
        if mutate:
            write_file(fixture.root, project_path()+'/manual.md', 'User changed workspace while paused.')
            with pytest.raises(RuntimeExecutionError, match='preparation context changed'):
                await step()
            assert len(model.requests) == 1 and fixture.executor.calls == []
            return
    events = await step()
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == len(fixture.executor.calls) == 1
    assert len(list((tmp_path/'generated').rglob('*.zip'))) == 1
    assert len(gateways) == 4
