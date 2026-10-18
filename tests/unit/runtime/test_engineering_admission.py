import asyncio
from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.config.service import ConfigService
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import TaskQueue
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.defaults import CapacityFactory, HarnessToolInvoker
from agent_hub.runtime.engineering_admission import EngineeringAdmissionRuntime
from agent_hub.security.secrets import SecretService


class Child:
    def __init__(self, mode: TaskMode) -> None:
        self.mode = mode
        self.calls: list[TaskContext] = []
        self.restored: list[RuntimeCheckpoint] = []

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.calls.append(context)
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=context.run_id)

    async def cancel(self) -> None:
        pass

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise RuntimeError('no child checkpoint')

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        self.restored.append(checkpoint)


class Authorizer:
    def __init__(self) -> None:
        self.approved: set[str] = set()
        self.calls: list[tuple[UUID, HarnessToolCallRequest, UUID | None, Role | None]] = []

    async def authorize(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        self.calls.append((tenant_id, request, user_id, role))
        if request.tool_name not in self.approved:
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status='failed', failure_reason='capability requires approval',
                payload={'approval_id':'approval_' + request.tool_name.replace('.','_')})
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status='succeeded', payload={'authorized':True})


def context(mode: TaskMode) -> TaskContext:
    return TaskContext(run_id=uuid4(),tenant_id=UUID('00000000-0000-4000-8000-000000000001'),
        actor_id=uuid4(),actor_role=Role.OPERATOR,mode=mode,request='Build a Node.js billing module',
        routing_decision={'vibe_coding':True,'project_id':'billing','workspace_session_id':'one',
            'sandbox_profile':'workspace_write','execution_backend':'systemd',
            'requested_permissions':('workspace.read','workspace.write','command.run','network.read')})


