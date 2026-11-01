"""Typed checkpoint/failure seams; no grant, fallback data or service execution."""
from collections.abc import Mapping
from typing import Never

import pytest
from sqlalchemy import Table

from agent_hub.db.base import Base
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import (
    Artifact,
    JsonValue,
    RuntimeCheckpoint,
    TaskContext,
    _freeze_object,
)

type ProofCall = tuple[TaskContext, RuntimeCheckpoint | None, RuntimeCheckpoint, tuple[Artifact, ...]]
type ProofCalls = list[ProofCall]


def present[T](value: T | None) -> T:
    assert value is not None
    return value


def number(value: object) -> int | float:
    assert isinstance(value, int | float) and not isinstance(value, bool)
    return value


def text(value: object) -> str:
    assert isinstance(value, str)
    return value


def checkpoint_state(value: Mapping[str, object]) -> Mapping[str, JsonValue]:
    return _freeze_object(value, name='checkpoint state')


def model_table(model: type[Base]) -> Table:
    table = model.__table__
    assert isinstance(table, Table)
    return table


class NoExecutionService(RunService):
    """Nominal service receiver that fails on any attempted attribute access."""
    def __init__(self) -> None:
        pass

    def __getattribute__(self, name: str) -> Never:
        pytest.fail('verification diagnostic attempted service work: ' + name)
