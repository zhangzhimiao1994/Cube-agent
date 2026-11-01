"""Nominal in-memory SQL/JSON fixtures dedicated to physical-budget tests.

The real admission repository, transaction, lock/fence and lease checks run.
Only SQL row delivery is intercepted; async lock-wait hooks still suspend inside
SQLAlchemy's greenlet bridge. No PostgreSQL or provider connection is opened.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, TypedDict
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.engine import Result
from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.sql import Select
from sqlalchemy.util.concurrency import await_only

from agent_hub.db.models import (
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunRow,
)
from agent_hub.models import physical_budget
from agent_hub.models.admission_bounds import KnownPricing
from agent_hub.models.capacity import CapacityLease
from agent_hub.models.run_admission_repository import (
    UsageObservation,
    _ExecutionArguments,
    _FinishArguments,
    _IssueArguments,
    _ReserveArguments,
    _WorkerLease,
)
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    StructuredResponseSchema,
    TokenUsage,
    ToolDefinition,
)
from tests.unit.models.test_gateway import CapacityStub, lease

type BudgetCall = (
    tuple[Literal["reserve"], _ReserveArguments]
    | tuple[Literal["issued"], _IssueArguments]
    | tuple[Literal["finish"], _FinishArguments]
)
type WireJson = str | int | float | bool | None | list[WireJson] | dict[str, WireJson]


class PhysicalBudgetModule(Protocol):
    """Actual private module surface used by this fixture and its callers."""

    @property
    def _RunBudgetContext(self) -> type[physical_budget._RunBudgetContext]: ...

    @property
    def _BudgetAdmission(self) -> type[physical_budget._BudgetAdmission]: ...

    @property
    def _bind_run_budget(
        self,
    ) -> Callable[[physical_budget._RunBudgetContext], AbstractContextManager[None]]: ...

    @property
    def _usage_observation(
        self,
    ) -> Callable[[TokenUsage | None, KnownPricing], UsageObservation | None]: ...


class ModelRequestChanges(TypedDict, total=False):
    logical_model: str
    messages: tuple[ModelMessage, ...]
    required_capabilities: frozenset[ModelCapability]
    timeout_seconds: float
    allow_fallback: bool
    max_output_tokens: int
    response_schema: StructuredResponseSchema | None
    tools: tuple[ToolDefinition, ...]
    native_tool_continuation: bool
    required_tool: str | None


def wire_json(value: object) -> WireJson:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [wire_json(item) for item in value]
    assert isinstance(value, dict)
    result: dict[str, WireJson] = {}
    for key, item in value.items():
        assert isinstance(key, str)
        result[key] = wire_json(item)
    return result


def wire_object(raw: bytes) -> dict[str, WireJson]:
    value: object = json.loads(raw)
    result = wire_json(value)
    assert isinstance(result, dict)
    return result


async def capture_void_return[T](operation: Awaitable[T]) -> tuple[T]:
    """Retain the actual return for assertions without widening its type."""
    return (await operation,)


@dataclass
class FaultableExecutionBinding:
    """Explicit test-only fault injection; production kwargs keep their real types."""

    tenant_id: UUID
    run_id: UUID
    authorization_turn_id: UUID
    lease: _WorkerLease
    config_sha256: str

    def inject(self, changes: Mapping[str, object]) -> None:
        for key, value in changes.items():
            assert key in {"tenant_id", "lease", "config_sha256"}
            # Deliberately violate the declared field invariant, as other tests
            # mutate frozen lease/request fields to test the actual runtime fence.
            object.__setattr__(self, key, value)

    def arguments(self) -> _ExecutionArguments:
        return {
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "authorization_turn_id": self.authorization_turn_id,
            "lease": self.lease,
            "config_sha256": self.config_sha256,
        }


class FenceState:
    calls: list[BudgetCall]
    current_run: RunRow
    current_pool: RunAdmissionPoolRow | None
    current_link: RunInputAuthorizationRow
    current_turn: RunAuthenticatedInputRow

    def __init__(self) -> None:
        self.current_reads: list[tuple[str, int]] = []
        self.before_current_read: Callable[[], Awaitable[None]] | None = None
        self.current_read_error: Exception | None = None

    def observed(self, kind: str) -> None:
        finishes = sum(name == "finish" for name, _ in self.calls)
        self.current_reads.append((kind, finishes))


class CurrentFenceSession(Session):
    pass


def single_rows[T](values: Sequence[T]) -> IteratorResult[tuple[T]]:
    return IteratorResult(SimpleResultMetaData(["fixture_row"]), iter((value,) for value in values))


@event.listens_for(CurrentFenceSession, "do_orm_execute")
def current_fence_rows(
    state: ORMExecuteState,
) -> (
    Result[tuple[RunRow]]
    | Result[tuple[RunAdmissionPoolRow]]
    | Result[tuple[RunInputAuthorizationRow, RunAuthenticatedInputRow]]
    | Result[tuple[datetime]]
):
    store = state.session.info["physical_budget_fence"]
    assert isinstance(store, FenceState)
    statement = state.statement
    assert isinstance(statement, Select)
    entity = statement.column_descriptions[0].get("entity")
    if entity is RunRow:
        store.observed("run")
        if store.before_current_read is not None:
            await_only(store.before_current_read())
        if store.current_read_error is not None:
            raise store.current_read_error
        return single_rows([store.current_run])
    if entity is RunAdmissionPoolRow:
        store.observed("pool")
        return single_rows([] if store.current_pool is None else [store.current_pool])
    if entity is RunInputAuthorizationRow:
        store.observed("authorization")
        return IteratorResult(
            SimpleResultMetaData(["authorization", "authenticated_input"]),
            iter([(store.current_link, store.current_turn)]),
        )
    assert len(statement.column_descriptions) == 1
    assert str(statement.column_descriptions[0]["expr"]) == "clock_timestamp()"
    store.observed("clock")
    return single_rows([datetime.now(UTC)])


class SelectedCapacity(CapacityStub):
    """Same acquisition spy without assigning an untyped closure to a method."""

    async def acquire(
        self,
        candidates: Sequence[Deployment],
        wait_timeout: float,
        *,
        estimated_tokens: int | Mapping[str, int],
    ) -> CapacityLease:
        self.events.append(("acquire", tuple(item.id for item in candidates)))
        return lease(candidates[0].id)
