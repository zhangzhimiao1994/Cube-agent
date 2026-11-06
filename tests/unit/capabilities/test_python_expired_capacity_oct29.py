"""Service DB-lock and immutable-reader fixtures; no Root deletion or execution."""
from __future__ import annotations

import asyncio
import contextlib
import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID

import pytest

from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
from tests.unit.capabilities.capability_lifecycle_typed_fixture import (
    UnusedContextReader,
    UnusedReleaser,
)
from tests.unit.capabilities.test_python_project_service import configured
from tests.unit.skills.sandbox.test_python_project_client import invocation


class Session:
    def __init__(self) -> None:
        self.row = SimpleNamespace(id=UUID(int=2), tenant_id=UUID(int=1), status='failed',
            worker_id=None, worker_lease_token=None, worker_lease_expires_at=None,
            worker_heartbeat_at=None, updated_at=datetime.now(UTC)-timedelta(days=2), version=1,
            routing_decision={'project_id':'demo','workspace_session_id':'one',
                'sandbox_profile':'workspace_write','requested_permissions':['workspace.read'],
                'project_input_preparation':{'schema_version':2,'phase':'executing',
                    'execution_id':'original_failed',
                    'reference':invocation().original_reference.model_dump(mode='json'),
                    'python_recipe':{'schema_version':1,'recipe_sha256':invocation().recipe_sha256,
                                     'environment_input_sha256':invocation().environment_input_sha256}}})
        self.open = False
        self.queries: list[Any] = []
        self.block_table: str | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    @asynccontextmanager
    async def begin(self) -> Any:
        self.open = True
        try:
            yield
        finally:
            self.open = False

    async def scalar(self, query: Any) -> Any:
        self.queries.append(query)
        if query.column_descriptions[0].get('entity').__name__ == 'RunRow':
            assert query._for_update_arg is not None
            return self.row
        if query.column_descriptions[0]['entity'].__tablename__ == self.block_table:
            return UUID(int=99)
        return None

    async def scalars(self, query: Any) -> Any:
        self.queries.append(query)
        return SimpleNamespace(all=lambda:[self.row])


async def test_maintenance_holds_original_run_lock_through_rpc_without_rewriting_legacy() -> None:
    session = Session()
    before = copy.deepcopy(vars(session.row))
    calls = []

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Reader(UnusedContextReader):
        async def python_expired_environment_invocation(self, tenant: UUID, run: UUID) -> Any:
            assert session.open and (tenant,run)==(session.row.tenant_id,session.row.id)
            calls.append('readonly')
            return invocation().model_copy(update={'source_kind':'original','install_allowed':False})

    class Executor(UnusedReleaser):
        async def release_expired(self, item: Any) -> bool:
            assert session.open and item.run_id==session.row.id and item.install_allowed is False
            calls.append('rpc')
            return True

    hook = PythonProjectTerminalHook(Repository(), Reader(), Executor())
    assert await hook.maintain_capacity() == 1
    assert calls==['readonly','rpc'] and vars(session.row)==before


@pytest.mark.parametrize('defect',[None,'provider','approval','outbox','native-proof',
    'hybrid','unknown','young','lease','scope','root-refusal'])
async def test_generated_expiry_uses_locked_native_proof_and_metadata_only_selector(
    monkeypatch: pytest.MonkeyPatch, defect: str | None,
) -> None:
    from tests.unit.capabilities.test_python_generated_lifecycle import generated_session

    session = Session()
    session.row.routing_decision = generated_session().row.routing_decision
    session.row.mode = 'direct'
    if defect in ('provider','approval','outbox'):
        session.block_table = {'provider':'agent_hub_provider_continuations',
            'approval':'agent_hub_run_approvals','outbox':'agent_hub_run_outbox'}[defect]
    elif defect == 'hybrid':
        session.row.mode = 'hybrid'
    elif defect == 'unknown':
        session.row.routing_decision['retained'] = 'UNKNOWN'
    elif defect == 'young':
        session.row.updated_at = datetime.now(UTC)
    elif defect == 'lease':
        session.row.worker_lease_token = UUID(int=9)
    before = copy.deepcopy(vars(session.row))
    calls = []

    async def proof(actual_session: Any, row: Any) -> bool:
        assert actual_session is session and session.open and row is session.row
        calls.append('native-proof')
        return defect != 'native-proof'

    monkeypatch.setattr('agent_hub.capabilities.python_direct_expiry.verified_direct_expiry', proof)

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Reader(UnusedContextReader):
        async def python_expired_environment_invocation(self, tenant: UUID, run: UUID) -> Any:
            assert session.open
            calls.append('metadata')
            return invocation().model_copy(update={'install_allowed':False,'source_kind':'candidate',
                **({'run_id':UUID(int=9)} if defect == 'scope' else {})})

    class Executor(UnusedReleaser):
        async def release_expired(self, item: Any) -> bool:
            assert session.open and item.source_kind == 'candidate'
            calls.append('root-fenced-rpc')
            return defect != 'root-refusal'

    assert await PythonProjectTerminalHook(Repository(), Reader(), Executor()).maintain_capacity() == int(defect is None)
    expected = {'native-proof':['native-proof'], 'scope':['native-proof','metadata'],
        'unknown':['native-proof'],
        'root-refusal':['native-proof','metadata','root-fenced-rpc'],
        None:['native-proof','metadata','root-fenced-rpc']}.get(defect, [])
    assert calls == expected
    assert vars(session.row) == before


