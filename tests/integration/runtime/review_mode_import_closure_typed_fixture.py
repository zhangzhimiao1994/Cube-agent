"""Typed IO contracts owned by the review-mode import-closure test slice."""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, TypedDict, runtime_checkable
from uuid import UUID

from alembic.operations import Operations
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.routers.runs import CreateRunRequest
from agent_hub.auth.models import Role
from agent_hub.capabilities.defaults import RuntimeCapabilityStack
from agent_hub.capabilities.review_contracts import (
    ApprovalReviewConfig,
    FrozenReviewAction,
    ModelReviewVerdict,
)
from agent_hub.capabilities.review_repository import ReviewOutcome, _ReviewReservation
from agent_hub.config.service import ConfigService
from agent_hub.models.capacity import CapacityPool
from agent_hub.models.gateway import CapacityController, ModelTransport
from agent_hub.models.run_admission_repository import (
    DeploymentIdentity,
    UsageObservation,
    _ExecutionArguments,
    _WorkerLease,
)
from agent_hub.models.types import Deployment
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.artifacts import ArtifactRepository
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.defaults import (
    ConfigBackedDirectRuntime,
    ConfigBackedDiscussionRuntime,
    ConfigBackedDispatchRuntime,
    ConfigBackedHybridRuntime,
    HarnessToolInvoker,
    RuntimeCapabilityGatewayProtocol,
)
from agent_hub.runtime.review_assembly import ReviewAssemblyBroker
from agent_hub.security.secrets import SecretService

if TYPE_CHECKING:
    from tests.integration.runtime.test_model_review_modes_helper import FixedWire

type NativeRuntime = (
    ConfigBackedDirectRuntime
    | ConfigBackedDispatchRuntime
    | ConfigBackedDiscussionRuntime
    | ConfigBackedHybridRuntime
)


class SnapshotChanges(TypedDict, total=False):
    request: Mapping[str, object]
    role: Role
    authorization_turn_id: UUID
    authorization_sha256: str
    policy_sha256: str
    config_sha256: str
    source_binding: Mapping[str, object]
    normalized_effect: Mapping[str, object]


class BeginChanges(TypedDict, total=False):
    snapshot: FrozenReviewAction
    config: ApprovalReviewConfig
    reservation: _ReviewReservation
    review_id: UUID


class BeginArguments(TypedDict):
    snapshot: FrozenReviewAction
    config: ApprovalReviewConfig
    reservation: _ReviewReservation
    review_id: UUID


class FinishArguments(TypedDict):
    tenant_id: UUID
    run_id: UUID
    review_id: UUID
    attempt_id: UUID
    request_sha256: str
    physical_identity: UUID | None
    lease: _WorkerLease
    actual_model_identity: DeploymentIdentity
    outcome: ReviewOutcome
    verdict: ModelReviewVerdict | None
    usage: UsageObservation | None


class ReceiptChanges(TypedDict, total=False):
    tenant_id: UUID
    run_id: UUID
    review_id: UUID
    attempt_id: UUID
    request_sha256: str
    physical_identity: UUID | None
    lease: _WorkerLease
    actual_model_identity: DeploymentIdentity
    verdict: ModelReviewVerdict | None


@runtime_checkable
class MigrationModule(Protocol):
    op: Operations | ModuleType

    def downgrade(self) -> None: ...


@runtime_checkable
class UUIDConstructor(Protocol):
    def __call__(self, value: str) -> UUID: ...


def driver_uuid(value: str) -> UUID:
    """Use the actual extension constructor, verifying its untyped output boundary."""
    module = importlib.import_module("asyncpg.pgproto.pgproto")
    constructor: object = vars(module)["UUID"]
    assert isinstance(constructor, UUIDConstructor)
    identifier = constructor(value)
    assert isinstance(identifier, UUID) and type(identifier) is not UUID
    return identifier


class DecimalValue(TypedDict):
    decimal: str


class WireUsage(TypedDict):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: DecimalValue


class WireDeployment(TypedDict):
    provider_id: str
    deployment_id: str
    model_id: str


class WireAttempt(TypedDict):
    request_sha256: str
    purpose: str
    physical_transport_id: str | None
    deployment: WireDeployment
    usage: WireUsage


class WireReceipt(TypedDict):
    outcome: str
    deployment: WireDeployment
    attempt_id: str
    physical_transport_id: str
    usage: WireUsage


class PoolCharge(Protocol):
    @property
    def token_charged(self) -> int: ...


@dataclass
class TokenPool:
    token_charged: int


class LeaseBinding(TypedDict):
    lease: _WorkerLease


class RuntimeArguments(TypedDict):
    config_service: ConfigService
    secret_service: SecretService
    capacity_factory: Callable[
        [UUID, Sequence[Deployment]], Awaitable[CapacityController | CapacityPool]
    ]
    transport: ModelTransport
    capability_gateway: RuntimeCapabilityGatewayProtocol
    harness_tool_gateway: HarnessToolInvoker
    artifact_repository: ArtifactRepository


@dataclass
class NativeCase:
    factory: async_sessionmaker[AsyncSession]
    repository: RunRepository
    broker: ReviewAssemblyBroker
    context: TaskContext
    binding: _ExecutionArguments
    wire: FixedWire
    runtime: NativeRuntime
    stack: RuntimeCapabilityStack
    capacities: list[CapacityPool]
    root: Path
    config: ApprovalReviewConfig
    redis: Redis
    body: CreateRunRequest
