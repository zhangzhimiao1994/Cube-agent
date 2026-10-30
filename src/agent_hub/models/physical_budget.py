"""Private durable admission adapter for an ALREADY opened original-run pool.

Nothing here opens a pool, derives authority from model arguments, or installs a
runtime binding. The worker/API owner supplies authenticated evidence, lease,
original deadline and independently established provider accounting contracts.
"""

from __future__ import annotations

import asyncio
import math
import time
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.models.admission_bounds import (
    AdmissionBounds,
    KnownPricing,
    TrustedContextBound,
    compute_admission_bounds,
    freeze_model_request,
)
from agent_hub.models.failure_receipt import _valid_usage
from agent_hub.models.physical_admission import (
    _bind_physical_selection,
    _PhysicalAdmissionScope,
    _PhysicalAdmissionStopped,
    _PhysicalAttemptMetadata,
    _PhysicalPermit,
    _PhysicalReceipt,
    _PhysicalSelection,
    _resume_physical_admission,
)
from agent_hub.models.run_admission_repository import (
    DeploymentIdentity,
    PoolSnapshot,
    RunAdmissionRepository,
    UsageObservation,
    _WorkerLease,
)
from agent_hub.models.types import Deployment, ModelRequest, TokenUsage
from agent_hub.runs.authorization import _AuthenticatedInput, _validate_authenticated_input

if TYPE_CHECKING:
    from agent_hub.models.gateway import DeploymentPricing


def _config_sha(config: ApprovalReviewConfig) -> str:
    # models.__init__ exports Gateway; importing 2C eagerly here would cycle
    # when a caller imports ReviewRepository before importing Gateway.
    from agent_hub.capabilities.review_repository import review_config_sha256

    return review_config_sha256(config)


def _identity(deployment: Deployment) -> DeploymentIdentity:
    return DeploymentIdentity(
        deployment.provider_model.split("/", 1)[0], deployment.id, deployment.provider_model
    )


def _same_model(left: DeploymentIdentity, right: DeploymentIdentity) -> bool:
    # A new alias, deployment or credential for the same underlying model does
    # not establish independence. Unknown identity is rejected by the caller.
    return left.deployment_id == right.deployment_id or (
        left.provider_id == right.provider_id and left.model_id == right.model_id
    )


def _wire_model(deployment: Deployment) -> str:
    # Conservative equivalence: changing a gateway prefix or key cannot make
    # the same configured actual wire model an independent reviewer.
    return (deployment.request_model or deployment.provider_model).rsplit("/", 1)[-1]


def _usage_observation(usage: TokenUsage | None, pricing: KnownPricing) -> UsageObservation | None:
    if not _valid_usage(usage):
        return None
    assert usage is not None
    pricing.__post_init__()

    # Exact integer microdollars; independent of ambient Decimal context/traps.
    def micros(rate: Decimal) -> int:
        numerator, denominator = rate.as_integer_ratio()
        return numerator * 1_000_000 // denominator

    numerator = usage.prompt_tokens * micros(
        pricing.input_per_million_usd
    ) + usage.completion_tokens * micros(pricing.output_per_million_usd)
    amount = (numerator + 999_999) // 1_000_000
    dollars, fraction = divmod(amount, 1_000_000)
    return UsageObservation(
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
        Decimal(f"{dollars}.{fraction:06d}"),
    )


