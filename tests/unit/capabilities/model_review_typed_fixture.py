"""Typed surfaces for the model-review sequencing tests; no live authority."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NotRequired, Protocol, TypedDict
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities import model_review
from agent_hub.capabilities.review_contracts import FrozenReviewAction, ModelReviewVerdict
from agent_hub.capabilities.review_repository import ReviewOutcome
from agent_hub.models.run_admission_repository import (
    DeploymentIdentity,
    UsageObservation,
    _WorkerLease,
)
from agent_hub.models.types import ModelRequest, StructuredResponseSchema


class ModelReviewModule(Protocol):
    @property
    def ModelApprovalReviewService(self) -> type[model_review.ModelApprovalReviewService]: ...

    @property
    def _LiveReviewContext(self) -> type[model_review._LiveReviewContext]: ...

    @property
    def _request(
        self,
    ) -> Callable[[FrozenReviewAction, model_review._LiveReviewContext], ModelRequest]: ...

    @property
    def _review_schema(self) -> Callable[[], StructuredResponseSchema]: ...


class SnapshotArguments(TypedDict):
    role: Role
    authorization_turn_id: UUID
    authorization_sha256: str
    policy_sha256: str
    config_sha256: str
    source_binding: Mapping[str, object]
    normalized_effect: Mapping[str, object]


class SnapshotChanges(TypedDict, total=False):
    role: Role
    authorization_turn_id: UUID
    authorization_sha256: str
    policy_sha256: str
    config_sha256: str
    source_binding: Mapping[str, object]
    normalized_effect: Mapping[str, object]


class ReviewFinishArguments(TypedDict):
    tenant_id: UUID
    run_id: UUID
    review_id: UUID
    attempt_id: UUID
    request_sha256: str
    physical_identity: UUID | None
    lease: _WorkerLease
    actual_model_identity: DeploymentIdentity
    outcome: ReviewOutcome
    usage: NotRequired[UsageObservation | None]
    verdict: NotRequired[ModelReviewVerdict | None]


@dataclass
class ReviewContextSlot:
    """Deliberately inject a foreign object at the actual runtime type-check fence."""

    value: model_review._LiveReviewContext

    def inject(self, value: object) -> None:
        object.__setattr__(self, "value", value)