async def test_readonly_executing_invocation_uses_real_immutable_store_without_observation(tmp_path: Path) -> None:
    verifier, repository, executor, inv, _ = await configured(tmp_path)
    # configured has native prep2/captured; model the producer's persisted transition.
    from dataclasses import replace
    route = copy.deepcopy(repository.record.routing_decision)
    route['project_input_preparation'].update(phase='executing',execution_id='original_failed')
    repository.record = replace(repository.record,routing_decision=route)
    before = copy.deepcopy(repository.record)
    result = await verifier.python_expired_environment_invocation(inv.tenant_id, inv.run_id)
    assert result is not None and result.source_kind=='original' and result.install_allowed is False
    assert result.run_id==inv.run_id and result.tenant_id==inv.tenant_id
    assert result.execution_id!='original_failed' and result.timeout_seconds==30
    assert not executor.calls and not executor.inspections and repository.record==before


async def exercise(session: Session, *, response: object = True,
                   change: dict[str, object] | None = None) -> tuple[int, list[str]]:
    calls = []

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Reader(UnusedContextReader):
        async def python_expired_environment_invocation(self, tenant: UUID, run: UUID) -> Any:
            assert session.open
            calls.append('readonly')
            return invocation().model_copy(update={
                'source_kind':'original','install_allowed':False, **(change or {})})

    class Executor(UnusedReleaser):
        async def release_expired(self, item: Any) -> Any:
            assert session.open
            calls.append('rpc')
            if isinstance(response, BaseException):
                raise response
            return response

    return await PythonProjectTerminalHook(Repository(), Reader(), Executor()).maintain_capacity(), calls


@pytest.mark.parametrize('defect', [
    'young', 'naive', 'running', 'worker_id', 'worker_lease_token',
    'worker_lease_expires_at', 'worker_heartbeat_at', 'node', 'captured',
    'prep-extra', 'prep-bool', 'unknown', 'self-repair', 'generated',
    'released', 'wrong-permissions',
])
async def test_db_and_route_eligibility_fail_closed_without_mutation(defect: str) -> None:
    session = Session()
    route = session.row.routing_decision
    state = route['project_input_preparation']
    if defect == 'young':
        session.row.updated_at = datetime.now(UTC)
    elif defect == 'naive':
        session.row.updated_at = datetime.now(UTC).replace(tzinfo=None)-timedelta(days=2)
    elif defect == 'running':
        session.row.status = 'running'
    elif defect.startswith('worker_'):
        setattr(session.row, defect, 'retained')
    elif defect == 'node':
        state['schema_version'] = 1
    elif defect == 'captured':
        state['phase'] = 'captured'
    elif defect == 'prep-extra':
        state['unrecognized'] = 'value'
    elif defect == 'prep-bool':
        state['schema_version'] = True
    elif defect == 'unknown':
        route['retained_native'] = 'UNKNOWN'
    elif defect == 'self-repair':
        route['source'] = 'self_repair'
    elif defect == 'generated':
        route['generated_project_preparation'] = {}
    elif defect == 'released':
        route['python_project_cache_release'] = {}
    else:
        route['requested_permissions'] = []
    before = copy.deepcopy(vars(session.row))
    result, calls = await exercise(session)
    assert result==0 and 'rpc' not in calls and vars(session.row)==before


