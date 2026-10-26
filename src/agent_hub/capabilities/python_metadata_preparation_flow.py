"""Durable pre-capture metadata flow, distinct from supply and installation."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, cast
from uuid import uuid4

from agent_hub.capabilities.python_metadata_preparation import metadata_state_plan
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    PythonMetadataDraft,
    PythonMetadataPlan,
    PythonMetadataScope,
)
from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
    PythonMetadataPreparationReceipt,
    PythonMetadataPreparationStatus,
)


class MetadataPreparationClient(Protocol):
    async def plan(self, invocation: ProjectInvocation, scope: PythonMetadataScope,
                   draft: PythonMetadataDraft) -> PythonMetadataPlan: ...

    async def prepare(self, invocation: ProjectInvocation,
                      plan: PythonMetadataPlan) -> PythonMetadataPreparationReceipt: ...

    async def inspect(self, invocation: ProjectInvocation, plan: PythonMetadataPlan,
                      execution_id: str) -> PythonMetadataPreparationStatus: ...


class MetadataStateAccess(Protocol):
    async def __call__(self, *, expected: Mapping[str, JsonValue] | None = None,
                       replace: Mapping[str, JsonValue] | None = None) -> Mapping[str, JsonValue] | None: ...


async def prepare_python_metadata(
    *, client: MetadataPreparationClient, invocation: ProjectInvocation,
    scope: PythonMetadataScope, draft: PythonMetadataDraft, state_io: MetadataStateAccess,
    authorize: Callable[[PythonMetadataPlan], Awaitable[None]], deadline: float,
) -> Mapping[str, JsonValue] | None:
    """Once intent precedes Root dispatch; consumed executions are inspect-only."""
    if type(deadline) is not float or not math.isfinite(deadline):
        raise ValueError('metadata_prepare_timeout')
    scope = PythonMetadataScope.model_validate(scope, strict=True)
    draft = PythonMetadataDraft.model_validate(draft, strict=True)
    scope.require_invocation(invocation)

    def fresh(prefix: str) -> ProjectInvocation:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining < 1:
            raise ValueError('metadata_prepare_timeout')
        return ProjectInvocation.model_validate({**invocation.model_dump(),
            'execution_id': prefix + uuid4().hex, 'timeout_seconds': min(invocation.timeout_seconds, int(remaining)),
            'sandbox_profile': 'restricted', 'install_allowed': False}, strict=True)

    def bound(plan: PythonMetadataPlan) -> PythonMetadataPlan:
        if type(plan) is not PythonMetadataPlan:
            raise ValueError('metadata_prepare_request_invalid')
        plan = PythonMetadataPlan.model_validate(plan, strict=True)
        if plan.scope != scope or plan.draft != draft:
            raise ValueError('metadata_prepare_scope_changed')
        return plan

    async def current(expected: Mapping[str, JsonValue] | None) -> None:
        if await state_io() != expected:
            raise ValueError('metadata_prepare_state_changed')

    async with asyncio.timeout_at(deadline):
        state = await state_io()
        if state is not None:
            plan = metadata_state_plan(state)
            if plan.scope != scope:
                raise ValueError('metadata_prepare_scope_changed')
            if state['phase'] != 'planned':
                plan = bound(plan)
        if state is None or state['phase'] == 'planned':
            measured = bound(await client.plan(fresh('metadata_plan_'), scope, draft))
            await current(state)
            if measured.mode == 'ready':
                return None
            if state is None or plan != measured:
                plan = measured
                state = await state_io(expected=state, replace={'schema_version': 1, 'phase': 'planned',
                    'plan': cast(JsonValue, plan.model_dump(mode='json')), 'plan_sha256': plan.sha256})
                if state is None:
                    raise ValueError('metadata_prepare_state_changed')
            await authorize(plan)
            await current(state)
            execution = fresh('metadata_prepare_')
            state = await state_io(expected=state, replace={**state, 'phase': 'executing',
                'execution_id': execution.execution_id})
            if state is None:
                raise ValueError('metadata_prepare_state_changed')
            receipt = await client.prepare(execution, plan)
            await current(state)
        elif state['phase'] == 'executing':
            execution_id = cast(str, state['execution_id'])
            status = await client.inspect(fresh('metadata_inspect_'), plan, execution_id)
            await current(state)
            if type(status) is not PythonMetadataPreparationStatus:
                raise ValueError('metadata_prepare_request_invalid')
            status = PythonMetadataPreparationStatus.model_validate(status, strict=True)
            if status.plan_sha256 != plan.sha256 or status.preparation_execution_id != execution_id:
                raise ValueError('metadata_prepare_scope_changed')
            if status.state != 'prepared' or status.receipt is None:
                raise ValueError('metadata_prepare_unknown')
            receipt = status.receipt
        else:
            await current(state)
            return state
        if type(receipt) is not PythonMetadataPreparationReceipt:
            raise ValueError('metadata_prepare_request_invalid')
        receipt = PythonMetadataPreparationReceipt.model_validate(receipt, strict=True)
        if receipt.plan != plan or receipt.preparation_execution_id != state['execution_id']:
            raise ValueError('metadata_prepare_plan_changed')
        return await state_io(expected=state, replace={**state, 'phase': 'prepared',
            'receipt': cast(JsonValue, receipt.model_dump(mode='json'))})