@pytest.mark.parametrize('mode',[TaskMode.DIRECT,TaskMode.DISPATCH,TaskMode.DISCUSS,TaskMode.HYBRID])
async def test_all_modes_wait_before_child_and_resume_after_each_exact_scope(mode: TaskMode) -> None:
    original=context(mode)
    authorizer=Authorizer()
    cp: RuntimeCheckpoint | None = None
    for expected in ('workspace.write_text','project.verify','workspace.bundle'):
        child=Child(mode)
        runtime=EngineeringAdmissionRuntime(child,authorizer)
        current=original.model_copy(update={'checkpoint':cp})
        if cp:
            await runtime.restore_checkpoint(cp)
        events=[e async for e in runtime.run(current)]
        assert child.calls==[]
        assert len(events)==1 and events[0].kind is EventKind.CHECKPOINT_SAVED
        cp=events[0].checkpoint
        assert cp is not None and cp.state['tool_name']==expected
        assert isinstance(cp.state['approval_id'], str)
        assert checkpoint_waits_for_approval(cp,cp.state['approval_id'])
        assert await runtime.save_checkpoint()==cp
        authorizer.approved.add(expected)
    child=Child(mode)
    runtime=EngineeringAdmissionRuntime(child,authorizer)
    assert cp is not None
    await runtime.restore_checkpoint(cp)
    events=[e async for e in runtime.run(original.model_copy(update={'checkpoint':cp}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(child.calls)==1 and child.calls[0].checkpoint is None
    assert child.calls[0].actor_id==original.actor_id
    assert child.calls[0].actor_role is Role.OPERATOR
    assert child.calls[0].routing_decision==original.routing_decision
    assert all(c[2]==original.actor_id and c[3] is Role.OPERATOR for c in authorizer.calls)


async def test_pending_receipt_cannot_authorize_changed_actor_or_request() -> None:
    original=context(TaskMode.DIRECT)
    runtime=EngineeringAdmissionRuntime(Child(original.mode),Authorizer())
    events=[e async for e in runtime.run(original)]
    cp=events[0].checkpoint
    assert cp
    for update in ({'actor_id':uuid4()},{'request':'Different task'},{'tenant_id':uuid4()}):
        child=Child(original.mode)
        gateway=Authorizer()
        resumed=EngineeringAdmissionRuntime(child,gateway)
        await resumed.restore_checkpoint(cp)
        with pytest.raises(RuntimeError,match='admission checkpoint'):
            _=[e async for e in resumed.run(original.model_copy(update={**update,'checkpoint':cp}))]
        assert child.calls==[] and gateway.calls==[]


async def test_nonengineering_context_does_not_prompt_or_modify_checkpoint() -> None:
    original=context(TaskMode.DIRECT).model_copy(update={'routing_decision':{}})
    child=Child(original.mode)
    gateway=Authorizer()
    runtime=EngineeringAdmissionRuntime(child,gateway)
    _=[e async for e in runtime.run(original)]
    assert gateway.calls==[] and child.calls==[original]


async def test_denial_and_malformed_admission_fail_closed_without_child() -> None:
    original=context(TaskMode.DIRECT)
    class Denied:
        async def authorize(
            self, tenant_id: UUID, request: HarnessToolCallRequest, **kwargs: object,
        ) -> HarnessToolCallResult:
            return HarnessToolCallResult(call_id=request.call_id,tool_name=request.tool_name,
                status='failed',failure_reason='capability denied',payload={})
    child=Child(original.mode)
    runtime=EngineeringAdmissionRuntime(child,Denied())
    with pytest.raises(RuntimeError,match='permission gate failed: denied'):
        _=[e async for e in runtime.run(original)]
    assert child.calls==[]


async def test_permission_failure_keeps_safe_machine_diagnostic() -> None:
    from agent_hub.runtime.failure_reason import safe_runtime_failure_diagnostic
    original=context(TaskMode.DIRECT)
    class Denied:
        async def authorize(
            self, tenant_id: UUID, request: HarnessToolCallRequest, **kwargs: object,
        ) -> HarnessToolCallResult:
            return HarnessToolCallResult(call_id=request.call_id,tool_name=request.tool_name,
                status='failed',failure_reason='capability authorization unavailable',payload={})
    runtime=EngineeringAdmissionRuntime(Child(original.mode),Denied())
    with pytest.raises(RuntimeError) as caught:
        _=[e async for e in runtime.run(original)]
    diagnostic=safe_runtime_failure_diagnostic(caught.value)
    assert diagnostic['error_code']=='runtime.engineering_permission.gate_unavailable'
    assert diagnostic['error_summary']=='engineering permission gate failed: gate_unavailable'


async def test_child_checkpoint_is_delegated_not_replayed_as_admission() -> None:
    original=context(TaskMode.DIRECT)
    child=Child(original.mode)
    gateway=Authorizer()
    cp=RuntimeCheckpoint(id=uuid4(),runtime_type='direct',runtime_version='1',
        run_id=original.run_id,tenant_id=original.tenant_id,mode=original.mode,state={'completed':True})
    runtime=EngineeringAdmissionRuntime(child,gateway)
    await runtime.restore_checkpoint(cp)
    _=[e async for e in runtime.run(original.model_copy(update={'checkpoint':cp}))]
    assert gateway.calls==[] and child.restored==[cp] and child.calls[0].checkpoint==cp


@pytest.mark.parametrize('mode',[TaskMode.DIRECT,TaskMode.DISPATCH,TaskMode.DISCUSS,TaskMode.HYBRID])
async def test_production_config_backed_runtime_wraps_admission_before_child(
    mode: TaskMode, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.runtime.defaults import (
        ConfigBackedDirectRuntime,
        ConfigBackedDiscussionRuntime,
        ConfigBackedDispatchRuntime,
        ConfigBackedHybridRuntime,
    )
    cls: type[
        ConfigBackedDirectRuntime | ConfigBackedDispatchRuntime |
        ConfigBackedDiscussionRuntime | ConfigBackedHybridRuntime
    ]
    cls={TaskMode.DIRECT:ConfigBackedDirectRuntime,TaskMode.DISPATCH:ConfigBackedDispatchRuntime,
        TaskMode.DISCUSS:ConfigBackedDiscussionRuntime,TaskMode.HYBRID:ConfigBackedHybridRuntime}[mode]
    authorizer=Authorizer()
    runtime=cls(
        config_service=cast(ConfigService, None),secret_service=cast(SecretService, None),
        capacity_factory=cast(CapacityFactory, None),
        harness_tool_gateway=cast(HarnessToolInvoker, authorizer))
    child=Child(mode)
    async def build(context: TaskContext) -> Child:
        return child
    monkeypatch.setattr(runtime,'_runtime_for',build)
    original=context(mode)
    cp: RuntimeCheckpoint | None = None
    for expected in ('workspace.write_text','project.verify','workspace.bundle'):
        if cp:
            await runtime.restore_checkpoint(cp)
        events=[e async for e in runtime.run(original.model_copy(update={'checkpoint':cp}))]
        cp=events[0].checkpoint
        assert cp and cp.state['tool_name']==expected and child.calls==[]
        authorizer.approved.add(expected)
    assert cp is not None
    await runtime.restore_checkpoint(cp)
    events=[e async for e in runtime.run(original.model_copy(update={'checkpoint':cp}))]
    assert len(child.calls)==1 and child.calls[0].checkpoint is None
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_forged_admission_receipt_does_not_grant_tools() -> None:
    original=context(TaskMode.DIRECT)
    first=EngineeringAdmissionRuntime(Child(original.mode),Authorizer())
    events=[e async for e in first.run(original)]
    cp=events[0].checkpoint
    assert cp
    state={**dict(cp.state),'tool_name':'workspace.bundle','approval_id':'approval_invented'}
    forged=RuntimeCheckpoint(id=uuid4(),runtime_type=cp.runtime_type,runtime_version=cp.runtime_version,
        run_id=cp.run_id,tenant_id=cp.tenant_id,mode=cp.mode,state=state)
    gateway=Authorizer()
    child=Child(original.mode)
    resumed=EngineeringAdmissionRuntime(child,gateway)
    await resumed.restore_checkpoint(forged)
    events=[e async for e in resumed.run(original.model_copy(update={'checkpoint':forged}))]
    assert events[0].checkpoint is not None
    assert child.calls==[] and events[0].checkpoint.state['tool_name']=='workspace.write_text'
    assert gateway.calls[0][1].tool_name=='workspace.write_text'


@pytest.mark.parametrize('mode',[TaskMode.DIRECT,TaskMode.DISPATCH,TaskMode.DISCUSS,TaskMode.HYBRID])
@pytest.mark.parametrize('driver_uuid',[False,True])
async def test_worker_persists_admission_before_releasing_waiting_execution(
    mode: TaskMode, monkeypatch: pytest.MonkeyPatch, driver_uuid: bool,
) -> None:
    from agent_hub.domain.runs import RunStatus
    from agent_hub.runs.service import RunService
    from agent_hub.runtime.registry import RuntimeRegistry
    from tests.unit.runs import test_approval_checkpoint_service as fixture

    monkeypatch.setattr(fixture,'APPROVAL_ID','approval_current')
    repository=fixture.ApprovalRepository()
    repository.row.mode=mode.value
    repository.row.actor_role=Role.OPERATOR.value
    repository.row.routing_decision=dict(context(mode).routing_decision)
    if driver_uuid:
        from asyncpg.pgproto.pgproto import UUID as DriverUUID  # type: ignore[import-untyped]

        repository.row.actor_id=DriverUUID(str(repository.row.actor_id))
        repository.row.tenant_id=DriverUUID(str(repository.row.tenant_id))
    class Pending(Authorizer):
        async def authorize(
            self, tenant_id: UUID, request: HarnessToolCallRequest, **kwargs: object,
        ) -> HarnessToolCallResult:
            repository.begin_approval()
            return HarnessToolCallResult(call_id=request.call_id,tool_name=request.tool_name,
                status='failed',failure_reason='capability requires approval',payload={'approval_id':'approval_current'})
    child=Child(mode)
    runtime=EngineeringAdmissionRuntime(child,Pending())
    service=RunService(cast(RunRepository, repository),runtime_registry=RuntimeRegistry((runtime,)),
        router=None,task_queue=cast(TaskQueue, object()))
    execution_id=DriverUUID(str(repository.run_id)) if driver_uuid else repository.run_id
    submitted=await service.execute(execution_id)
    assert submitted.status is RunStatus.WAITING_APPROVAL
    assert child.calls==[]
    assert checkpoint_waits_for_approval(repository.checkpoint,'approval_current')
    assert repository.row.worker_lease_token is None


async def test_owned_cancellation_during_authorization_releases_worker_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.domain.runs import RunStatus
    from agent_hub.runs.service import RunService
    from agent_hub.runtime.defaults import ConfigBackedDirectRuntime
    from agent_hub.runtime.registry import RuntimeRegistry
    from tests.unit.runs.test_approval_checkpoint_service import ApprovalRepository

    repository=ApprovalRepository()
    repository.row.mode=TaskMode.DIRECT.value
    repository.row.actor_role=Role.OPERATOR.value
    repository.row.routing_decision=dict(context(TaskMode.DIRECT).routing_decision)
    started=asyncio.Event()
    class Blocked:
        async def authorize(self,*args: object,**kwargs: object) -> None:
            started.set()
            await asyncio.Event().wait()
    child=Child(TaskMode.DIRECT)
    runtime=ConfigBackedDirectRuntime(config_service=cast(ConfigService, None),
        secret_service=cast(SecretService, None),capacity_factory=cast(CapacityFactory, None),
        harness_tool_gateway=cast(HarnessToolInvoker, Blocked()))
    async def build(context: TaskContext) -> Child:
        return child
    monkeypatch.setattr(runtime,'_runtime_for',build)
    service=RunService(cast(RunRepository, repository),runtime_registry=RuntimeRegistry((runtime,)),
        router=None,task_queue=cast(TaskQueue, object()))
    task=asyncio.create_task(service.execute(repository.run_id))
    try:
        async with asyncio.timeout(2):
            await started.wait()
            token=repository.row.worker_lease_token
            assert token
            repository.row.status=RunStatus.CANCELLED.value
            await runtime.cancel_run_owned(repository.run_id,token)
            result=await task
        assert result.status is RunStatus.CANCELLED
        assert repository.row.worker_lease_token is None
        assert child.calls==[] and not runtime._active
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task,return_exceptions=True)
