"""Durable metadata flow; simulated Root, no native execution or installation."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
    PythonMetadataPreparationReceipt,
    PythonMetadataPreparationStatus,
)
from tests.unit.capabilities.test_python_metadata_preparation import setup
from tests.unit.skills.test_python_metadata_plan_protocol import request as planning_request


def case(*, existing: bool = True) -> Any:
    m, row, plan = setup()
    state = deepcopy(row.routing_decision[m.METADATA_KEY]) if existing else None
    events = []

    async def state_io(*, expected: Any = None, replace: Any = None) -> Any:
        nonlocal state
        if replace is not None:
            assert state == expected
            state = dict(m.validate_metadata_transition(state, replace))
            events.append(('state', state['phase']))
        return state

    async def authorize(actual: Any) -> None:
        assert actual == plan
        events.append(('authorize', plan.sha256))

    class Client:
        async def plan(self, invocation: Any, scope: Any, draft: Any) -> Any:
            assert invocation.install_allowed is False and invocation.sandbox_profile == 'restricted'
            events.append(('plan', invocation.timeout_seconds))
            return plan

        async def prepare(self, invocation: Any, actual: Any) -> Any:
            assert state is not None
            assert state['phase'] == 'executing' and state['execution_id'] == invocation.execution_id
            assert actual == plan
            events.append(('prepare', invocation.execution_id))
            return PythonMetadataPreparationReceipt(plan=plan, preparation_execution_id=invocation.execution_id,
                index_sha256='f' * 64, index_record_count=plan.catalog_count)

        async def inspect(self, invocation: Any, actual: Any, execution_id: str) -> Any:
            assert actual == plan
            events.append(('inspect', execution_id))
            return PythonMetadataPreparationStatus(state='prepared', plan_sha256=plan.sha256,
                preparation_execution_id=execution_id, receipt=PythonMetadataPreparationReceipt(
                    plan=plan, preparation_execution_id=execution_id, index_sha256='f' * 64,
                    index_record_count=plan.catalog_count))

    return SimpleNamespace(m=m, plan=plan, state=lambda: state, state_io=state_io,
        authorize=authorize, client=Client(), events=events,
        invocation=planning_request().invocation.model_copy(update={
            'tenant_id': plan.scope.tenant_id, 'run_id': plan.scope.run_id,
            'project_id': plan.scope.project_id, 'session_id': plan.scope.session_id}))


async def run(c: Any) -> Any:
    name = 'agent_hub.capabilities.python_metadata_preparation_flow'
    assert importlib.util.find_spec(name) is not None, 'missing durable metadata flow'
    return await importlib.import_module(name).prepare_python_metadata(
        client=c.client, invocation=c.invocation, scope=c.plan.scope, draft=c.plan.draft,
        state_io=c.state_io, authorize=c.authorize, deadline=asyncio.get_running_loop().time() + 20)


async def test_metadata_records_intent_before_root_and_receipt_after() -> None:
    c = case()
    result = await run(c)
    assert result['phase'] == 'prepared'
    assert [e[0] for e in c.events] == ['plan', 'authorize', 'state', 'prepare', 'state']
    assert [e[1] for e in c.events if e[0] == 'state'] == ['executing', 'prepared']


async def test_initial_plan_is_durable_before_approval() -> None:
    c = case(existing=False)
    result = await run(c)
    assert result['phase'] == 'prepared'
    assert [e[0] for e in c.events] == ['plan', 'state', 'authorize', 'state', 'prepare', 'state']
    assert c.events[1] == ('state', 'planned')


async def test_complete_root_index_does_not_create_mutation_or_fake_receipt() -> None:
    from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlan

    c = case(existing=False)

    async def ready(*args: Any) -> Any:
        from tests.unit.skills.test_python_metadata_plan_protocol import plan_fields

        return PythonMetadataPlan.model_validate(plan_fields('ready') | {
            'scope': c.plan.scope.model_dump(), 'draft': c.plan.draft.model_dump()})

    c.client.plan = ready
    assert await run(c) is None
    assert c.state() is None and not c.events


async def test_known_receipt_is_reused_without_download_or_replanning() -> None:
    c = case()
    previous = await run(c)
    c.events.clear()
    assert await run(c) == previous
    assert not c.events


async def test_cancelled_dispatch_remains_consumed_not_planned() -> None:
    c = case()

    async def cancel(*args: Any) -> Any:
        raise asyncio.CancelledError

    c.client.prepare = cancel
    with pytest.raises(asyncio.CancelledError):
        await run(c)
    assert c.state()['phase'] == 'executing'
    c.events.clear()
    await run(c)
    assert [event[0] for event in c.events] == ['inspect', 'state']


async def test_uncertain_dispatch_cold_recovery_only_inspects_same_execution() -> None:
    c = case()

    async def lost(*args: Any) -> Any:
        raise OSError('lost connection')

    c.client.prepare = lost
    with pytest.raises(OSError):
        await run(c)
    execution = c.state()['execution_id']
    assert c.state()['phase'] == 'executing'
    c.events.clear()
    result = await run(c)
    assert result['phase'] == 'prepared'
    assert c.events == [('inspect', execution), ('state', 'prepared')]


@pytest.mark.parametrize('status', ['absent', 'in_progress', 'unknown'])
async def test_unknown_never_downloads_or_resets_execution(status: Literal['absent', 'in_progress', 'unknown']) -> None:
    c = case()
    before = c.state()
    await c.state_io(expected=before, replace=before | {'phase': 'executing',
        'execution_id': 'metadata_prepare_' + 'a' * 32})
    c.events.clear()

    async def inspect(invocation: Any, plan: Any, execution: str) -> Any:
        return PythonMetadataPreparationStatus(state=status, plan_sha256=plan.sha256,
            preparation_execution_id=execution)

    c.client.inspect = inspect
    with pytest.raises(ValueError, match='metadata_prepare_unknown'):
        await run(c)
    assert c.state()['phase'] == 'executing' and not c.events


async def test_denial_preserves_planned_and_performs_no_root_mutation() -> None:
    c = case()

    async def deny(plan: Any) -> None:
        raise PermissionError('concrete metadata approval required')

    c.authorize = deny
    with pytest.raises(PermissionError):
        await run(c)
    assert c.state()['phase'] == 'planned' and [e[0] for e in c.events] == ['plan']


@pytest.mark.parametrize('defect', ['receipt_changed', 'extra_receipt'])
async def test_untrusted_root_result_never_commits_prepared(defect: str) -> None:
    c = case()
    old = c.client.prepare

    async def bad(*args: Any) -> Any:
        receipt = await old(*args)
        return receipt.model_copy(update={'plan': c.plan.model_copy(update={'catalog_sha256': '0' * 64})}
            if defect == 'receipt_changed' else {'extra': 'hidden'})
    c.client.prepare = bad
    with pytest.raises(ValueError):
        await run(c)
    assert c.state()['phase'] != 'prepared'


async def test_unexecuted_changed_plan_requires_fresh_exact_approval() -> None:
    c = case()
    new = c.plan.model_copy(update={'catalog_sha256': '0' * 64})

    async def changed(*args: Any) -> Any:
        return new

    async def authorize(plan: Any) -> None:
        assert plan == new and c.state()['plan_sha256'] == new.sha256
        raise PermissionError('old plan grant must not authorize this plan')

    c.client.plan, c.authorize = changed, authorize
    with pytest.raises(PermissionError):
        await run(c)
    assert c.state()['phase'] == 'planned' and c.state()['plan_sha256'] == new.sha256
    assert c.events == [('state', 'planned')]
