"""Typed in-memory ORM boundary for review tests; never a PostgreSQL lock proof."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Self, TypeGuard
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.engine import Result
from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, async_sessionmaker
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.orm.unitofwork import UOWTransaction
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import BinaryExpression, BindParameter

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.capabilities.review_repository import review_config_sha256
from agent_hub.db.base import Base
from agent_hub.db.models import (
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunReviewEventRow,
    RunRow,
)
from agent_hub.models.admission_bounds import TrustedContextBound
from agent_hub.models.physical_budget import _RunBudgetContext
from agent_hub.models.run_admission_repository import (
    PoolSnapshot,
    RunAdmissionRepository,
    _WorkerLease,
)
from agent_hub.models.types import ModelMessage, ModelRequest
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runtime.contracts import JsonValue, _freeze_object


def present[T](value: T | None) -> T:
    assert value is not None
    return value


def _string_keys(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def mutable(value: object) -> dict[str, object]:
    assert _string_keys(value)
    return value


def mapping(value: object) -> dict[str, JsonValue]:
    return dict(_freeze_object(value, name='review fixture mapping'))


def json_objects(value: object) -> list[dict[str, JsonValue]]:
    assert isinstance(value, (tuple, list))
    return [mapping(item) for item in value]


def result_rows(values: Sequence[tuple[object, ...]]) -> IteratorResult[tuple[object, ...]]:
    width = len(values[0]) if values else 1
    return IteratorResult(SimpleResultMetaData([str(i) for i in range(width)]), iter(values))


class ReviewSession(Session):
    pass


class ReviewTransaction(AsyncSessionTransaction):
    async def __aenter__(self) -> Self:
        owner = self.session.info['review_fixture']
        assert isinstance(owner, ReviewMemoryDB)
        await owner.lock.acquire()
        try:
            await super().__aenter__()
        except BaseException:
            owner.lock.release()
            raise
        return self

    async def __aexit__(self, typ: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None) -> None:
        owner = self.session.info['review_fixture']
        assert isinstance(owner, ReviewMemoryDB)
        try:
            await super().__aexit__(typ, value, traceback)
        finally:
            owner.lock.release()


class ReviewAsyncSession(AsyncSession):
    def begin(self) -> AsyncSessionTransaction:
        return ReviewTransaction(self)


class ReviewMemoryDB(async_sessionmaker[AsyncSession]):
    def __init__(self) -> None:
        self.rows: list[Base] = []
        self.lock = asyncio.Lock()
        super().__init__(class_=ReviewAsyncSession, sync_session_class=ReviewSession,
            expire_on_commit=False, info={'review_fixture': self})

    def add(self, row: Base) -> None:
        if isinstance(row, RunReviewEventRow):
            row.sequence = len(self.rows)
        self.rows.append(row)

    def select(self, statement: Select[tuple[object, ...]]) -> list[Base]:
        entity = statement.column_descriptions[0]['entity']
        assert isinstance(entity, type) and issubclass(entity, Base)
        rows = [row for row in self.rows if type(row) is entity]
        for condition in statement._where_criteria:
            assert isinstance(condition, BinaryExpression)
            assert isinstance(condition.right, BindParameter)
            name = condition.left.key
            assert isinstance(name, str)
            value: object = condition.right.value
            rows = [row for row in rows if getattr(row, name) == value]
        return rows

    def execute_rows(self, statement: Select[tuple[object, ...]]) -> IteratorResult[tuple[object, ...]]:
        if (statement.column_descriptions[0].get('entity') is RunInputAuthorizationRow
                and len(statement.column_descriptions) == 2):
            link = next(r for r in self.rows if isinstance(r, RunInputAuthorizationRow))
            turn = next(r for r in self.rows if isinstance(r, RunAuthenticatedInputRow))
            return result_rows([(link, turn)])
        if 'clock_timestamp' in str(statement):
            return result_rows([(datetime.now(UTC),)])
        return result_rows([(row,) for row in self.select(statement)])


@event.listens_for(ReviewSession, 'do_orm_execute')
def rows_for_query(state: ORMExecuteState) -> Result[tuple[object, ...]]:
    owner = state.session.info['review_fixture']
    assert isinstance(owner, ReviewMemoryDB)
    assert isinstance(state.statement, Select)
    return owner.execute_rows(state.statement)


@event.listens_for(ReviewSession, 'before_flush')
def retain_rows(session: Session, context: UOWTransaction, instances: Sequence[object] | None) -> None:
    owner = session.info['review_fixture']
    assert isinstance(owner, ReviewMemoryDB)
    for row in session.new:
        assert isinstance(row, Base)
        owner.add(row)
    session.expunge_all()


def configured_budget() -> _RunBudgetContext:
    """Same fixed grant/identity/current-fence facts as the prior budget fixture.

    Only the current fence is needed here. Model reservation/charging in assembly
    tests still uses the real RunAdmissionRepository and its own memory database.
    """
    config = ApprovalReviewConfig(engine='rules_then_model', reviewer_logical_model='reviewer',
        max_input_tokens=20000, max_output_tokens=512, max_cost_usd_per_run='10.00')
    evidence = _capture_authenticated_input(
        AuthenticatedPrincipal(tenant_id=uuid4(), user_id=uuid4(), role=Role.OPERATOR),
        'Write the exact approved test file.', {'workspace': 'synthetic'})
    pool = PoolSnapshot(evidence.tenant_id, uuid4(), evidence.turn_id, review_config_sha256(config),
        40000, datetime.now(UTC)+timedelta(seconds=50), 40000, 8, Decimal(10), 0, 0, 0, Decimal(0), False)
    db = ReviewMemoryDB()
    repository = RunAdmissionRepository(db)
    context = _RunBudgetContext(repository=repository, pool=pool, evidence=evidence,
        lease=_WorkerLease('test-worker', uuid4()), config=config, deadline=time.monotonic()+45,
        trusted_contexts=tuple(TrustedContextBound(*values, 10000) for values in (
            ('selected','primary','openai/acting'), ('review-selected','reviewer','openai/reviewer'))),
        acting_model=None)
    db.add(RunRow(id=context.run_id, tenant_id=context.tenant_id, actor_id=evidence.actor_id,
        actor_role=evidence.actor_role.value, request=evidence.message, status='running', mode='direct',
        blocked_by_run_id=None, worker_id=context.lease.worker_id, worker_lease_token=context.lease.token,
        worker_lease_expires_at=pool.original_deadline, routing_decision={
            'runtime_token_absolute_tokens':40000,'runtime_plan_token_budget':40000,
            'runtime_timeout_absolute_seconds':50}))
    db.add(RunAdmissionPoolRow(tenant_id=context.tenant_id,run_id=context.run_id,
        authorization_turn_id=evidence.turn_id,actor_id=evidence.actor_id,actor_role=evidence.actor_role.value,
        message_sha256=evidence.message_sha256,scope_sha256=evidence.scope_sha256,
        config_sha256=review_config_sha256(config),original_token_grant=pool.original_token_grant,
        original_deadline=pool.original_deadline,closed=False))
    db.add(RunInputAuthorizationRow(run_id=context.run_id,tenant_id=context.tenant_id,turn_id=evidence.turn_id))
    db.add(RunAuthenticatedInputRow(run_id=context.run_id,tenant_id=context.tenant_id,turn_id=evidence.turn_id,
        actor_id=evidence.actor_id,actor_role=evidence.actor_role.value,message=evidence.message,
        message_sha256=evidence.message_sha256,input_scope=evidence.input_scope,scope_sha256=evidence.scope_sha256))
    return context


def logical_request(*, max_output_tokens: int = 30000, logical_model: str = 'primary', allow_fallback: bool = True) -> ModelRequest:
    return ModelRequest(logical_model=logical_model, messages=(ModelMessage(role='user',content='private prompt'),),
        max_output_tokens=max_output_tokens, allow_fallback=allow_fallback)


def response(text: str = 'ok', usage: bool = True) -> dict[str, object]:
    body: dict[str, object] = {'id':'fixed','object':'chat.completion','created':1,'model':'test',
        'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}]}
    if usage:
        body['usage'] = {'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}
    return body


from uuid import UUID

from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.review_repository import (
    ReviewIntent,
    ReviewRepository,
    _CurrentReviewBinding,
)
from agent_hub.domain.runs import RunStatus
from agent_hub.runs.repository import RunRecord
from tests.unit.capabilities.test_defaults import (
    RUN_ID,
    TENANT_ID,
    USER_ID,
    RecordingApprovalRepository,
)


class ConcreteRepositoryAdapter:
    """Await the unchanged decorated repository methods through an async protocol."""
    def __init__(self, repository: ReviewRepository) -> None:
        self.repository = repository

    async def lookup_effect(self, tenant_id: UUID, run_id: UUID, authorization_turn_id: UUID, effect_digest: str) -> ReviewIntent | None:
        return await self.repository.lookup_effect(tenant_id, run_id, authorization_turn_id, effect_digest)

    async def consume_permit(self, *, tenant_id: UUID, run_id: UUID, review_id: UUID, current_binding: _CurrentReviewBinding) -> bool:
        return await self.repository.consume_permit(tenant_id=tenant_id,run_id=run_id,review_id=review_id,current_binding=current_binding)


class ModeRepository(RecordingApprovalRepository):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.record = RunRecord(tenant_id=TENANT_ID,id=RUN_ID,actor_id=USER_ID,
            actor_role=Role.OPERATOR,status=RunStatus.RUNNING,mode=TaskMode.DIRECT,
            request='fixed permission-mode fixture',version=1,created_at=datetime.now(UTC),routing_decision={
                'permission_mode':mode,'sandbox_profile':'read_only' if mode=='read_only' else 'workspace_write',
                'requested_permissions':['workspace.read']})

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
        return self.record


from collections.abc import AsyncIterator
from typing import Never

import pytest

from agent_hub.config.service import ConfigService
from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityPool
from agent_hub.models.gateway import CapacityController, ModelGateway
from agent_hub.models.types import Deployment
from agent_hub.runtime.contracts import RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.security.secrets import SecretService


class StreamRuntimeStub:
    """Only run() is exercised; checkpoint operations are deliberately forbidden."""
    mode = TaskMode.DIRECT

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        raise AssertionError('the fixture must provide its actual event stream')

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError('this fixture must not save a checkpoint')

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError('this fixture must not restore a checkpoint')

    async def cancel(self) -> None:
        pass


class UnusedGateway(ModelGateway):
    def __init__(self) -> None:
        pass

    async def complete(self, *args: object, **kwargs: object) -> Never:
        pytest.fail('cancel-only fixture must not call a model')


class UnusedConfigs(ConfigService):
    def __init__(self) -> None:
        pass


class UnusedSecrets(SecretService):
    def __init__(self) -> None:
        pass


async def unused_capacity(tenant: UUID, deployments: tuple[Deployment, ...]) -> CapacityController | CapacityPool:
    raise AssertionError('cancel-only fixture must not acquire model capacity')


from collections.abc import Mapping

from agent_hub.models.capacity import CapacityLease, _CapacityScopeView
from tests.unit.models.test_gateway import CapacityStub, lease


class FixedCapacityView(_CapacityScopeView):
    """Real scoped catalog checks, fixed local capacity replies; no Redis proof."""
    def __init__(self, root: CapacityPool, deployments: Sequence[Deployment]) -> None:
        super().__init__(root, deployments)
        self.delegate = CapacityStub([])

    async def initialize(self) -> None:
        await self.delegate.initialize()

    async def acquire(self, candidates: Sequence[Deployment], wait_timeout: float, *,
        estimated_tokens: int | Mapping[str, int]) -> CapacityLease:
        self.validate_configuration(candidates)
        assert self.delegate.initialized
        return lease(candidates[0].id, candidates[0].quota_scope_id)

    async def release(self, value: CapacityLease) -> bool:
        return await self.delegate.release(value)

    async def renew(self, value: CapacityLease) -> CapacityLease | None:
        return await self.delegate.renew(value)

    async def record_outcome(self, quota_scope_id: str, *, status_code: int | None,
        latency_seconds: float, succeeded: bool) -> None:
        await self.delegate.record_outcome(quota_scope_id, status_code=status_code,
            latency_seconds=latency_seconds, succeeded=succeeded)


class FixedCapacityPool(CapacityPool):
    def __init__(self, deployments: Sequence[Deployment]) -> None:
        super().__init__(object(), deployments=deployments)

    def scoped(self, deployments: Sequence[Deployment]) -> _CapacityScopeView:
        return FixedCapacityView(self, deployments)


class FixedPolicyRules:
    def __init__(self, rules: tuple[CapabilityRule, ...]) -> None:
        self.rules = rules

    def capability_policy_rules(self, tenant_id: UUID) -> tuple[CapabilityRule, ...]:
        return self.rules
