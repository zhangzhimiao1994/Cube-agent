"""Typed integration seams. Local row delivery is not PostgreSQL proof."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeGuard
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.orm.unitofwork import UOWTransaction
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import BinaryExpression, BindParameter
from sqlalchemy.sql.operators import eq
from sqlalchemy.sql.visitors import iterate

from agent_hub.db.base import Base
from agent_hub.db.models import (
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunRow,
)
from agent_hub.models.run_admission_repository import (
    RunAdmissionRepository,
    _ExecutionArguments,
    _OpenArguments,
    _WorkerLease,
)
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from tests.integration.models.run_admission_typed_fixture import Storage
from tests.integration.models.test_run_admission import prepare


def present[T](value: T | None) -> T:
    assert value is not None
    return value


def integer(value: object) -> int:
    assert type(value) is int
    return value


def _dictionary(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(k, str) for k in value)


def mapping(value: object) -> dict[str, object]:
    assert _dictionary(value)
    return value


def context_route(value: object) -> Mapping[str, JsonValue]:
    """Use the production immutable JSON view only for TaskContext construction."""
    frozen = _freeze_object(value, name='routing_decision')
    assert isinstance(frozen, Mapping)
    return frozen


def _binding(value: object) -> TypeGuard[_ExecutionArguments]:
    return (_dictionary(value) and all(isinstance(value.get(k), UUID) for k in ('tenant_id', 'run_id', 'authorization_turn_id'))
            and isinstance(value.get('lease'), _WorkerLease) and isinstance(value.get('config_sha256'), str))


def _opening(value: object) -> TypeGuard[_OpenArguments]:
    return (_binding(value) and isinstance(value.get('original_deadline'), datetime)
            and all(type(value.get(k)) is int for k in ('original_token_grant', 'review_token_limit', 'review_count_limit'))
            and isinstance(value.get('review_cost_limit'), Decimal))


async def prepare_mutable_route(
    storage: Storage, *, route: dict[str, object], open_now: bool = True,
) -> tuple[RunAdmissionRepository, _ExecutionArguments, _OpenArguments]:
    """Call original prepare with unchanged mutable JSON; validate its real result.

    Shared prepare annotates frozen JsonValue, while this original fixture passes
    a mutable requested_permissions list. No coercion, duplicate DAO or replacement
    grant/evidence is introduced at this one legacy JSON boundary.
    """
    original: object = prepare
    assert callable(original)
    result: object = await original(storage, route=route, open_now=open_now)
    assert isinstance(result, tuple) and len(result) == 3
    repository, binding, opening = result
    assert isinstance(repository, RunAdmissionRepository)
    assert _binding(binding) and _opening(opening)
    return repository, binding, opening


class PreparationSession(Session):
    pass


def result_rows(values: Sequence[tuple[object, ...]]) -> IteratorResult[tuple[object, ...]]:
    width = len(values[0]) if values else 1
    return IteratorResult(SimpleResultMetaData([str(i) for i in range(width)]), iter(values))


def tenant_matches(row: Base, statement: Select[tuple[object, ...]]) -> bool:
    # Session.get uses its primary key alone, as the original get seam did.
    # Explicit tenant predicates in scalar/authorization SELECTs must still filter.
    for clause in statement._where_criteria:
        for term in iterate(clause):
            if isinstance(term, BinaryExpression) and getattr(term.left, 'key', None) == 'tenant_id':
                assert term.operator is eq and isinstance(term.right, BindParameter)
                actual: object = getattr(row, 'tenant_id', None)
                if actual != term.right.value:
                    return False
    return True


def owner_of(session: Session) -> PreparationStorage:
    owner = session.info['dynamic_resolution_preparation']
    assert isinstance(owner, PreparationStorage)
    return owner


@event.listens_for(PreparationSession, 'after_attach')
def attached(session: Session, row: object) -> None:
    owner = owner_of(session)
    assert isinstance(row, Base)
    if isinstance(row, RunRow) and row.created_at is None:
        row.created_at = owner.now
    owner.rows.append(row)


@event.listens_for(PreparationSession, 'before_flush')
def flush_rows(session: Session, context: UOWTransaction, objects: object) -> None:
    session.expunge_all()


@event.listens_for(PreparationSession, 'do_orm_execute')
def selected(state: ORMExecuteState) -> IteratorResult[tuple[object, ...]]:
    owner = owner_of(state.session)
    statement = state.statement
    assert isinstance(statement, Select)
    if str(statement.column_descriptions[0]['expr']) == 'clock_timestamp()':
        return result_rows([(owner.now,)])
    entities = tuple(column['entity'] for column in statement.column_descriptions)
    parameters: list[object] = list(statement.compile().params.values())
    if isinstance(state.parameters, Mapping):
        parameters.extend(state.parameters.values())
    if entities == (RunInputAuthorizationRow, RunAuthenticatedInputRow):
        owner.authorization_reads += 1
        link = next((r for r in owner.rows if isinstance(r, RunInputAuthorizationRow) and r.run_id in parameters), None)
        turn = None if link is None else next((r for r in owner.rows if isinstance(r, RunAuthenticatedInputRow) and r.turn_id == link.turn_id), None)
        return result_rows([] if link is None or turn is None or not tenant_matches(link, statement) or not tenant_matches(turn, statement) else [(link, turn)])
    assert len(entities) == 1
    entity = entities[0]
    assert entity in (RunRow, RunAdmissionPoolRow, RunInputAuthorizationRow, RunAuthenticatedInputRow)
    for row in owner.rows:
        if type(row) is not entity or not tenant_matches(row, statement):
            continue
        if isinstance(row, RunRow):
            key = row.id
        elif isinstance(row, RunAuthenticatedInputRow):
            key = row.turn_id
        else:
            assert isinstance(row, RunAdmissionPoolRow | RunInputAuthorizationRow)
            key = row.run_id
        if key in parameters:
            return result_rows([(row,)])
    return result_rows([])


class PreparationStorage(async_sessionmaker[AsyncSession]):
    """Real AsyncSession transaction API; only SQL delivery is intercepted."""
    def __init__(self) -> None:
        self.now = datetime.now(UTC)
        self.rows: list[Base] = []
        self.authorization_reads = 0
        super().__init__(expire_on_commit=False, sync_session_class=PreparationSession,
                         info={'dynamic_resolution_preparation': self})

    async def get[T: Base](self, kind: type[T], key: UUID) -> T | None:
        async with self() as session:
            return await session.get(kind, key)
