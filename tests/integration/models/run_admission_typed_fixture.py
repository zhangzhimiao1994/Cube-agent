"""Types for real PostgreSQL admission fixtures; no repository or SQL substitute."""
import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Protocol, TypedDict, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.models.run_admission_repository import DeploymentIdentity

type Storage = tuple[async_sessionmaker[AsyncSession], list[UUID]]


class PoolLimits(TypedDict, total=False):
    original_token_grant: int
    original_deadline: datetime
    review_token_limit: int
    review_count_limit: int
    review_cost_limit: Decimal


class ReopenChanges(PoolLimits, total=False):
    config_sha256: str


class ReservationOverrides(TypedDict, total=False):
    request_sha256: str
    deployment: DeploymentIdentity
    token_hold: int
    review_cost_hold: Decimal | None


class BindingChanges(TypedDict, total=False):
    tenant_id: UUID
    authorization_turn_id: UUID
    config_sha256: str


class EventReason(TypedDict, total=False):
    reason: str


class LockWaitStarted(asyncio.Event):
    transaction_start: datetime | None = None


@runtime_checkable
class MigrationModule(Protocol):
    def downgrade(self) -> None: ...


def present[T](value: T | None) -> T:
    """Require the actual selected row/value; absence never becomes fabricated data."""
    assert value is not None
    return value
