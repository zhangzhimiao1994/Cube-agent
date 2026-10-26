"""Task-scoped approval admission before engineering model or file side effects."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from typing import Protocol, cast
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.contracts import (
    EventKind,
    ExecutionRuntime,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.streams import closing_runtime_events

_TOOLS = ('workspace.write_text', 'project.verify', 'workspace.bundle')
_TYPE = 'engineering_admission'
_KEYS = {'phase', 'approval_id', 'tool_name', 'context_sha256', 'completed'}
_GATE_CODES = {
    'capability denied':'denied',
    'capability authorization unavailable':'gate_unavailable',
    'capability identity unavailable':'identity_unavailable',
    'tool unavailable':'tool_unavailable',
    'capability requires approval':'invalid_approval_receipt',
    'approval required':'approval_unavailable',
}


class _Authorizer(Protocol):
    async def authorize(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                        user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult: ...


def admission_checkpoint_valid(checkpoint: RuntimeCheckpoint, approval_id: str | None = None) -> bool:
    state = checkpoint.state
    return bool(checkpoint.runtime_type == _TYPE and checkpoint.runtime_version == '1'
        and checkpoint.mode in {TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID}
        and checkpoint.state_sha256 == checkpoint.recompute_state_sha256()
        and set(state) == _KEYS and state.get('phase') == 'before_model'
        and state.get('completed') is False and state.get('tool_name') in _TOOLS
        and isinstance(state.get('approval_id'),str)
        and re.fullmatch(r'approval_[A-Za-z0-9_-]{1,100}',cast(str,state['approval_id']))
        and (approval_id is None or state['approval_id'] == approval_id)
        and isinstance(state.get('context_sha256'),str)
        and re.fullmatch(r'[a-f0-9]{64}',cast(str,state['context_sha256'])))


def _required(context: TaskContext) -> bool:
    routing = context.routing_decision
    permissions = routing.get('requested_permissions')
    return bool(routing.get('sandbox_profile') == 'workspace_write'
        and isinstance(permissions,(tuple,list))
        and all(isinstance(item,str) for item in permissions)
        and {'workspace.read','workspace.write','command.run'}.issubset(permissions))


def _fingerprint(context: TaskContext) -> str:
    routing = context.routing_decision
    identity = {'request':context.request,'run_id':str(context.run_id),'tenant_id':str(context.tenant_id),
        'actor_id':str(context.actor_id),'actor_role':str(context.actor_role),'mode':str(context.mode),
        'project_id':routing.get('project_id'),'workspace_session_id':routing.get('workspace_session_id'),
        'sandbox_profile':routing.get('sandbox_profile'),'execution_backend':routing.get('execution_backend','systemd'),
        'requested_permissions':sorted(cast(list[str],routing.get('requested_permissions',[])))}
    return hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def with_engineering_admission(child: ExecutionRuntime, authorizer: object | None,
                               context: TaskContext, *, concrete_actions: bool = False) -> ExecutionRuntime:
    version = context.routing_decision.get('engineering_action_admission_version')
    legacy_pending = context.checkpoint is not None and context.checkpoint.runtime_type == _TYPE
    if (concrete_actions is True and type(version) is int and version == 1
            and context.mode in {TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID}
            and context.routing_decision.get('permission_mode') in {'default', 'auto_review'}
            and _required(context) and not legacy_pending):
        return child
    if _required(context) or (context.checkpoint is not None and context.checkpoint.runtime_type == _TYPE):
        return EngineeringAdmissionRuntime(child, authorizer)
    return child


class EngineeringAdmissionRuntime:
    def __init__(self, child: ExecutionRuntime, authorizer: object | None) -> None:
        self.mode = child.mode
        self._child, self._authorizer = child, authorizer
        self._restored: RuntimeCheckpoint | None = None
        self._last: RuntimeCheckpoint | None = None
        self._delegated = False
        self._child_started = False
        self._cancelled = False
        self._task: asyncio.Task[HarnessToolCallResult] | None = None

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        if context.mode is not self.mode:
            raise RuntimeError('engineering admission mode mismatch')
        if self._delegated or not _required(context):
            if self._restored is not None:
                raise RuntimeError('engineering admission checkpoint context mismatch')
        else:
            if type(context.actor_id) is not UUID or not isinstance(context.actor_role,Role):
                raise RuntimeError('engineering permission gate failed: identity_unavailable')
            if self._restored is not None:
                cp = self._restored
                if (not admission_checkpoint_valid(cp) or cp.run_id != context.run_id
                    or cp.tenant_id != context.tenant_id or cp.mode is not context.mode
                    or context.checkpoint is None or context.checkpoint.id != cp.id
                    or cp.state['context_sha256'] != _fingerprint(context)):
                    raise RuntimeError('engineering admission checkpoint context mismatch')
            elif context.checkpoint is not None:
                raise RuntimeError('engineering admission checkpoint was not restored')
            if not callable(getattr(self._authorizer,'authorize',None)):
                raise RuntimeError('engineering permission gate failed: gate_unavailable')
            loop = asyncio.get_running_loop()
            deadline = loop.time() + context.timeout_seconds
            for tool in _TOOLS:
                if self._cancelled:
                    yield RunEvent(kind=EventKind.RUNTIME_CANCELLED,sequence=1,run_id=context.run_id)
                    return
                request = HarnessToolCallRequest(run_id=context.run_id,actor='main_agent',tool_name=tool,
                    arguments={},approval_required=False,sandbox='workspace_write',
                    idempotency_key='engineering-admission-' + tool + '-' + context.run_id.hex)
                self._task = asyncio.create_task(cast(_Authorizer,self._authorizer).authorize(
                    context.tenant_id,request,user_id=context.actor_id,role=context.actor_role))
                try:
                    async with asyncio.timeout_at(deadline):
                        result = await self._task
                except asyncio.CancelledError:
                    if not self._cancelled:
                        raise
                    # Owned cancellation must return through the worker's normal
                    # terminal path so its execution lease is actually released.
                    yield RunEvent(kind=EventKind.RUNTIME_CANCELLED,sequence=1,run_id=context.run_id)
                    return
                finally:
                    if not self._task.done():
                        self._task.cancel()
                    await asyncio.gather(self._task,return_exceptions=True)
                    self._task = None
                if (not isinstance(result,HarnessToolCallResult) or result.truncated
                    or result.call_id != request.call_id or result.tool_name != tool):
                    raise RuntimeError('engineering permission gate failed: invalid_result')
                if result.status == 'succeeded' and result.payload.get('authorized') is True:
                    continue
                approval = result.payload.get('approval_id')
                if (result.status != 'failed' or result.failure_reason != 'capability requires approval'
                    or not isinstance(approval,str) or not re.fullmatch(r'approval_[A-Za-z0-9_-]{1,100}',approval)):
                    code = _GATE_CODES.get(result.failure_reason or '', 'unclassified')
                    raise RuntimeError('engineering permission gate failed: ' + code)
                self._last = RuntimeCheckpoint(id=uuid4(),runtime_type=_TYPE,runtime_version='1',
                    run_id=context.run_id,tenant_id=context.tenant_id,mode=context.mode,
                    state={'phase':'before_model','completed':False,'approval_id':approval,
                           'tool_name':tool,'context_sha256':_fingerprint(context)})
                yield RunEvent(kind=EventKind.CHECKPOINT_SAVED,sequence=1,run_id=context.run_id,
                    checkpoint=self._last)
                return
            # Admission receipts authorize nothing by themselves. Every actual
            # tool call still crosses the original policy gateway at execution.
            context = context.model_copy(update={'checkpoint':None})
        if self._cancelled:
            yield RunEvent(kind=EventKind.RUNTIME_CANCELLED,sequence=1,run_id=context.run_id)
            return
        self._child_started = True
        async with closing_runtime_events(self._child.run(context)) as events:
            async for event in events:
                yield event

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        checkpoint = RuntimeCheckpoint.from_payload(checkpoint.to_payload())
        if checkpoint.mode is not self.mode:
            raise RuntimeError('engineering admission checkpoint mode mismatch')
        if checkpoint.runtime_type == _TYPE:
            if not admission_checkpoint_valid(checkpoint):
                raise RuntimeError('engineering admission checkpoint invalid')
            self._restored = self._last = checkpoint
        else:
            await self._child.restore_checkpoint(checkpoint)
            self._delegated = True

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        if self._child_started:
            return await self._child.save_checkpoint()
        if self._last is None:
            raise RuntimeError('engineering admission checkpoint unavailable')
        return self._last

    async def cancel(self) -> None:
        self._cancelled = True
        if self._task is not None:
            self._task.cancel()
        if self._child_started:
            await self._child.cancel()
