"""Supply orchestration through the real adapter/DAO and simulated Root I/O."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.capabilities.toolchain_preparation import supply_project_toolchain
from agent_hub.runs.repository import RunConflict
from agent_hub.runtime.contracts import _freeze_object
from tests.unit.capabilities.test_toolchain_preparation import Root, state_for
from tests.unit.runs.test_toolchain_supply_repository import SupplyDB, api
from tests.unit.runs.test_toolchain_supply_repository import case as case  # noqa: PLC0414
from tests.unit.runs.test_toolchain_supply_repository import db as db  # noqa: PLC0414


class CommittedRoot(Root):
    def __init__(self, db: SupplyDB) -> None:
        self.db = db
        super().__init__(db.case, self)  # type: ignore[arg-type]

    @property
    def value(self) -> Any:
        state = self.db.row.routing_decision.get('toolchain_supply')
        return None if state is None else _freeze_object(state, name='committed supply')

    def event(self, name: str) -> None:
        super().event(name)
        if name in {'supply', 'profile'}:
            assert self.db.commits[-1] == ('executing' if name == 'supply' else 'supplied')


async def execute(db: SupplyDB, root: Root, *, approve: bool = True) -> Any:
    adapter = api().ToolchainSupplyStateAdapter(db.repository, authority=db.bound)

    async def authorize(plan: Any, state: Any) -> None:
        assert state['phase'] == 'planned'
        if approve:
            db.approve()

    return await supply_project_toolchain(client=root, invocation=db.case.call, stored=db.case.stored,
        state_io=adapter, authorize=authorize, deadline=asyncio.get_running_loop().time() + 30)


async def test_adapter_commits_intent_before_root_and_returns_exact_state(db: SupplyDB) -> None:
    root = CommittedRoot(db)
    result = await execute(db, root)
    assert result == root.value and result['phase'] == 'supplied'
    assert root.calls == ['plan', 'supply', 'profile']
    assert db.row.version == 7
    assert db.row.routing_decision['unrelated'] == {'keep': [1, False, 'value']}


@pytest.mark.parametrize('phase', ['planned', 'executing'])
async def test_failed_commit_prevents_privileged_supply(db: SupplyDB, phase: str) -> None:
    db.fail_phase = phase
    root = CommittedRoot(db)
    with pytest.raises(OSError, match='commit failure'):
        await execute(db, root)
    assert root.calls == ['plan']
    assert db.row.routing_decision.get('toolchain_supply', {}).get('phase') != 'executing'


async def test_authorization_callback_is_not_itself_a_db_grant(db: SupplyDB) -> None:
    root = CommittedRoot(db)
    with pytest.raises(RunConflict):
        await execute(db, root, approve=False)
    assert root.calls == ['plan'] and root.value['phase'] == 'planned'


async def test_lost_reply_new_lease_only_inspects_original_execution(db: SupplyDB) -> None:
    first = CommittedRoot(db)
    first.supply_error = TimeoutError()
    with pytest.raises(ValueError, match='toolchain_timeout'):
        await execute(db, first)
    execution = first.value['execution_id']
    old = api().ToolchainSupplyStateAdapter(db.repository, authority=db.bound)
    db.row.worker_id, db.row.worker_lease_token = 'replacement-worker', uuid4()
    with pytest.raises(RunConflict):
        await old()
    db.bound = api().ToolchainSupplyAuthority.from_context(db.context(), worker_id='replacement-worker')
    second = CommittedRoot(db)
    result = await execute(db, second, approve=False)
    assert second.calls == ['inspect', 'profile']
    assert result['execution_id'] == execution


@pytest.mark.parametrize('status', ['absent', 'in_progress', 'unknown'])
async def test_unsettled_execution_never_resupplies(db: SupplyDB, status: Any) -> None:
    db.row.routing_decision['toolchain_supply'] = state_for(db.case, 'executing')
    db.approve()
    root = CommittedRoot(db)
    root.status = status
    with pytest.raises(ValueError, match='toolchain_unknown'):
        await execute(db, root)
    assert root.calls == ['inspect'] and root.value['phase'] == 'executing'


async def test_profile_retry_needs_live_grant_and_never_resupplies(db: SupplyDB) -> None:
    root = CommittedRoot(db)
    root.profile_error = ValueError('toolchain_unavailable')
    with pytest.raises(ValueError, match='toolchain_unavailable'):
        await execute(db, root)
    assert root.value['phase'] == 'supplied'
    root.profile_error = None
    root.calls.clear()
    await execute(db, root, approve=False)
    assert root.calls == ['profile']
    db.approvals[0].status = 'denied'
    root.calls.clear()
    with pytest.raises(RunConflict):
        await execute(db, root, approve=False)
    assert root.calls == [] and root.value['phase'] == 'supplied'


@pytest.mark.parametrize('point', ['plan', 'supply', 'inspect', 'profile'])
async def test_root_io_cannot_hide_lost_worker_identity(db: SupplyDB, point: str) -> None:
    root = CommittedRoot(db)
    if point == 'inspect':
        db.row.routing_decision['toolchain_supply'] = state_for(db.case, 'executing')
        db.approve()
    original = root.event

    def lose_worker(name: str) -> None:
        original(name)
        if name == point:
            db.row.worker_id = 'foreign-worker'

    root.event = lose_worker  # type: ignore[method-assign]
    with pytest.raises(RunConflict):
        await execute(db, root)
    assert point in root.calls


async def test_nonempty_expected_without_replacement_is_not_a_delete(db: SupplyDB) -> None:
    await db.write(None, state_for(db.case))
    adapter = api().ToolchainSupplyStateAdapter(db.repository, authority=db.bound)
    with pytest.raises((RunConflict, ValueError)):
        await adapter(expected=_freeze_object(state_for(db.case), name='expected'))
    assert db.row.routing_decision['toolchain_supply']['phase'] == 'planned'


@pytest.mark.parametrize('point', ['plan', 'supply', 'profile'])
@pytest.mark.parametrize('defect', ['disabled', 'downgraded'])
async def test_user_changed_during_root_io_is_rechecked(db: SupplyDB, point: str, defect: str) -> None:
    root = CommittedRoot(db)
    original = root.event

    def change_user(name: str) -> None:
        original(name)
        if name == point:
            if defect == 'disabled':
                db.user.disabled = True
            else:
                db.user.role = 'viewer'

    root.event = change_user  # type: ignore[method-assign]
    with pytest.raises(RunConflict):
        await execute(db, root)
    assert point in root.calls


async def test_recovery_with_revoked_grant_does_no_root_io(db: SupplyDB) -> None:
    db.row.routing_decision['toolchain_supply'] = state_for(db.case, 'executing')
    db.approve()
    db.approvals[0].status = 'denied'
    root = CommittedRoot(db)
    with pytest.raises(RunConflict):
        await execute(db, root, approve=False)
    assert root.calls == [] and root.value['phase'] == 'executing'


async def test_receipt_commit_failure_recovers_by_inspect_without_resupply(db: SupplyDB) -> None:
    db.fail_phase = 'supplied'
    root = CommittedRoot(db)
    with pytest.raises(OSError, match='commit failure'):
        await execute(db, root)
    assert root.calls == ['plan', 'supply'] and root.value['phase'] == 'executing'
    execution = root.value['execution_id']
    db.fail_phase = None
    recovered = CommittedRoot(db)
    result = await execute(db, recovered, approve=False)
    assert recovered.calls == ['inspect', 'profile']
    assert result['execution_id'] == execution
