"""Typed local transports and explicit malformed-input seams for review tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol, TypedDict
from uuid import UUID

from sqlalchemy import Table, event
from sqlalchemy.engine import Engine, Result
from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import ORMExecuteState

from agent_hub.auth.models import Role
from agent_hub.capabilities import review_repository
from agent_hub.models.run_admission_repository import (
    DeploymentIdentity,
    UsageObservation,
    _WorkerLease,
)
from agent_hub.runs.authorization import _AuthenticatedInput


class ActionArguments(TypedDict, total=False):
    role: Role
    authorization_turn_id: UUID
    authorization_sha256: str
    config_sha256: str
    policy_sha256: str
    source_binding: Mapping[str, object]
    normalized_effect: Mapping[str, object]


def replaced_evidence(
    evidence: _AuthenticatedInput, field: str, value: object
) -> _AuthenticatedInput:
    # dataclasses.replace accepts arbitrary runtime values; validation belongs to
    # the production hash boundary, including malformed hashes and private seal.
    constructor: object = replace
    assert callable(constructor)
    result: object = constructor(evidence, **{field: value})
    assert isinstance(result, _AuthenticatedInput)
    return result


def untrusted_reservation(values: Mapping[str, object]) -> review_repository._ReviewReservation:
    constructor: object = review_repository._ReviewReservation
    assert callable(constructor)
    result: object = constructor(**values)
    assert isinstance(result, review_repository._ReviewReservation)
    return result


def untrusted_usage(values: tuple[object, object, object, object]) -> UsageObservation:
    constructor: object = UsageObservation
    assert callable(constructor)
    result: object = constructor(*values)
    assert isinstance(result, UsageObservation)
    return result


def freeze_untrusted_usage(
    usage: object, lease: _WorkerLease, model: DeploymentIdentity
) -> tuple[object, _WorkerLease, DeploymentIdentity, object]:
    freeze: object = review_repository._freeze_finish_arguments
    assert callable(freeze)
    result: object = freeze(usage, lease, model, None)
    assert isinstance(result, tuple) and len(result) == 4
    frozen, owned_lease, owned_model, verdict = result
    assert isinstance(owned_lease, _WorkerLease)
    assert isinstance(owned_model, DeploymentIdentity)
    return frozen, owned_lease, owned_model, verdict


def observation(value: object) -> dict[str, object] | None:
    from agent_hub.models.run_admission_repository import _observation

    observe: object = _observation
    assert callable(observe)
    result: object = observe(value)
    if result is None:
        return None
    return mapping(result)


def mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    result: dict[str, object] = {}
    for key, item in value.items():
        assert isinstance(key, str)
        result[key] = item
    return result


def reclassify(value: object, cls: type[object]) -> None:
    attribute = "__class__"
    setattr(value, attribute, cls)


def text(value: object) -> str:
    assert isinstance(value, str)
    return value


def present[T](value: T | None) -> T:
    assert value is not None
    return value


class SQLSession(AsyncSession):
    """Execute unchanged production SQL on SQLite, with PG-style UTC results."""

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.sql_connection = engine.connect()
        event.listen(self.sync_session, "do_orm_execute", self._execute_sql, retval=True)

    def _execute_sql(self, state: ORMExecuteState) -> Result[tuple[object, ...]]:
        result = self.sql_connection.execute(state.statement)
        values: list[tuple[object, ...]] = [
            tuple(
                value.replace(tzinfo=UTC)
                if isinstance(value, datetime) and value.tzinfo is None
                else value
                for value in row
            )
            for row in result.all()
        ]
        return IteratorResult(SimpleResultMetaData(list(result.keys())), iter(values))

    async def __aexit__(self, *args: object) -> None:
        await self.close()
        self.sql_connection.close()


class SQLSessions(async_sessionmaker[AsyncSession]):
    def __init__(self, engine: Engine, session_type: type[SQLSession] = SQLSession) -> None:
        super().__init__()
        self.engine = engine
        self.session_type = session_type

    def __call__(self, **local_kw: object) -> SQLSession:
        return self.session_type(self.engine)


class Seed(Protocol):
    def __call__(self, count: int = 1) -> None: ...


@dataclass
class ReviewStore:
    engine: Engine
    reviews: Table
    owners: Table
    seed: Seed
    statements: list[str]
    factory: async_sessionmaker[AsyncSession]


class UnexpectedTaskQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        raise AssertionError("read-only model review history must not enqueue work")