class _RunBudgetContext:
    """Revocable server continuity, never an agent DTO or a new budget grant."""

    def __init__(
        self,
        *,
        repository: RunAdmissionRepository,
        pool: PoolSnapshot,
        evidence: _AuthenticatedInput,
        lease: _WorkerLease,
        config: ApprovalReviewConfig,
        deadline: float,
        trusted_contexts: tuple[TrustedContextBound, ...] = (),
        acting_model: DeploymentIdentity | None = None,
    ) -> None:
        captured_monotonic = time.monotonic()
        try:
            if (
                type(pool) is not PoolSnapshot
                or type(evidence) is not _AuthenticatedInput
                or type(lease) is not _WorkerLease
                or type(config) is not ApprovalReviewConfig
                or not isinstance(repository, RunAdmissionRepository)
                or type(deadline) not in (float, int)
                or not math.isfinite(deadline)
                or pool.closed
                or type(pool.run_id) is not UUID
            ):
                raise ValueError
            _validate_authenticated_input(
                evidence, pool.tenant_id, evidence.actor_id, evidence.actor_role, evidence.message
            )
            lease.__post_init__()
            config_copy = ApprovalReviewConfig.from_payload(config.to_payload())
            if (
                pool.authorization_turn_id != evidence.turn_id
                or pool.config_sha256 != _config_sha(config_copy)
                or config_copy.engine != "rules_then_model"
                or pool.original_deadline.tzinfo is None
            ):
                raise ValueError
            remaining = (pool.original_deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0 or deadline <= time.monotonic():
                raise ValueError
            contracts = tuple(
                TrustedContextBound(
                    c.deployment_id, c.logical_model, c.provider_model, c.total_tokens
                )
                for c in trusted_contexts
            )
            if len({c.deployment_id for c in contracts}) != len(contracts):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid trusted run budget context") from None
        self.repository = repository
        self._pool = replace(pool)
        self._evidence = replace(evidence)
        self._lease = _WorkerLease(lease.worker_id, lease.token)
        self._config = config_copy
        self._deadline = min(float(deadline), captured_monotonic + remaining)
        self._trusted_contexts = contracts
        self._acting_model = (
            None
            if acting_model is None
            else DeploymentIdentity(
                acting_model.provider_id, acting_model.deployment_id, acting_model.model_id
            )
        )
        self._acting_models = [] if self._acting_model is None else [self._acting_model]
        self._acting_wire_models: set[str] = set()
        self._active = True
        self._scopes: weakref.WeakSet[_PhysicalAdmissionScope] = weakref.WeakSet()

    @property
    def tenant_id(self) -> UUID:
        return self._pool.tenant_id

    @property
    def run_id(self) -> UUID:
        return self._pool.run_id

    @property
    def evidence(self) -> _AuthenticatedInput:
        return replace(self._evidence)

    @property
    def lease(self) -> _WorkerLease:
        return replace(self._lease)

    @property
    def config(self) -> ApprovalReviewConfig:
        return ApprovalReviewConfig.from_payload(self._config.to_payload())

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def acting_model(self) -> DeploymentIdentity | None:
        return self._acting_model

    @property
    def acting_models(self) -> tuple[DeploymentIdentity, ...]:
        return tuple(self._acting_models)

    @property
    def acting_wire_models(self) -> frozenset[str]:
        return frozenset(self._acting_wire_models)

    def revoke(self) -> None:
        self._active = False
        for scope in tuple(self._scopes):
            scope.revoke()

    def _register_scope(self, scope: _PhysicalAdmissionScope) -> None:
        if not self._active:
            scope.revoke()
            raise _PhysicalAdmissionStopped()
        self._scopes.add(scope)

    def check(self) -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        if not self._active or time.monotonic() >= self.deadline:
            raise _PhysicalAdmissionStopped()

    def bounds(
        self,
        deployment: Deployment,
        request: ModelRequest,
        pricing: KnownPricing | None,
    ) -> AdmissionBounds:
        self.check()
        contract = next(
            (c for c in self._trusted_contexts if c.deployment_id == deployment.id), None
        )
        return compute_admission_bounds(
            deployment, request, trusted_context=contract, effective_pricing=pricing
        )


_run_budget: ContextVar[_RunBudgetContext | None] = ContextVar("_run_budget", default=None)


@contextmanager
def _bind_run_budget(context: _RunBudgetContext) -> Iterator[None]:
    if type(context) is not _RunBudgetContext or _run_budget.get() is not None:
        raise _PhysicalAdmissionStopped()
    context.check()
    token = _run_budget.set(context)
    try:
        yield
    finally:
        context.revoke()
        _run_budget.reset(token)


@dataclass
class _BudgetAttempt:
    bounds: AdmissionBounds = field(repr=False)
    physical_id: UUID
    receipt: _PhysicalReceipt | None = field(default=None, repr=False)
    settled: bool = False


class _BudgetAdmission:
    purpose: Literal["primary", "review"] = "primary"

    def __init__(self, context: _RunBudgetContext) -> None:
        self.context = context
        self._selections: list[tuple[_PhysicalSelection, AdmissionBounds]] = []
        self._attempts: dict[UUID, _BudgetAttempt] = {}

    def select(
        self,
        deployment: Deployment,
        request: ModelRequest,
        pricing: KnownPricing | None,
    ) -> tuple[Deployment, ModelRequest, _PhysicalSelection]:
        bounds = self.context.bounds(deployment, request, pricing)
        # Selection carries effective final prices, while bounds owns the full
        # original deployment config and request snapshots separately.
        rates = bounds.pricing()
        owned = replace(
            bounds.deployment(),
            input_per_million_usd=rates.input_per_million_usd,
            output_per_million_usd=rates.output_per_million_usd,
        )
        selection = _PhysicalSelection(
            bounds.request_digest, owned, bounds.total_tokens, bounds.cost_usd
        )
        self._selections.append((selection, bounds))
        return owned, bounds.request(), selection

    def _selected(self, meta: _PhysicalAttemptMetadata, deadline: float) -> AdmissionBounds:
        self.context.check()
        if meta.purpose != self.purpose or deadline > self.context.deadline:
            raise _PhysicalAdmissionStopped()
        bounds = next((b for s, b in self._selections if s is meta.selection), None)
        if bounds is None:
            raise _PhysicalAdmissionStopped()
        d = bounds.deployment()
        if (meta.deployment_id, meta.provider_model, meta.request_sha256) != (
            d.id,
            d.provider_model,
            bounds.request_digest,
        ):
            raise _PhysicalAdmissionStopped()
        return bounds

    async def before_send(
        self, meta: _PhysicalAttemptMetadata, *, deadline: float
    ) -> _PhysicalPermit:
        bounds = self._selected(meta, deadline)
        context = self.context
        attempt_id, physical_id = uuid4(), uuid4()
        await context.repository.reserve(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            authorization_turn_id=context.evidence.turn_id,
            lease=context.lease,
            config_sha256=_config_sha(context.config),
            attempt_id=attempt_id,
            purpose="primary",
            request_sha256=bounds.request_digest,
            deployment=_identity(bounds.deployment()),
            token_hold=bounds.total_tokens,
            review_cost_hold=None,
        )
        # Each public method returns only after its transaction commits.
        issued = await context.repository.mark_issued(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            authorization_turn_id=context.evidence.turn_id,
            lease=context.lease,
            config_sha256=_config_sha(context.config),
            attempt_id=attempt_id,
            physical_transport_id=physical_id,
        )
        if not issued:
            raise _PhysicalAdmissionStopped()
        identity = _identity(bounds.deployment())
        if identity not in context._acting_models:
            context._acting_models.append(identity)
        context._acting_wire_models.add(_wire_model(bounds.deployment()))
        self._attempts[attempt_id] = _BudgetAttempt(bounds, physical_id)
        return _PhysicalPermit(attempt_id)

    async def after_receipt(
        self,
        permit: _PhysicalPermit,
        receipt: _PhysicalReceipt,
        *,
        deadline: float,
    ) -> None:
        attempt = self._attempts[permit.attempt_id]
        if attempt.receipt is not None and attempt.receipt != receipt:
            raise _PhysicalAdmissionStopped()
        attempt.receipt = receipt
        observed = _usage_observation(receipt.usage, attempt.bounds.pricing())
        known = (
            receipt.outcome in ("completed", "rejected")
            and receipt.delegated
            and receipt.usage_status == "known"
        )
        # Retained earlier valid tokens remain evidence, but conflicting usage,
        # cancellation and errors cannot acquire known cost or receive a refund.
        usage = (
            observed
            if known or observed is None
            else UsageObservation(
                observed.prompt_tokens, observed.completion_tokens, observed.total_tokens, None
            )
        )
        await self.context.repository.finish(
            tenant_id=self.context.tenant_id,
            run_id=self.context.run_id,
            attempt_id=permit.attempt_id,
            lease=self.context.lease,
            deployment=_identity(attempt.bounds.deployment()),
            outcome="completed" if usage is not None else "unknown",
            usage=usage,
        )
        attempt.settled = True


_review_admission: ContextVar[_BudgetAdmission | None] = ContextVar(
    "_review_admission", default=None
)


@contextmanager
def _bind_review_admission(admission: _BudgetAdmission) -> Iterator[None]:
    if _review_admission.get() is not None or admission.purpose != "review":
        raise _PhysicalAdmissionStopped()
    token = _review_admission.set(admission)
    try:
        yield
    finally:
        _review_admission.reset(token)


class _BudgetCall:
    def __init__(self, context: _RunBudgetContext, request: ModelRequest) -> None:
        self.request = freeze_model_request(request).request()
        self.admission = _review_admission.get() or _BudgetAdmission(context)
        if self.admission.context is not context:
            raise _PhysicalAdmissionStopped()
        self.context = context
        self.deadline = min(context.deadline, time.monotonic() + self.request.timeout_seconds)
        self.scope = _PhysicalAdmissionScope(
            self.admission,
            request_sha256=freeze_model_request(self.request).digest,
            purpose=self.admission.purpose,
            deadline=self.deadline,
            max_attempts=1 if self.admission.purpose == "review" else 2**63 - 1,
        )
        context._register_scope(self.scope)

    def check(self) -> None:
        self.context.check()
        self.scope.check()


_budget_call: ContextVar[_BudgetCall | None] = ContextVar("_budget_call", default=None)


def _capture_budget_call(request: ModelRequest, transport: object) -> _BudgetCall | None:
    context = _run_budget.get()
    if context is None:
        if _review_admission.get() is not None:
            raise _PhysicalAdmissionStopped()
        return None
    # Exact controlled transport, not a self-declared marker on a custom adapter.
    from agent_hub.models.litellm_client import LiteLLMClient

    if type(transport) is not LiteLLMClient:
        raise _PhysicalAdmissionStopped()
    return _BudgetCall(context, request)


@contextmanager
def _resume_budget_call(call: _BudgetCall | None, *, check: bool = True) -> Iterator[None]:
    """Restore continuity per generator advancement; lifecycle owner revokes once.

    Use 3A's non-revoking resume API. Never reset a token across tasks/yields.
    No admission happens here; the logical-call owner revokes at final close.
    """
    if call is not None and check:
        call.check()
    token = _budget_call.set(call)
    try:
        if call is None:
            yield
        else:
            with _resume_physical_admission(call.scope, check=check):
                yield
    finally:
        _budget_call.reset(token)


def _selected_budget_request(
    deployment: Deployment,
    request: ModelRequest,
    pricing: Mapping[str, DeploymentPricing],
) -> tuple[Deployment, ModelRequest, _PhysicalSelection | None]:
    call = _budget_call.get()
    if call is None:
        return deployment, request, None
    call.check()
    override = pricing.get(deployment.id)
    final = None
    if override is not None:
        final = KnownPricing(override.input_per_million_usd, override.output_per_million_usd)
    try:
        return call.admission.select(deployment, request, final)
    except (ValueError, TypeError):
        raise _PhysicalAdmissionStopped() from None


def _budget_deadline(deadline: float) -> float:
    call = _budget_call.get()
    return deadline if call is None else min(deadline, call.deadline)


async def _selection_stream[T](
    events: AsyncIterator[T],
    selection: _PhysicalSelection | None,
) -> AsyncGenerator[T, None]:
    try:
        while True:
            try:
                if selection is None:
                    event = await anext(events)
                else:
                    with _bind_physical_selection(selection):
                        event = await anext(events)
            except StopAsyncIteration:
                return
            yield event
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            await close()
