"""Historical suggestion classification only: no native cache, provider or cleanup."""
from __future__ import annotations

import asyncio
import copy
import selectors
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
from agent_hub.db.models import RunCheckpointRow, RunEventRow, RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.self_repair import SelfRepairPolicy, classify_terminal_run
from agent_hub.runtime.contracts import EventKind, RunEvent
from tests.unit.capabilities.capability_lifecycle_typed_fixture import UnusedContextReader


class MetadataBoundary(Exception):
    """The real lifecycle reached its unchanged downstream metadata selector."""


class MemorySelector(selectors.SelectSelector):
    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        return []


class MemoryLoop(asyncio.SelectorEventLoop):
    """These tests need task/deadline scheduling, never Windows socketpair IO."""

    def __init__(self) -> None:
        self.memory_selector = MemorySelector()
        super().__init__(selector=self.memory_selector)

    def _make_self_pipe(self) -> None:
        self._ssock = self._csock = None

    def _close_self_pipe(self) -> None:
        pass

    def _write_to_self(self) -> None:
        pass


def run[T](coro: Coroutine[object, object, T]) -> T:
    loop = MemoryLoop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Session:
    def __init__(self) -> None:
        self.tenant, self.owner = UUID(int=1), UUID(int=2)
        failure = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=5, run_id=self.owner,
                           reason='structured output invalid')
        decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.HYBRID,
            routing_decision={}, events=(failure,), policy=SelfRepairPolicy())
        assert decision is not None
        proposal = decision.to_proposal(run_id=self.owner)
        assert proposal is not None and proposal['pauses_run'] is False
        self.row = SimpleNamespace(tenant_id=self.tenant, id=self.owner, status='failed', version=14,
            updated_at=datetime.now(UTC)-timedelta(days=3), blocked_by_run_id=None, mode='hybrid',
            worker_id=None, worker_lease_token=None, worker_lease_expires_at=None, worker_heartbeat_at=None,
            routing_decision={'project_id':'demo','workspace_session_id':'one',
                'sandbox_profile':'workspace_write','requested_permissions':['workspace.read'],
                'approval_kind':'self_repair', 'decision_token':'decision-'+'1'*64,
                'repair_proposal':proposal, 'project_input_preparation':{'schema_version':2,'phase':'observed'}})
        self.source: SimpleNamespace | None = SimpleNamespace(tenant_id=self.tenant,run_id=self.owner,sequence=5,
            kind='runtime.failed',payload=failure.to_payload())
        classified = decision.to_event(run_id=self.owner, sequence=6)
        self.events = [SimpleNamespace(tenant_id=self.tenant,run_id=self.owner,sequence=6,
            kind='repair.classified',payload=classified.to_payload())]
        self.open = False
        self.queries: list[Any] = []
        self.block_table: str | None = None
        self.child: Any = None
        self.checkpoint: Any = None
        # Real SQLAlchemy interface; query results remain deliberate in-memory
        # metadata doubles, including malformed values under test.
        self.sql_session = AsyncSession()
        for name in ("begin", "scalar", "scalars"):
            patch.object(self.sql_session, name, getattr(self, name)).start()

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
        assert self.open
        self.queries.append(query)
        entity = query.column_descriptions[0]['entity']
        if entity is RunRow:
            if query._for_update_arg is not None:
                return self.row
            return self.child
        if entity is RunEventRow:
            return self.source
        if entity is RunCheckpointRow:
            return self.checkpoint
        if entity.__tablename__ == self.block_table:
            return UUID(int=99)
        return None

    async def scalars(self, query: Any) -> Any:
        assert self.open
        self.queries.append(query)
        assert query.column_descriptions[0]['entity'] is RunEventRow
        return SimpleNamespace(all=lambda:self.events)


def hook(session: Session) -> PythonProjectTerminalHook:
    class Repository:
        async def run_transaction(self) -> AsyncSession:
            return session.sql_session

    class Reader(UnusedContextReader):
        async def python_expired_environment_invocation(self, tenant: UUID, owner: UUID) -> Any:
            assert session.open and (tenant,owner)==(session.tenant,session.owner)
            raise MetadataBoundary

    class Executor:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError('no native cleanup is part of these tests')

    return PythonProjectTerminalHook(Repository(),Reader(),Executor())


def exercise(session: Session) -> bool:
    return run(hook(session)._release_expired_terminal_observed(
        session.tenant, session.owner, datetime.now(UTC)-timedelta(days=1)))


def test_persisted_nonblocking_manual_suggestion_reaches_unchanged_next_gate() -> None:
    session = Session()
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.events[0].payload)
    with pytest.raises(MetadataBoundary):
        exercise(session)
    assert (vars(session.row),session.events[0].payload)==before
    assert session.queries[0]._for_update_arg is not None and not session.open


@pytest.mark.parametrize('defect', [
    'pause','pause-int','automatic','approval-not-required','wrong-kind','bad-token',
    'foreign-proposal','foreign-event','foreign-source','source-sequence','source-kind',
    'fingerprint','proposal-extra','event-extra','event-version','missing-event','duplicate-event',
    'missing-source','replaced-source','unknown-route','source-selfrepair','repair-child',
    'pending-approval','outbox','provider','reserved-artifact','checkpoint-running',
])
def test_unproved_suggestion_or_original_downstream_blocker_stays_rejected(defect: str) -> None:
    session = Session()
    route = session.row.routing_decision
    proposal = route['repair_proposal']
    if defect=='pause':
        proposal['pauses_run']=True
    elif defect=='pause-int':
        proposal['pauses_run']=0
    elif defect=='automatic':
        proposal['automatic_execution']=True
    elif defect=='approval-not-required':
        proposal['requires_approval']=False
    elif defect=='wrong-kind':
        route['approval_kind']='project_preflight'
    elif defect=='bad-token':
        route['decision_token']=''
    elif defect=='foreign-proposal':
        proposal['source_run_id']=str(UUID(int=99))
    elif defect=='foreign-event':
        session.events[0].tenant_id=UUID(int=99)
    elif defect=='foreign-source':
        assert session.source is not None
        session.source.run_id=UUID(int=99)
    elif defect=='source-sequence':
        proposal['source_event_sequence']=4
    elif defect=='source-kind':
        assert session.source is not None
        session.source.kind='model.completed'
    elif defect=='fingerprint':
        proposal['fingerprint']='0'*64
    elif defect=='proposal-extra':
        proposal['execution_authorized']=True
    elif defect=='event-extra':
        session.events[0].payload['payload']['execution_authorized']=True
    elif defect=='event-version':
        session.events[0].payload['payload']['schema_version']=True
    elif defect=='missing-event':
        session.events=[]
    elif defect=='duplicate-event':
        session.events*=2
    elif defect=='missing-source':
        session.source=None
    elif defect=='replaced-source':
        assert session.source is not None
        session.source.payload['reason']='different failure'
    elif defect=='unknown-route':
        route['other_consumer']='UNKNOWN'
    elif defect=='source-selfrepair':
        route['source']='self_repair'
    elif defect=='repair-child':
        session.child=UUID(int=99)
    elif defect=='checkpoint-running':
        session.checkpoint=SimpleNamespace(runtime_type='direct',runtime_version='1',mode='direct',state={'phase':'running'})
    else:
        session.block_table={'pending-approval':'agent_hub_run_approvals','outbox':'agent_hub_run_outbox',
            'provider':'agent_hub_provider_continuations','reserved-artifact':'agent_hub_runtime_artifact_writes'}[defect]
    before=copy.deepcopy(vars(session.row))
    assert exercise(session)is False
    assert vars(session.row)==before and not session.open