@pytest.mark.parametrize('table', [
    'agent_hub_run_checkpoints', 'agent_hub_provider_continuations',
    'agent_hub_run_usage', 'agent_hub_run_approvals', 'agent_hub_run_events', 'agent_hub_run_outbox',
])
async def test_any_persisted_execution_provider_usage_or_pending_history_blocks_rpc(table: str) -> None:
    session = Session()
    session.block_table = table
    before = copy.deepcopy(vars(session.row))
    result, calls = await exercise(session)
    assert result==0 and calls==[] and vars(session.row)==before
    for query in session.queries[1:]:
        if query.column_descriptions[0].get('entity').__name__ != 'RunRow':
            sql = str(query)
            assert 'tenant_id' in sql and 'run_id' in sql and query._limit_clause.value==1


@pytest.mark.parametrize('change', [
    {'tenant_id': UUID(int=9)}, {'run_id': UUID(int=9)}, {'project_id':'foreign'},
    {'session_id':'foreign'}, {'source_kind':'candidate'}, {'install_allowed':True},
    {'recipe_sha256':'e'*64}, {'environment_input_sha256':'e'*64},
])
async def test_readonly_invocation_is_rebound_to_locked_native_scope(change: dict[str, object]) -> None:
    session = Session()
    result, calls = await exercise(session, change=change)
    assert result==0 and calls==['readonly']


@pytest.mark.parametrize('response', [False, RuntimeError('fixed_retained_unknown'), None, 1])
async def test_root_absence_refusal_and_invalid_result_never_write_legacy_history(response: object) -> None:
    session = Session()
    before = copy.deepcopy(vars(session.row))
    result, calls = await exercise(session,response=response)
    assert result==0 and calls==['readonly','rpc'] and vars(session.row)==before


async def test_scanner_is_eight_bounded_and_waits_for_locked_candidate() -> None:
    session = Session()
    original = session.scalar
    entered, unlocked = asyncio.Event(), asyncio.Event()

    async def locked(query: Any) -> Any:
        if query.column_descriptions[0].get('entity').__name__ == 'RunRow':
            entered.set()
            if query._for_update_arg.skip_locked:
                return None
            await unlocked.wait()
            return session.row
        return await original(query)

    session.scalar = locked  # type: ignore[method-assign]
    task = asyncio.create_task(exercise(session))
    try:
        await entered.wait()
        await asyncio.sleep(0)
        assert not task.done()
        unlocked.set()
        result, calls = await task
        assert result==1 and calls==['readonly','rpc']
    finally:
        unlocked.set()
        await task
    query = session.queries[0]
    assert query._limit_clause.value==8


@pytest.mark.parametrize('defect', ['recipe','scope','phase','observation'])
async def test_native_readonly_selector_rejects_wrong_recipe_scope_or_phase(
    tmp_path: Path, defect: str,
) -> None:
    from dataclasses import replace

    from agent_hub.capabilities.project_verification import ProjectVerificationError

    verifier, repository, executor, inv, _ = await configured(tmp_path)
    route = copy.deepcopy(repository.record.routing_decision)
    state = route['project_input_preparation']
    state.update(phase='executing',execution_id='original_failed')
    if defect == 'recipe':
        state['python_recipe']['recipe_sha256'] = 'not-a-sha'
    elif defect == 'scope':
        repository.record = replace(repository.record,tenant_id=UUID(int=99))
    elif defect == 'phase':
        state['phase'] = 'observed'
    else:
        state['observation'] = {'schema_version':2,'environment_reference':None}
    repository.record = replace(repository.record,routing_decision=route)
    before = copy.deepcopy(repository.record)
    with pytest.raises(ProjectVerificationError):
        await verifier.python_expired_environment_invocation(inv.tenant_id,inv.run_id)
    assert repository.record==before and not executor.calls and not executor.inspections


