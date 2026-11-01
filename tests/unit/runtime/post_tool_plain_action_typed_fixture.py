"""Typed offline receipt and plain-action seams; no provider or native grant."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Never
from uuid import UUID, uuid4

from agent_hub.capabilities.review_repository import (
    ReviewIntent,
    ReviewRepository,
    _CurrentReviewBinding,
)
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import ConfigService
from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    ScopeIncompletePhase,
    ScopeIncompleteReason,
    _GatewayFailureHistory,
)
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    RejectedOutputEvidence,
    TokenUsage,
)
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import JsonValue, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
from agent_hub.runtime.defaults import ConfigBackedDirectRuntime
from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository
from tests.unit.capabilities.review_reachability_typed_fixture import (
    ReachabilitySetup,
    SandboxProfile,
)
from tests.unit.models.test_gateway import deployment
from tests.unit.runs.project_auto_repair_typed_fixture import (
    integer,
    json_map,
    mapping,
    present,
    string,
)
from tests.unit.runtime.crew.required_correction_typed_fixture import native
from tests.unit.runtime.crew.test_adapter_failure_reason import _workspace_delivery_sequence_context
from tests.unit.runtime.crew.test_meaningful_workspace_progress import Observations
from tests.unit.runtime.crew.test_native_tool_continuation import subject as native_subject

__all__ = ["integer", "json_map", "mapping", "native", "present", "string"]

FINAL = ModelResponse(text="Workspace delivered with its actual bundle.", usage=TokenUsage(1, 1, 2))
HANDOFF = ModelResponse(
    text="The source was written; its bundle is still required.", usage=TokenUsage(1, 1, 2)
)


def model_rows(value: object) -> dict[str, Mapping[str, JsonValue]]:
    return {key: json_map(row) for key, row in mapping(value).items()}


class EstimateOnlyIO:
    async def resolve(self, secret_ref: str) -> Never:
        raise AssertionError("estimator-only fixture must not resolve credentials")

    async def complete(self, deployment: Deployment, request: ModelRequest, api_key: str) -> Never:
        raise AssertionError("estimator-only fixture must not dispatch a provider request")


class NativeGateway:
    """Original synthetic receipt sequence with the actual bounded status type."""

    def __init__(
        self,
        *outcomes: ModelResponse | str,
        status: Literal["completed", "incomplete", "refused", "unknown"] = "incomplete",
        trusted: bool = True,
        entries: int = 1,
        usage_known: bool = True,
    ) -> None:
        self.outcomes = outcomes
        self.requests: list[ModelRequest] = []
        self.status, self.trusted, self.entries, self.usage_known = (
            status,
            trusted,
            entries,
            usage_known,
        )
        self._registry = ModelRegistry(
            (
                deployment(
                    "primary",
                    logical_model="general",
                    capabilities=frozenset(ModelCapability),
                    max_output_tokens=100_000,
                    context_window_tokens=1_000_000,
                ),
            )
        )

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        assert len(self.requests) <= len(self.outcomes), "unexpected extra model request"
        outcome = self.outcomes[len(self.requests) - 1]
        if outcome == "incomplete":
            error = GatewayRejectedOutput(
                evidence=RejectedOutputEvidence(
                    final_text=None,
                    status=self.status,
                    reason="incomplete",
                    usage=TokenUsage(1, 16, 17) if self.usage_known else None,
                    usage_status="known" if self.usage_known else "missing",
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/chat",
                cost_usd=Decimal(0),
            )
            if self.trusted:
                history = _GatewayFailureHistory(entered_count=self.entries)
                history.mark_incomplete(
                    ScopeIncompletePhase.TRANSPORT, ScopeIncompleteReason.REJECTED_OUTPUT
                )
                history.attach_diagnostic(error)
            raise error
        if outcome == "cancel":
            raise asyncio.CancelledError
        assert isinstance(outcome, ModelResponse)
        return GatewayCompletion(
            response=outcome,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/chat",
            cost_usd=Decimal(0),
        )


def runtime_for(
    gateway: NativeGateway,
    harness: Observations | None = None,
    private: InMemoryProviderContinuationRepository | None = None,
    artifacts: InMemoryArtifactRepository | None = None,
) -> CrewDispatchRuntime:
    runtime = native_subject(
        gateway,
        harness if harness is not None else Observations(),
        private if private is not None else InMemoryProviderContinuationRepository(),
    )
    runtime._plan = runtime._plan.model_copy(
        update={
            "total_token_budget": 100_000,
            "total_cost_usd": Decimal("0.10"),
            "steps": (
                runtime._plan.steps[0].model_copy(
                    update={
                        "token_budget": 100_000,
                        "cost_budget_usd": Decimal("0.10"),
                    }
                ),
            ),
        }
    )
    if artifacts is not None:
        runtime._artifact_repository = artifacts
    return runtime


def context() -> TaskContext:
    return _workspace_delivery_sequence_context().model_copy(update={"token_budget": 100_000})


type RoomCase = tuple[CrewDispatchRuntime, TaskContext, NativeGateway, Observations, list[RunEvent]]


class ReviewRepositoryForGate(ReviewRepository):
    async def lookup_effect(
        self,
        tenant_id: UUID,
        run_id: UUID,
        authorization_turn_id: UUID,
        effect_digest: str,
    ) -> ReviewIntent | None:
        return await super().lookup_effect(tenant_id, run_id, authorization_turn_id, effect_digest)

    async def consume_permit(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        review_id: UUID,
        current_binding: _CurrentReviewBinding,
    ) -> bool:
        return await super().consume_permit(
            tenant_id=tenant_id,
            run_id=run_id,
            review_id=review_id,
            current_binding=current_binding,
        )


@dataclass
class NoteSetup(ReachabilitySetup):
    path: str = field(init=False)
    content: str = field(init=False)
    wire: list[dict[str, object]] = field(init=False)
    runtime: ConfigBackedDirectRuntime = field(init=False)


def note_setup(base: ReachabilitySetup) -> NoteSetup:
    result = NoteSetup(
        base.task,
        base.budget,
        base.runs,
        base.backend,
        base.calls,
        base.settings,
        base.policy,
        base.permits,
        base.reviewer,
        base.loader,
        base.gate,
        base.harness,
        base.request,
    )
    result.db, result.ledger, result.fixed, result.body = (
        base.db,
        base.ledger,
        base.fixed,
        base.body,
    )
    return result


def profile_for(barrier: str) -> SandboxProfile:
    if barrier == "read_only":
        return "read_only"
    if barrier == "restricted":
        return "restricted"
    if barrier == "none":
        return "none"
    return "workspace_write"


class NoteConfigs(ConfigService):
    def __init__(self, platform: PlatformConfig) -> None:
        self.platform = platform

    async def get_current(self, tenant_id: UUID) -> ConfigRevision:
        return ConfigRevision(
            uuid4(),
            tenant_id,
            1,
            ConfigStatus.PUBLISHED,
            self.platform.model_dump(mode="json"),
            None,
            datetime.now(UTC),
        )