@pytest.mark.parametrize('generated', [False, True])
async def test_expiry_only_selector_needs_no_project_baseline_or_live_generated_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generated: bool,
) -> None:
    import hashlib

    from agent_hub.auth.models import Role
    from agent_hub.capabilities.generated_project_service import GeneratedProjectService
    from agent_hub.capabilities.project_baselines import ProjectBaselineStore
    from agent_hub.capabilities.project_contracts import ManagedProjectInput
    from agent_hub.capabilities.project_verification import ProjectVerificationService
    from tests.unit.capabilities.test_python_generated_lifecycle import generated_session

    row = Session().row
    row.actor_id, row.actor_role, row.request = UUID(int=3), Role.OPERATOR, 'historical request'
    row.routing_decision.update(project_input_contract_version=1,
                                project_input=ManagedProjectInput(('src',)).to_payload())
    if generated:
        row.routing_decision = generated_session().row.routing_decision
        row.routing_decision['requested_permissions'] = ['workspace.read','workspace.write','command.run']
        row.routing_decision['generated_project_preparation']['request_sha256'] = hashlib.sha256(row.request.encode()).hexdigest()

    class Repository:
        async def get(self, tenant: UUID, run: UUID) -> Any:
            return row

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('expiry must not load mutable files or mint execution authority')

    verifier = ProjectVerificationService(root=tmp_path/'absent-projects', repository=Repository(), executor=None)
    monkeypatch.setattr(ProjectBaselineStore, 'load', forbidden)
    monkeypatch.setattr(GeneratedProjectService, 'load', forbidden)
    before = copy.deepcopy(vars(row))
    result = await verifier.python_expired_environment_invocation(row.tenant_id,row.id)
    assert result is not None and result.install_allowed is False and result.timeout_seconds==30
    assert result.source_kind==('candidate' if generated else 'original')
    assert result.run_id==row.id and result.tenant_id==row.tenant_id
    assert result.original_reference==invocation().original_reference
    assert vars(row)==before and not (tmp_path/'absent-projects').exists()


async def test_prepare_callback_waits_for_inflight_periodic_maintenance() -> None:
    """Do not let a concurrent prepare race a still-full cache and fail early."""
    session = Session()
    started, finish = asyncio.Event(), asyncio.Event()

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Reader(UnusedContextReader):
        async def python_expired_environment_invocation(self, tenant: UUID, run: UUID) -> Any:
            return invocation().model_copy(update={'source_kind':'original','install_allowed':False})

    class Executor(UnusedReleaser):
        async def release_expired(self, item: Any) -> bool:
            started.set()
            await finish.wait()
            return True

    hook = PythonProjectTerminalHook(Repository(), Reader(), Executor())
    periodic = asyncio.create_task(hook.maintain_capacity())
    callback = None
    try:
        await started.wait()
        callback = asyncio.create_task(hook.maintain_capacity())
        await asyncio.sleep(0)
        assert not callback.done()
        finish.set()
        assert await periodic == 1
        assert await callback == 0
    finally:
        finish.set()
        await periodic
        if callback is not None:
            await callback


async def test_periodic_row_lock_wait_has_its_own_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.capabilities import python_project_lifecycle as lifecycle

    session = Session()
    original = session.scalar

    async def forever(query: Any) -> Any:
        if query.column_descriptions[0]['entity'].__name__ == 'RunRow':
            assert not query._for_update_arg.skip_locked
            await asyncio.Event().wait()
        return await original(query)

    session.scalar = forever  # type: ignore[method-assign]
    monkeypatch.setattr(lifecycle, '_CAPACITY_TIMEOUT_SECONDS', .01)
    result, calls = await asyncio.wait_for(exercise(session), timeout=1)
    assert result==0 and calls==[] and not session.open


@pytest.mark.parametrize('cancel_twice', [False, True])
async def test_real_client_cancel_drain_finishes_before_db_lock_is_released(
    monkeypatch: pytest.MonkeyPatch, cancel_twice: bool,
) -> None:
    from agent_hub.skills.sandbox import python_project_client as client

    session = Session()
    started, terminating, drain_allowed, closed = (asyncio.Event() for _ in range(4))

    async def transport(*args: Any, **kwargs: Any) -> Any:
        assert session.open
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert session.open
            closed.set()

    async def terminate(self: Any, execution: str) -> None:
        assert session.open
        terminating.set()
        await drain_allowed.wait()
        assert session.open

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Reader(UnusedContextReader):
        async def python_expired_environment_invocation(self, tenant: UUID, run: UUID) -> Any:
            return invocation().model_copy(update={'source_kind':'original','install_allowed':False})

    monkeypatch.setattr(client, '_systemd_supported', lambda:True)
    monkeypatch.setattr(client, 'request_python_project_broker', transport)
    monkeypatch.setattr(client.PythonProjectSystemdSandbox, 'terminate', terminate)
    hook = PythonProjectTerminalHook(Repository(), Reader(), client.PythonProjectSystemdSandbox())
    task = asyncio.create_task(hook.maintain_capacity())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.wait_for(terminating.wait(), timeout=2)
        if cancel_twice:
            task.cancel()
            await asyncio.sleep(0)
        assert session.open and not task.done() and not closed.is_set()
        drain_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and not session.open
    finally:
        drain_allowed.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
