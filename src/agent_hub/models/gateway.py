"""The sole leased, redacted path from model requests to model transports."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import uuid4

if TYPE_CHECKING:
    from agent_hub.harness.provider import NormalizedProviderEvent
    from agent_hub.harness.streaming import OpenAICompatibleChunkTransport
from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityLease,
    CapacityPool,
    CapacityQueueFull,
    CapacityUnavailable,
    CapacityWaitTimeout,
    _CapacityScopeView,
)
from agent_hub.models.failure_receipt import (
    MAX_GATEWAY_FAILURE_ATTEMPTS,
    GatewayFailureAttempt,
    GatewayFailureReceipt,
    _attach_gateway_failure_receipt,
    _valid_usage,
)
from agent_hub.models.litellm_client import (
    ModelResponseCancelled,
    ModelResponseError,
    ModelTransportError,
    safe_model_client_error,
)
from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import (
    Deployment,
    ModelRequest,
    ModelResponse,
    RejectedOutputEvidence,
    TokenUsage,
    _require_safe_identifier,
)

_LOGGER = logging.getLogger(__name__)
_SCOPE_DIAGNOSTIC_ISSUER = object()
_SCOPE_DIAGNOSTIC_ATTRIBUTE = "_gateway_scope_diagnostic"


class ScopeIncompletePhase(StrEnum):
    PRETRANSPORT_CAPACITY = "pretransport_capacity"
    PRETRANSPORT_CREDENTIALS = "pretransport_credentials"
    OUTER_DEADLINE = "outer_deadline"
    CANCELLATION = "cancellation"
    CLEANUP = "cleanup"
    RECORDER = "recorder"
    TRANSPORT = "transport"
    SCOPE_TRACKER = "scope_tracker"
    UNKNOWN_ADAPTER = "unknown_adapter"


class ScopeIncompleteReason(StrEnum):
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    CAPACITY_BACKEND_FAILURE = "capacity_backend_failure"
    CREDENTIAL_RESOLUTION_FAILED = "credential_resolution_failed"
    DEADLINE_EXHAUSTED = "deadline_exhausted"
    CANCELLED = "cancelled"
    RELEASE_FAILED = "release_failed"
    OUTCOME_RECORDING_FAILED = "outcome_recording_failed"
    MISSING_STATUS = "missing_status"
    INVALID_USAGE = "invalid_usage"
    UNKNOWN_FAILURE = "unknown_failure"
    REJECTED_OUTPUT = "rejected_output"
    ATTEMPT_LIMIT = "attempt_limit"
    EVIDENCE_INVALID = "evidence_invalid"
    EVIDENCE_LIMIT = "evidence_limit"
    UNRECORDED_CALL = "unrecorded_call"
    RECEIPT_ISSUANCE_FAILED = "receipt_issuance_failed"


@dataclass(frozen=True, slots=True)
class GatewayScopeDiagnostic:
    """Diagnostic only: never authorizes a receipt or completes a scope."""

    phase: ScopeIncompletePhase
    reason: ScopeIncompleteReason
    transport_entered_count: int
    failure_attempt_count: int
    _issuer: object | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (type(self.phase) is not ScopeIncompletePhase
                or type(self.reason) is not ScopeIncompleteReason
                or type(self.transport_entered_count) is not int
                or type(self.failure_attempt_count) is not int
                or not 0 <= self.failure_attempt_count <= self.transport_entered_count
                or self.failure_attempt_count > MAX_GATEWAY_FAILURE_ATTEMPTS):
            raise ValueError("invalid gateway scope diagnostic")


_gateway_scope_observer: ContextVar[Callable[[GatewayScopeDiagnostic], None] | None] = ContextVar(
    "gateway_scope_observer", default=None,
)


def get_gateway_scope_diagnostic(value: object) -> GatewayScopeDiagnostic | None:
    """Accept immutable issuer metadata only, without adapter attribute protocols."""
    try:
        if type(value) is GatewayCompletion:
            diagnostic = value.scope_diagnostic
        elif isinstance(value, BaseException):
            namespace = object.__getattribute__(value, "__dict__")
            binding = namespace.get(_SCOPE_DIAGNOSTIC_ATTRIBUTE) if type(namespace) is dict else None
            if (type(binding) is not tuple or len(binding) != 3
                    or binding[0] is not _SCOPE_DIAGNOSTIC_ISSUER or binding[1] is not value):
                return None
            diagnostic = binding[2]
        else:
            return None
        if (type(diagnostic) is not GatewayScopeDiagnostic
                or diagnostic._issuer is not _SCOPE_DIAGNOSTIC_ISSUER):
            return None
        diagnostic.__post_init__()
        return diagnostic
    except BaseException:  # noqa: BLE001 - optional diagnostics cannot expose adapter details.
        return None


class ModelGatewayError(RuntimeError):
    """Stable, redacted failure at the model gateway boundary."""


class GatewayRejectedOutput(ModelGatewayError):
    """Rejected model output with selected provenance, not a completion."""

    def __init__(
        self, *, evidence: RejectedOutputEvidence | None,
        deployment_id: str, logical_model: str, provider_id: str, provider_model: str,
        cost_usd: Decimal | None = None, fallback_used: bool = False,
        fallback_from_logical_model: str | None = None, fallback_reason: str | None = None,
        attempted_logical_models: tuple[str, ...] = (),
    ) -> None:
        super().__init__("model response rejected")
        self.evidence = evidence
        self.deployment_id = deployment_id
        self.logical_model = logical_model
        self.provider_id = provider_id
        self.provider_model = provider_model
        self.cost_usd = cost_usd
        self.fallback_used = fallback_used
        self.fallback_from_logical_model = fallback_from_logical_model
        self.fallback_reason = fallback_reason
        self.attempted_logical_models = attempted_logical_models


@dataclass(frozen=True, slots=True)
class DeploymentPricing:
    """Gateway-owned token pricing in USD per one million tokens."""

    input_per_million_usd: Decimal
    output_per_million_usd: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("input_per_million_usd", self.input_per_million_usd),
            ("output_per_million_usd", self.output_per_million_usd),
        ):
            if type(value) is not Decimal:
                raise TypeError(f"{name} must be a Decimal")
            exponent = value.as_tuple().exponent
            if (
                not value.is_finite()
                or value < 0
                or (isinstance(exponent, int) and exponent < -6)
                or value > Decimal(1000000)
            ):
                raise ValueError(f"{name} must be a bounded USD decimal")


@dataclass(frozen=True, slots=True)
class GatewayCompletion:
    """A response plus the gateway-trusted deployment that produced it."""

    response: ModelResponse = field(repr=False)
    deployment_id: str
    logical_model: str
    provider_id: str
    provider_model: str = field(repr=False)
    cost_usd: Decimal | None = None
    fallback_used: bool = False
    fallback_from_logical_model: str | None = None
    fallback_reason: str | None = None
    attempted_logical_models: tuple[str, ...] = ()
    scope_diagnostic: GatewayScopeDiagnostic | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.response, ModelResponse):
            raise TypeError("response must be ModelResponse")
        _require_safe_identifier("deployment id", self.deployment_id)
        _require_safe_identifier("logical model", self.logical_model)
        _require_safe_identifier("provider id", self.provider_id)
        if self.cost_usd is not None and type(self.cost_usd) is not Decimal:
            raise ValueError("gateway cost must be a bounded USD decimal")
        if type(self.fallback_used) is not bool:
            raise ValueError("fallback_used must be a boolean")
        if self.fallback_from_logical_model is not None:
            _require_safe_identifier(
                "fallback source logical model", self.fallback_from_logical_model
            )
        if self.fallback_reason is not None:
            _require_safe_identifier("fallback reason", self.fallback_reason)
        for logical_model in self.attempted_logical_models:
            _require_safe_identifier("attempted logical model", logical_model)
        if not self.fallback_used and (
            self.fallback_from_logical_model is not None or self.fallback_reason is not None
        ):
            raise ValueError("fallback metadata requires fallback_used")
        if self.fallback_used and (
            self.fallback_from_logical_model is None or self.fallback_reason is None
        ):
            raise ValueError("fallback metadata is required when fallback_used is true")
        cost_exponent = None if self.cost_usd is None else self.cost_usd.as_tuple().exponent
        if (
            not self.provider_model
            or self.provider_model != self.provider_model.strip()
            or len(self.provider_model) > 512
        ):
            raise ValueError("provider_model must be bounded and unpadded")
        if self.provider_model.split("/", 1)[0] != self.provider_id:
            raise ValueError("provider provenance is inconsistent")
        if self.cost_usd is not None and (
            not self.cost_usd.is_finite()
            or self.cost_usd < 0
            or (isinstance(cost_exponent, int) and cost_exponent < -6)
            or self.cost_usd > Decimal(1000000)
        ):
            raise ValueError("gateway cost must be a bounded USD decimal")


@dataclass(frozen=True, slots=True)
class _SafeTransportFailure:
    error: ModelTransportError | ModelGatewayError | ModelResponseCancelled


@dataclass(slots=True)
class _GatewayFailureHistory:
    call_id: str = field(default_factory=lambda: str(uuid4()))
    attempted_logical_models: list[str] = field(default_factory=list)
    attempts: list[GatewayFailureAttempt] = field(default_factory=list)
    entered_count: int = 0
    history_complete: bool = True
    first_incomplete: tuple[ScopeIncompletePhase, ScopeIncompleteReason] | None = None

    def mark_incomplete(self, phase: ScopeIncompletePhase, reason: ScopeIncompleteReason) -> None:
        self.history_complete = False
        if self.first_incomplete is None:
            self.first_incomplete = (phase, reason)

    def diagnostic(self) -> GatewayScopeDiagnostic | None:
        if self.first_incomplete is None:
            return None
        diagnostic = GatewayScopeDiagnostic(
            *self.first_incomplete, self.entered_count, len(self.attempts),
        )
        object.__setattr__(diagnostic, "_issuer", _SCOPE_DIAGNOSTIC_ISSUER)
        return diagnostic

    def attach_diagnostic(self, error: BaseException) -> None:
        try:
            diagnostic = self.diagnostic()
            namespace = object.__getattribute__(error, "__dict__")
            if diagnostic is not None and type(namespace) is dict:
                namespace[_SCOPE_DIAGNOSTIC_ATTRIBUTE] = (
                    _SCOPE_DIAGNOSTIC_ISSUER, error, diagnostic,
                )
        except BaseException:  # noqa: BLE001 - preserve the original failure/cancellation.
            return

    def attach(self, error: BaseException, request: ModelRequest) -> None:
        if not self.attempts:
            return
        models = tuple(dict.fromkeys(self.attempted_logical_models))
        complete = (
            self.history_complete
            and self.entered_count == len(self.attempts)
            and len(models) <= MAX_GATEWAY_FAILURE_ATTEMPTS
        )
        try:
            receipt = GatewayFailureReceipt(
                call_id=self.call_id,
                requested_logical_model=request.logical_model,
                allow_fallback=request.allow_fallback,
                history_complete=complete,
                attempted_logical_models=models[:MAX_GATEWAY_FAILURE_ATTEMPTS],
                attempts=tuple(self.attempts),
            )
            _attach_gateway_failure_receipt(error, receipt)
        except Exception:  # noqa: BLE001 - optional evidence must not alter a primary error.
            self.mark_incomplete(ScopeIncompletePhase.RECORDER,
                                 ScopeIncompleteReason.RECEIPT_ISSUANCE_FAILED)


@dataclass(slots=True)
class _GatewayFailureTracking:
    history: _GatewayFailureHistory
    deployment: Deployment
    ordinal: int = 0
    recorded: bool = False

    def enter(self) -> None:
        self.history.entered_count += 1
        self.ordinal = self.history.entered_count
        if self.ordinal > MAX_GATEWAY_FAILURE_ATTEMPTS:
            self.history.mark_incomplete(ScopeIncompletePhase.RECORDER,
                                         ScopeIncompleteReason.ATTEMPT_LIMIT)

    def record(
        self, outcome: Literal["empty_response", "transport_error"],
        status_code: int | None, usage: TokenUsage | None = None,
    ) -> None:
        if self.ordinal == 0 or self.recorded or self.ordinal > MAX_GATEWAY_FAILURE_ATTEMPTS:
            self.history.mark_incomplete(ScopeIncompletePhase.RECORDER,
                                         ScopeIncompleteReason.EVIDENCE_INVALID)
            return
        self.recorded = True
        usage_status: Literal["known", "missing", "invalid"] = (
            "missing" if usage is None else "known" if _valid_usage(usage) else "invalid"
        )
        if usage_status == "invalid":
            self.history.mark_incomplete(ScopeIncompletePhase.RECORDER,
                                         ScopeIncompleteReason.INVALID_USAGE)
        elif outcome == "transport_error" and status_code is None:
            self.history.mark_incomplete(ScopeIncompletePhase.TRANSPORT,
                                         ScopeIncompleteReason.MISSING_STATUS)
        try:
            self.history.attempts.append(GatewayFailureAttempt(
                ordinal=self.ordinal,
                logical_model=self.deployment.logical_model,
                deployment_id=self.deployment.id,
                provider_id=self.deployment.provider_model.split("/", 1)[0],
                provider_model=self.deployment.provider_model,
                outcome=outcome,
                status_code=status_code,
                usage_status=usage_status,
                usage=None if usage_status != "known" else TokenUsage(
                    cast(TokenUsage, usage).prompt_tokens,
                    cast(TokenUsage, usage).completion_tokens,
                    cast(TokenUsage, usage).total_tokens,
                ),
            ))
        except Exception:  # noqa: BLE001 - invalid supplier metadata must fail closed.
            self.history.mark_incomplete(ScopeIncompletePhase.RECORDER,
                                         ScopeIncompleteReason.EVIDENCE_INVALID)


class GatewayResponseCancelled(asyncio.CancelledError):
    """Received model accounting receipt; cancellation still stops execution."""

    def __init__(self, *, receipt: GatewayCompletion | GatewayRejectedOutput) -> None:
        if not isinstance(receipt, GatewayCompletion | GatewayRejectedOutput):
            raise TypeError("gateway cancellation receipt must be a received model outcome")
        super().__init__("model gateway response cancelled")
        self.receipt = receipt

    def __str__(self) -> str:
        return "model gateway response cancelled"

    def __repr__(self) -> str:
        return "GatewayResponseCancelled('model gateway response cancelled')"


def _received_result(
    outcome: ModelResponse | _SafeTransportFailure | None,
) -> ModelResponse | RejectedOutputEvidence | None:
    if isinstance(outcome, ModelResponse):
        return outcome
    if isinstance(outcome, _SafeTransportFailure):
        if isinstance(outcome.error, ModelResponseCancelled):
            return outcome.error.receipt
        if isinstance(outcome.error, ModelResponseError):
            return outcome.error.evidence
    return None


async def _settle_cleanup(future: asyncio.Future[Any]) -> BaseException | None:
    cancellation: asyncio.CancelledError | None = None
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        except Exception:  # noqa: BLE001 - caller decides cleanup failure precedence
            break
    try:
        future.result()
    except asyncio.CancelledError as error:
        return cancellation or error
    except Exception as error:  # noqa: BLE001 - preserve cleanup failure for the caller
        return cancellation or error
    return cancellation


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _retryable_model_failure(error: BaseException) -> bool:
    if isinstance(error, ModelResponseError | GatewayRejectedOutput):
        return False
    if isinstance(error, ModelTransportError):
        return error.status_code is None or error.status_code in {
            408,
            409,
            425,
            429,
            500,
            502,
            503,
            504,
        }
    if isinstance(error, ModelGatewayError):
        return str(error) in {
            "model transport failed",
            "model response text is empty",
            "model response is empty",
        }
    return False


def _fallback_reason(error: BaseException) -> str:
    if isinstance(error, ModelTransportError):
        if error.status_code == 429:
            return "capacity_pressure"
        return "transport_retryable"
    if isinstance(error, ModelGatewayError) and str(error) in {
        "model response text is empty",
        "model response is empty",
    }:
        return "empty_response"
    return "gateway_retryable"


class SecretResolver(Protocol):
    async def resolve(self, secret_ref: str) -> str: ...


class ModelTransport(Protocol):
    async def complete(
        self, deployment: Deployment, request: ModelRequest, api_key: str
    ) -> ModelResponse: ...


class TokenEstimator(Protocol):
    def estimate(self, request: ModelRequest) -> int: ...


class CapacityController(Protocol):
    async def initialize(self) -> None: ...

    def validate_configuration(self, deployments: Sequence[Deployment]) -> None: ...

    async def acquire(
        self,
        candidates: Sequence[Deployment],
        wait_timeout: float,
        *,
        estimated_tokens: int | Mapping[str, int],
    ) -> CapacityLease: ...

    async def renew(self, lease: CapacityLease) -> CapacityLease | None: ...

    async def release(self, lease: CapacityLease) -> bool: ...

    async def record_outcome(
        self,
        quota_scope_id: str,
        *,
        status_code: int | None,
        latency_seconds: float,
        succeeded: bool,
    ) -> None: ...


class ConservativeTokenEstimator:
    """Deterministic token estimate with explicit text and JSON structure costs."""

    def estimate(self, request: ModelRequest) -> int:
        return self.estimate_input(request) + request.max_output_tokens

    def estimate_input(self, request: ModelRequest) -> int:
        payload: dict[str, object] = {
            "logical_model": request.logical_model,
            "messages": [
                {"role": message.role, "content": self._mutable_json(message.content)}
                for message in request.messages
            ],
            "required_capabilities": sorted(str(item) for item in request.required_capabilities),
            "timeout_seconds": request.timeout_seconds,
            "allow_fallback": request.allow_fallback,
            "max_output_tokens": request.max_output_tokens,
            "response_schema": None,
        }
        if request.response_schema is not None:
            payload["response_schema"] = {
                "name": request.response_schema.name,
                "schema": self._mutable_json(request.response_schema.schema),
            }
        normalized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return max(1, self._estimate_text_tokens(normalized))

    @staticmethod
    def _estimate_text_tokens(value: str) -> int:
        tokens = 0
        ascii_run = 0

        def flush_ascii_run() -> None:
            nonlocal ascii_run, tokens
            if ascii_run:
                tokens += math.ceil(ascii_run / 4)
                ascii_run = 0

        for character in value:
            if character.isascii() and (character.isalnum() or character.isspace()):
                ascii_run += 1
                continue
            flush_ascii_run()
            if character.isascii():
                tokens += 1
            else:
                tokens += max(1, math.ceil(len(character.encode("utf-8")) / 2))
        flush_ascii_run()
        return tokens

    def _mutable_json(self, value: object) -> object:
        if isinstance(value, Mapping):
            return {key: self._mutable_json(item) for key, item in value.items()}
        if isinstance(value, tuple | list):
            return [self._mutable_json(item) for item in value]
        return value


class ModelGateway:
    def __init__(
        self,
        registry: ModelRegistry,
        capacity_pool: CapacityController | CapacityPool,
        secret_resolver: SecretResolver,
        transport: ModelTransport,
        *,
        fallbacks: Mapping[str, str] | None = None,
        capacity_wait_timeout: float = 5,
        heartbeat_interval: float = 10,
        heartbeat_safety_fraction: float = 0.5,
        token_estimator: TokenEstimator | None = None,
        pricing: Mapping[str, DeploymentPricing] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = _utc_now,
    ) -> None:
        for name, value in (
            ("capacity_wait_timeout", capacity_wait_timeout),
            ("heartbeat_interval", heartbeat_interval),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive and finite")
        if (
            isinstance(heartbeat_safety_fraction, bool)
            or not isinstance(heartbeat_safety_fraction, int | float)
            or not math.isfinite(heartbeat_safety_fraction)
            or not 0 < heartbeat_safety_fraction <= 0.5
        ):
            raise ValueError("heartbeat_safety_fraction must be finite and between 0 and 0.5")
        configured_fallbacks = dict(fallbacks or {})
        self._validate_fallbacks(registry, configured_fallbacks)
        configured_pricing = dict(pricing or {})
        deployment_ids = {deployment.id for deployment in registry.deployments}
        for deployment_id, deployment_pricing in configured_pricing.items():
            _require_safe_identifier("pricing deployment id", deployment_id)
            if deployment_id not in deployment_ids:
                raise ValueError(f"unknown pricing deployment {deployment_id!r}")
            if type(deployment_pricing) is not DeploymentPricing:
                raise TypeError("pricing values must be DeploymentPricing")
        capacity_pool.validate_configuration(registry.deployments)
        self._registry = registry
        self._capacity = capacity_pool
        self._capacity_initializers: set[asyncio.Task[None]] = set()
        self._secret_resolver = secret_resolver
        self._transport = transport
        self._fallbacks = MappingProxyType(configured_fallbacks)
        self._pricing = MappingProxyType(configured_pricing)
        self._capacity_wait_timeout = float(capacity_wait_timeout)
        self._heartbeat_interval = float(heartbeat_interval)
        del heartbeat_safety_fraction, utc_now
        self._token_estimator = token_estimator or ConservativeTokenEstimator()
        self._monotonic = monotonic

    @staticmethod
    def _validate_fallbacks(registry: ModelRegistry, fallbacks: Mapping[str, str]) -> None:
        for source, target in fallbacks.items():
            _require_safe_identifier("fallback source", source)
            _require_safe_identifier("fallback target", target)
            if source not in registry.logical_models:
                raise ValueError(f"unknown fallback source model {source!r}")
            if target not in registry.logical_models:
                raise ValueError(f"unknown fallback model {target!r}")
            if source == target:
                raise ValueError("fallback model must not reference itself")
        for origin in fallbacks:
            seen: set[str] = set()
            current = origin
            while current in fallbacks:
                if current in seen:
                    raise ValueError("fallback model cycle")
                seen.add(current)
                current = fallbacks[current]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return (await self.complete_with_context(request)).response

    async def _record_capacity_outcome(
        self,
        capacity: CapacityController | CapacityPool,
        lease: CapacityLease,
        *,
        deadline: float,
        status_code: int | None,
        latency_seconds: float,
        succeeded: bool,
    ) -> None:
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds <= 0:
            raise TimeoutError
        await asyncio.wait_for(
            capacity.record_outcome(
                lease.quota_scope_id,
                status_code=status_code,
                latency_seconds=latency_seconds,
                succeeded=succeeded,
            ),
            timeout=min(self._capacity_wait_timeout, remaining_seconds),
        )

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        failure_history = _GatewayFailureHistory()
        # Consume the call-local observer before spawning transport/cleanup tasks.
        observer = _gateway_scope_observer.get()
        _gateway_scope_observer.set(None)
        try:
            completion = await self._complete_with_failure_history(request, failure_history)
            diagnostic = failure_history.diagnostic()
            return (completion if diagnostic is None
                    else replace(completion, scope_diagnostic=diagnostic))
        except BaseException as error:
            if isinstance(error, GatewayRejectedOutput):
                failure_history.mark_incomplete(ScopeIncompletePhase.TRANSPORT,
                                                ScopeIncompleteReason.REJECTED_OUTPUT)
            elif isinstance(error, asyncio.CancelledError):
                failure_history.mark_incomplete(ScopeIncompletePhase.CANCELLATION,
                                                ScopeIncompleteReason.CANCELLED)
            elif isinstance(error, (CapacityBackendError, CapacityConfigurationError)):
                failure_history.mark_incomplete(ScopeIncompletePhase.PRETRANSPORT_CAPACITY,
                                                ScopeIncompleteReason.CAPACITY_BACKEND_FAILURE)
            elif not failure_history.attempts or not failure_history.history_complete:
                failure_history.mark_incomplete(ScopeIncompletePhase.UNKNOWN_ADAPTER,
                                                ScopeIncompleteReason.UNKNOWN_FAILURE)
            if isinstance(error, (ModelTransportError, ModelGatewayError)):
                failure_history.attach(error, request)
            failure_history.attach_diagnostic(error)
            raise
        finally:
            if observer is not None:
                try:
                    diagnostic = failure_history.diagnostic()
                    if diagnostic is not None:
                        observer(diagnostic)
                except BaseException:  # noqa: BLE001 - diagnostics never replace model outcomes.
                    _LOGGER.warning("model_scope_observer_failed")

    async def _complete_with_failure_history(
        self, request: ModelRequest, failure_history: _GatewayFailureHistory,
    ) -> GatewayCompletion:
        request_deadline = asyncio.get_running_loop().time() + request.timeout_seconds
        (
            candidate_groups,
            attempted_logical_models,
            fallback_from_logical_model,
            fallback_reason,
        ) = self._capable_candidate_groups(request)
        failure_history.attempted_logical_models = attempted_logical_models
        relevant_deployments = tuple(
            deployment
            for _logical_model, candidates in candidate_groups
            for deployment in candidates
        )
        scoped = getattr(self._capacity, "scoped", None)
        capacity = self._capacity if scoped is None else scoped(relevant_deployments)
        input_tokens = self._estimated_input_tokens(request, relevant_deployments)
        last_retryable_error: BaseException | None = None
        for group_index, (logical_model, candidates) in enumerate(candidate_groups):
            remaining_seconds = request_deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                failure_history.mark_incomplete(ScopeIncompletePhase.OUTER_DEADLINE,
                                                ScopeIncompleteReason.DEADLINE_EXHAUSTED)
                break
            attempted_logical_models.append(logical_model)
            compatible_candidates = self._context_compatible_candidates(
                candidates, input_tokens
            )
            if not compatible_candidates:
                if fallback_from_logical_model is None:
                    fallback_from_logical_model = logical_model
                    fallback_reason = "context_window_exceeded"
                continue
            estimated_tokens = self._capacity_token_estimates(
                request, compatible_candidates, input_tokens
            )
            try:
                lease = await self._acquire_capacity(
                    capacity, compatible_candidates, estimated_tokens,
                    deadline=request_deadline,
                    wait_timeout=min(self._capacity_wait_timeout, request.timeout_seconds),
                    has_fallback=any(
                        self._context_compatible_candidates(items, input_tokens)
                        for _, items in candidate_groups[group_index + 1:]
                    ),
                )
            except (TimeoutError, CapacityWaitTimeout, CapacityQueueFull):
                failure_history.mark_incomplete(ScopeIncompletePhase.PRETRANSPORT_CAPACITY,
                                                ScopeIncompleteReason.CAPACITY_UNAVAILABLE)
                if fallback_from_logical_model is None:
                    fallback_from_logical_model = logical_model
                    fallback_reason = "capacity_unavailable"
                continue
            selected = next(
                (
                    item
                    for item in compatible_candidates
                    if item.id == lease.deployment_id
                ),
                None,
            )
            if selected is None or selected.quota_scope_id != lease.quota_scope_id:
                failure_history.mark_incomplete(ScopeIncompletePhase.PRETRANSPORT_CAPACITY,
                                                ScopeIncompleteReason.CAPACITY_BACKEND_FAILURE)
                cleanup_error = await self._release_cleanup(
                    capacity, lease, deadline=request_deadline
                )
                if isinstance(cleanup_error, asyncio.CancelledError):
                    raise cleanup_error
                if cleanup_error is not None:
                    raise CapacityBackendError("model capacity release failed") from None
                raise CapacityBackendError("model capacity returned an unknown deployment")
            remaining_seconds = request_deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                failure_history.mark_incomplete(ScopeIncompletePhase.OUTER_DEADLINE,
                                                ScopeIncompleteReason.DEADLINE_EXHAUSTED)
                cleanup_error = await self._release_cleanup(
                    capacity, lease, deadline=request_deadline
                )
                if cleanup_error is not None:
                    raise CapacityBackendError("model capacity release failed") from None
                break
            selected_request = self._request_for_deployment(request, selected, input_tokens)
            try:
                response = await self._complete_leased(
                    capacity, selected, lease, selected_request, deadline=request_deadline,
                    failure_tracking=_GatewayFailureTracking(failure_history, selected),
                )
            except ModelResponseCancelled as error:
                cancelled = self._cancelled_output(
                    selected, selected_request, error.receipt, fallback_from_logical_model,
                    fallback_reason, attempted_logical_models,
                )
                cancelled.args = error.args
                raise cancelled from None
            except ModelResponseError as error:
                raise self._rejected_output(
                    selected, selected_request, error.evidence, fallback_from_logical_model,
                    fallback_reason, attempted_logical_models,
                ) from None
            except (ModelTransportError, ModelGatewayError) as error:
                if not _retryable_model_failure(error):
                    raise
                last_retryable_error = error
                if fallback_from_logical_model is None:
                    fallback_from_logical_model = logical_model
                    fallback_reason = _fallback_reason(error)
                continue
            return GatewayCompletion(
                response=response,
                deployment_id=selected.id,
                logical_model=selected.logical_model,
                provider_id=selected.provider_model.split("/", 1)[0],
                provider_model=selected.provider_model,
                cost_usd=self._cost_usd(selected, response),
                fallback_used=selected.logical_model != request.logical_model,
                fallback_from_logical_model=(
                    fallback_from_logical_model
                    if selected.logical_model != request.logical_model
                    else None
                ),
                fallback_reason=(
                    fallback_reason if selected.logical_model != request.logical_model else None
                ),
                attempted_logical_models=tuple(dict.fromkeys(attempted_logical_models)),
            )
        if last_retryable_error is not None:
            raise last_retryable_error from None
        raise CapacityUnavailable("model capacity unavailable") from None

    async def stream_openai_compatible_events(
        self, request: ModelRequest
    ) -> AsyncIterator[NormalizedProviderEvent]:
        request_deadline = asyncio.get_running_loop().time() + request.timeout_seconds
        streaming_transport = self._streaming_transport()
        last_retryable_error: BaseException | None = None
        (
            candidate_groups,
            attempted_logical_models,
            fallback_from_logical_model,
            fallback_reason,
        ) = self._capable_candidate_groups(request)
        relevant_deployments = tuple(
            deployment
            for _logical_model, candidates in candidate_groups
            for deployment in candidates
        )
        scoped = getattr(self._capacity, "scoped", None)
        capacity = self._capacity if scoped is None else scoped(relevant_deployments)
        input_tokens = self._estimated_input_tokens(request, relevant_deployments)
        for group_index, (logical_model, candidates) in enumerate(candidate_groups):
            remaining_seconds = request_deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                break
            attempted_logical_models.append(logical_model)
            compatible_candidates = self._context_compatible_candidates(
                candidates, input_tokens
            )
            if not compatible_candidates:
                if fallback_from_logical_model is None:
                    fallback_from_logical_model = logical_model
                    fallback_reason = "context_window_exceeded"
                continue
            estimated_tokens = self._capacity_token_estimates(
                request, compatible_candidates, input_tokens
            )
            if (
                fallback_from_logical_model is not None
                and fallback_reason is not None
                and logical_model != request.logical_model
            ):
                yield self._stream_fallback_event(
                    from_logical_model=fallback_from_logical_model,
                    to_logical_model=logical_model,
                    reason=fallback_reason,
                    attempted_logical_models=attempted_logical_models,
                )
            remaining_seconds = request_deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                break
            try:
                lease = await self._acquire_capacity(
                    capacity, compatible_candidates, estimated_tokens,
                    deadline=request_deadline,
                    wait_timeout=min(self._capacity_wait_timeout, request.timeout_seconds),
                    has_fallback=any(
                        self._context_compatible_candidates(items, input_tokens)
                        for _, items in candidate_groups[group_index + 1:]
                    ),
                )
            except (TimeoutError, CapacityWaitTimeout, CapacityQueueFull):
                fallback_from_logical_model = logical_model
                fallback_reason = "capacity_unavailable"
                continue
            selected = next(
                (
                    item
                    for item in compatible_candidates
                    if item.id == lease.deployment_id
                ),
                None,
            )
            if selected is None or selected.quota_scope_id != lease.quota_scope_id:
                cleanup_error = await self._release_cleanup(
                    capacity, lease, deadline=request_deadline
                )
                if isinstance(cleanup_error, asyncio.CancelledError):
                    raise cleanup_error
                if cleanup_error is not None:
                    raise CapacityBackendError("model capacity release failed") from None
                raise CapacityBackendError("model capacity returned an unknown deployment")
            remaining_seconds = request_deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                cleanup_error = await self._release_cleanup(
                    capacity, lease, deadline=request_deadline
                )
                if cleanup_error is not None:
                    raise CapacityBackendError("model capacity release failed") from None
                break
            selected_request = self._request_for_deployment(request, selected, input_tokens)
            yielded = False
            events = self._stream_openai_compatible_leased(
                capacity,
                selected,
                lease,
                selected_request,
                streaming_transport,
                deadline=request_deadline,
            )
            try:
                while True:
                    try:
                        event = await anext(events)
                    except StopAsyncIteration:
                        break
                    yielded = True
                    yield event
                if not yielded:
                    last_retryable_error = ModelGatewayError("model response is empty")
                    fallback_from_logical_model = logical_model
                    fallback_reason = _fallback_reason(last_retryable_error)
                    continue
            except ModelResponseError as error:
                raise self._rejected_output(
                    selected, selected_request, error.evidence, fallback_from_logical_model,
                    fallback_reason, attempted_logical_models,
                ) from None
            except (ModelTransportError, ModelGatewayError) as error:
                if yielded or not _retryable_model_failure(error):
                    raise
                last_retryable_error = error
                fallback_from_logical_model = logical_model
                fallback_reason = _fallback_reason(error)
                continue
            finally:
                await self._stream_close_cleanup(events, deadline=request_deadline)
            return
        if last_retryable_error is not None:
            raise last_retryable_error from None
        raise CapacityUnavailable("model capacity unavailable") from None

    async def _acquire_capacity(
        self,
        capacity: CapacityController | CapacityPool,
        candidates: Sequence[Deployment],
        estimated_tokens: int | Mapping[str, int],
        *,
        deadline: float,
        wait_timeout: float,
        has_fallback: bool,
    ) -> CapacityLease:
        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CapacityWaitTimeout("model capacity queue timeout")
            await self._initialize_capacity(capacity, timeout=min(wait_timeout, remaining))
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CapacityWaitTimeout("model capacity queue timeout")
            window = min(wait_timeout, remaining)
            started = loop.time()
            try:
                # Native pools bound every RPC themselves; an outer timer would
                # race their last window and misclassify an unknown Redis outcome.
                if isinstance(capacity, CapacityPool | _CapacityScopeView):
                    return await capacity.acquire(
                        candidates, window, estimated_tokens=estimated_tokens,
                    )
                return await asyncio.wait_for(
                    capacity.acquire(candidates, window, estimated_tokens=estimated_tokens),
                    timeout=remaining,
                )
            except CapacityWaitTimeout:
                if has_fallback or loop.time() >= deadline:
                    raise
                # Custom controllers may report congestion before the window elapses.
                await asyncio.sleep(max(0, min(started + window, deadline) - loop.time()))

    async def _initialize_capacity(
        self, capacity: CapacityController | CapacityPool, *, timeout: float,
    ) -> None:
        task = asyncio.create_task(capacity.initialize())
        self._capacity_initializers.add(task)

        def finished(initializer: asyncio.Task[None]) -> None:
            self._capacity_initializers.discard(initializer)
            if not initializer.cancelled():
                initializer.exception()

        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if task in done:
                task.result()
                return
            if isinstance(capacity, CapacityPool | _CapacityScopeView):
                raise CapacityBackendError("model capacity initialization timed out")
            raise TimeoutError
        finally:
            if not task.done():
                # Native registration finishes its bounded owner-specific rollback
                # in the background; it must not extend the caller's deadline.
                task.cancel()

    def _estimate_tokens(self, request: ModelRequest) -> int:
        estimated_tokens = self._token_estimator.estimate(request)
        if type(estimated_tokens) is not int or estimated_tokens <= 0:
            raise ValueError("token estimator must return a strict positive integer")
        return estimated_tokens

    def _estimated_input_tokens(
        self, request: ModelRequest, deployments: Sequence[Deployment]
    ) -> int | None:
        if not any(item.context_window_tokens is not None for item in deployments):
            return None
        estimate_input = getattr(self._token_estimator, "estimate_input", None)
        if callable(estimate_input):
            estimated_input = estimate_input(request)
            if type(estimated_input) is not int or estimated_input <= 0:
                raise ValueError("input token estimator must return a strict positive integer")
            return estimated_input
        minimal_output_request = replace(request, max_output_tokens=1)
        return max(0, self._estimate_tokens(minimal_output_request) - 1)

    @staticmethod
    def _context_compatible_candidates(
        candidates: Sequence[Deployment], input_tokens: int | None
    ) -> tuple[Deployment, ...]:
        if input_tokens is None:
            return tuple(candidates)
        return tuple(
            deployment
            for deployment in candidates
            if deployment.context_window_tokens is None
            or input_tokens < deployment.context_window_tokens
        )

    @staticmethod
    def _request_for_deployment(
        request: ModelRequest,
        deployment: Deployment,
        input_tokens: int | None,
    ) -> ModelRequest:
        output_limit = request.max_output_tokens
        if deployment.max_output_tokens is not None:
            output_limit = min(output_limit, deployment.max_output_tokens)
        if deployment.context_window_tokens is not None and input_tokens is not None:
            output_limit = min(
                output_limit,
                max(1, deployment.context_window_tokens - input_tokens),
            )
        if output_limit == request.max_output_tokens:
            return request
        return replace(request, max_output_tokens=output_limit)

    def _capacity_token_estimates(
        self,
        request: ModelRequest,
        candidates: Sequence[Deployment],
        input_tokens: int | None,
    ) -> int | Mapping[str, int]:
        estimates = {
            deployment.id: self._estimate_tokens(
                self._request_for_deployment(request, deployment, input_tokens)
            )
            for deployment in candidates
        }
        unique_estimates = set(estimates.values())
        if len(unique_estimates) == 1:
            return next(iter(unique_estimates))
        return MappingProxyType(estimates)

    def _capable_candidate_groups(
        self, request: ModelRequest
    ) -> tuple[
        list[tuple[str, tuple[Deployment, ...]]],
        list[str],
        str | None,
        str | None,
    ]:
        candidate_groups: list[tuple[str, tuple[Deployment, ...]]] = []
        skipped_before_first_capable: list[str] = []
        first_capability_error: NoCapableDeployment | None = None
        for logical_model in self._fallback_chain(request.logical_model, request.allow_fallback):
            try:
                candidates = self._registry.candidates(
                    logical_model, request.required_capabilities
                )
            except NoCapableDeployment as error:
                if first_capability_error is None:
                    first_capability_error = error
                if not candidate_groups:
                    skipped_before_first_capable.append(logical_model)
                continue
            candidate_groups.append((logical_model, candidates))
        if not candidate_groups:
            if first_capability_error is not None:
                raise first_capability_error
            raise CapacityUnavailable("model capacity unavailable")
        if skipped_before_first_capable:
            return (
                candidate_groups,
                skipped_before_first_capable,
                skipped_before_first_capable[0],
                "capability_unavailable",
            )
        return candidate_groups, [], None, None

    def _stream_fallback_event(
        self,
        *,
        from_logical_model: str,
        to_logical_model: str,
        reason: str,
        attempted_logical_models: Sequence[str],
    ) -> NormalizedProviderEvent:
        from agent_hub.harness.provider import NormalizedProviderEvent

        return NormalizedProviderEvent(
            kind="model.fallback",
            payload={
                "schema_version": 1,
                "phase": "attempted",
                "from_logical_model": from_logical_model,
                "to_logical_model": to_logical_model,
                "reason": reason,
                "attempted_logical_models": tuple(dict.fromkeys(attempted_logical_models)),
            },
        )

    def _cost_usd(self, deployment: Deployment, response: ModelResponse) -> Decimal | None:
        pricing = self._pricing.get(deployment.id)
        if (
            pricing is None
            and deployment.input_per_million_usd is not None
            and deployment.output_per_million_usd is not None
        ):
            pricing = DeploymentPricing(
                input_per_million_usd=deployment.input_per_million_usd,
                output_per_million_usd=deployment.output_per_million_usd,
            )
        usage = response.usage
        if usage is None:
            return None
        if pricing is None:
            return Decimal(0)
        cost = (
            Decimal(usage.prompt_tokens) * pricing.input_per_million_usd
            + Decimal(usage.completion_tokens) * pricing.output_per_million_usd
        ) / Decimal(1000000)
        return cost.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)

    def _rejected_output(
        self, selected: Deployment, request: ModelRequest,
        evidence: RejectedOutputEvidence | None, fallback_from_logical_model: str | None,
        fallback_reason: str | None, attempted_logical_models: Sequence[str],
    ) -> GatewayRejectedOutput:
        fallback_used = selected.logical_model != request.logical_model
        return GatewayRejectedOutput(
            evidence=evidence,
            deployment_id=selected.id,
            logical_model=selected.logical_model,
            provider_id=selected.provider_model.split("/", 1)[0],
            provider_model=selected.provider_model,
            cost_usd=self._receipt_cost_usd(selected, None if evidence is None else evidence.usage),
            fallback_used=fallback_used,
            fallback_from_logical_model=fallback_from_logical_model if fallback_used else None,
            fallback_reason=fallback_reason if fallback_used else None,
            attempted_logical_models=tuple(dict.fromkeys(attempted_logical_models)),
        )

    def _cancelled_output(
        self, selected: Deployment, request: ModelRequest,
        receipt: ModelResponse | RejectedOutputEvidence, fallback_from_logical_model: str | None,
        fallback_reason: str | None, attempted_logical_models: Sequence[str],
    ) -> GatewayResponseCancelled:
        if isinstance(receipt, RejectedOutputEvidence):
            return GatewayResponseCancelled(receipt=self._rejected_output(
                selected, request, receipt, fallback_from_logical_model,
                fallback_reason, attempted_logical_models,
            ))
        fallback_used = selected.logical_model != request.logical_model
        return GatewayResponseCancelled(receipt=GatewayCompletion(
            response=receipt,
            deployment_id=selected.id,
            logical_model=selected.logical_model,
            provider_id=selected.provider_model.split("/", 1)[0],
            provider_model=selected.provider_model,
            cost_usd=self._receipt_cost_usd(selected, receipt.usage),
            fallback_used=fallback_used,
            fallback_from_logical_model=fallback_from_logical_model if fallback_used else None,
            fallback_reason=fallback_reason if fallback_used else None,
            attempted_logical_models=tuple(dict.fromkeys(attempted_logical_models)),
        ))

    def _receipt_cost_usd(
        self, deployment: Deployment, usage: TokenUsage | None,
    ) -> Decimal | None:
        if usage is None:
            return None
        pricing = self._pricing.get(deployment.id)
        if (
            pricing is None
            and deployment.input_per_million_usd is not None
            and deployment.output_per_million_usd is not None
        ):
            pricing = DeploymentPricing(
                deployment.input_per_million_usd, deployment.output_per_million_usd,
            )
        if pricing is None:
            return None
        cost = (
            Decimal(usage.prompt_tokens) * pricing.input_per_million_usd
            + Decimal(usage.completion_tokens) * pricing.output_per_million_usd
        ) / Decimal(1000000)
        return cost.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)

    def _fallback_chain(self, primary: str, allow_fallback: bool) -> tuple[str, ...]:
        chain = [primary]
        if allow_fallback:
            current = primary
            while current in self._fallbacks:
                current = self._fallbacks[current]
                chain.append(current)
        return tuple(chain)

    def _streaming_transport(self) -> OpenAICompatibleChunkTransport:
        stream_chunks = getattr(self._transport, "stream_openai_compatible_chunks", None)
        if not callable(stream_chunks):
            raise ModelGatewayError("model transport streaming unavailable")
        return cast("OpenAICompatibleChunkTransport", self._transport)

    async def _stream_openai_compatible_leased(
        self,
        capacity: CapacityController | CapacityPool,
        deployment: Deployment,
        lease: CapacityLease,
        request: ModelRequest,
        transport: OpenAICompatibleChunkTransport,
        *,
        deadline: float,
    ) -> AsyncIterator[NormalizedProviderEvent]:
        primary_error: BaseException | None = None
        transport_started: float | None = None
        should_record = False
        status_code: int | None = None
        succeeded = False
        try:
            try:
                remaining_seconds = deadline - asyncio.get_running_loop().time()
                if remaining_seconds <= 0:
                    raise TimeoutError
                api_key = await asyncio.wait_for(
                    self._secret_resolver.resolve(deployment.secret_ref),
                    timeout=remaining_seconds,
                )
            except TimeoutError:
                primary_error = ModelTransportError(
                    "model request deadline exhausted", status_code=408
                )
            except asyncio.CancelledError as error:
                primary_error = error
            except Exception:  # noqa: BLE001 - redact resolver details at the boundary
                primary_error = ModelGatewayError("model credential resolution failed")
            else:
                transport_started = self._monotonic()
                should_record = True
                stream_primary_error: BaseException | None = None
                events = self._iterate_stream_with_heartbeat(
                    capacity,
                    deployment,
                    lease,
                    request,
                    transport,
                    api_key,
                    deadline=deadline,
                )
                try:
                    yielded = False
                    while True:
                        try:
                            event = await self._next_stream_event_before_deadline(
                                events, deadline=deadline
                            )
                        except StopAsyncIteration:
                            break
                        yielded = True
                        yield event
                    status_code = 200
                    if yielded:
                        succeeded = True
                    else:
                        primary_error = ModelGatewayError("model response is empty")
                except TimeoutError:
                    stream_primary_error = ModelTransportError(
                        "model request deadline exhausted", status_code=408
                    )
                    primary_error = stream_primary_error
                except GeneratorExit as error:
                    # Caller-controlled closure is not evidence of provider overload.
                    should_record = False
                    stream_primary_error = error
                    primary_error = error
                    raise
                except asyncio.CancelledError as error:
                    should_record = False
                    stream_primary_error = error
                    primary_error = error
                except ModelResponseError as error:
                    stream_primary_error = error
                    status_code = error.status_code
                    primary_error = ModelResponseError(
                        "model response rejected", status_code=error.status_code,
                        evidence=error.evidence,
                    )
                    error.__traceback__ = None
                    error.__context__ = None
                    error.__cause__ = None
                except ModelTransportError as error:
                    stream_primary_error = error
                    status_code = error.status_code
                    primary_error = ModelTransportError(
                        "model transport failed",
                        status_code=error.status_code,
                    )
                    error.__traceback__ = None
                    error.__context__ = None
                    error.__cause__ = None
                    del error
                except (CapacityBackendError, CapacityConfigurationError) as error:
                    stream_primary_error = error
                    primary_error = error
                except Exception as error:  # noqa: BLE001 - redact arbitrary stream failures
                    safe_error = safe_model_client_error(deployment.id, error, (api_key,))
                    if isinstance(safe_error, ModelTransportError) and not isinstance(
                        safe_error, ModelResponseError,
                    ):
                        safe_error = ModelTransportError(
                            "model transport failed", status_code=safe_error.status_code,
                        )
                    stream_primary_error = safe_error
                    _LOGGER.error(
                        "model_stream_unexpected_failure deployment_id=%s error_type=%s",
                        deployment.id,
                        type(error).__name__,
                    )
                    error.__traceback__ = None
                    del error
                    if isinstance(safe_error, ModelResponseError):
                        status_code = safe_error.status_code
                        primary_error = safe_error
                    elif isinstance(safe_error, ModelTransportError):
                        status_code = safe_error.status_code
                        primary_error = ModelTransportError(
                            "model transport failed", status_code=status_code,
                        )
                    else:
                        primary_error = ModelGatewayError("model client internal failure")
                finally:
                    close_error = await self._stream_close_cleanup(
                        events, deadline=deadline
                    )
                    if close_error is not None and stream_primary_error is None:
                        primary_error = ModelGatewayError("model stream cleanup failed")
                    del api_key
        finally:
            if should_record:
                if transport_started is None:  # pragma: no cover - invariant
                    raise ModelGatewayError("model transport timing unavailable")
                latency = max(0.0, self._monotonic() - transport_started)
                try:
                    await self._record_capacity_outcome(
                        capacity,
                        lease,
                        deadline=deadline,
                        status_code=status_code,
                        latency_seconds=latency,
                        succeeded=succeeded,
                    )
                except asyncio.CancelledError as error:
                    if primary_error is None or isinstance(primary_error, ModelResponseError):
                        primary_error = error
                except Exception:  # noqa: BLE001 - preserve any primary model failure
                    if primary_error is None:
                        primary_error = ModelGatewayError("model outcome recording failed")
            release_error = await self._release_cleanup(
                capacity, lease, deadline=deadline
            )
            if isinstance(release_error, asyncio.CancelledError) and isinstance(
                primary_error, ModelResponseError
            ):
                primary_error = release_error
            if release_error is not None and primary_error is None:
                if isinstance(release_error, asyncio.CancelledError):
                    primary_error = release_error
                else:
                    primary_error = ModelGatewayError("model capacity release failed")

        if primary_error is not None:
            raise primary_error from None

    async def _iterate_stream_with_heartbeat(
        self,
        capacity: CapacityController | CapacityPool,
        deployment: Deployment,
        lease: CapacityLease,
        request: ModelRequest,
        transport: OpenAICompatibleChunkTransport,
        api_key: str,
        *,
        deadline: float,
    ) -> AsyncIterator[NormalizedProviderEvent]:
        from agent_hub.harness.streaming import transport_openai_compatible_stream_events

        events = transport_openai_compatible_stream_events(
            transport,
            deployment,
            request,
            api_key,
            provider=deployment.provider_model.split("/", 1)[0],
        )
        heartbeat_task = asyncio.create_task(self._heartbeat(capacity, lease))
        primary_error: BaseException | None = None
        next_event: asyncio.Task[NormalizedProviderEvent] | None = None
        try:
            while True:
                next_event = asyncio.create_task(self._next_stream_event(events))
                done, _pending = await asyncio.wait(
                    {next_event, heartbeat_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if next_event in done:
                    try:
                        event = next_event.result()
                    except StopAsyncIteration:
                        return
                    next_event = None
                    yield event
                else:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    next_event = None
                    await heartbeat_task
                    raise CapacityBackendError("model capacity heartbeat stopped")
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if next_event is not None and not next_event.done():
                next_event.cancel()
                await asyncio.gather(next_event, return_exceptions=True)
            if not heartbeat_task.done():
                heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            close_error = await self._stream_close_cleanup(events, deadline=deadline)
            if close_error is not None and primary_error is None:
                raise ModelGatewayError("model stream cleanup failed") from None

    async def _next_stream_event(
        self, events: AsyncIterator[NormalizedProviderEvent]
    ) -> NormalizedProviderEvent:
        return await anext(events)

    async def _next_stream_event_before_deadline(
        self,
        events: AsyncIterator[NormalizedProviderEvent],
        *,
        deadline: float,
    ) -> NormalizedProviderEvent:
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds <= 0:
            raise TimeoutError
        event_task: asyncio.Task[NormalizedProviderEvent] = asyncio.create_task(
            self._next_stream_event(events)
        )
        try:
            done, _pending = await asyncio.wait(
                {event_task}, timeout=remaining_seconds
            )
        except asyncio.CancelledError:
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)
            raise
        if event_task not in done:
            event_task.cancel()

            def _consume_event_result(task: asyncio.Task[NormalizedProviderEvent]) -> None:
                if not task.cancelled():
                    task.exception()

            event_task.add_done_callback(_consume_event_result)
            raise TimeoutError
        return event_task.result()

    async def _stream_close_cleanup(
        self, events: object, *, deadline: float
    ) -> BaseException | None:
        aclose = getattr(events, "aclose", None)
        if not callable(aclose):
            return None
        close_task = asyncio.create_task(cast(Any, aclose)())
        remaining_seconds = max(0.0, deadline - asyncio.get_running_loop().time())
        try:
            await asyncio.wait_for(asyncio.shield(close_task), timeout=remaining_seconds)
        except TimeoutError:
            close_task.cancel()

            def _consume_close_result(task: asyncio.Task[Any]) -> None:
                if not task.cancelled():
                    task.exception()

            close_task.add_done_callback(_consume_close_result)
            return ModelGatewayError("model stream cleanup failed")
        except asyncio.CancelledError as error:
            close_task.cancel()
            cleanup_error = await _settle_cleanup(close_task)
            if isinstance(cleanup_error, asyncio.CancelledError):
                return error
            return cleanup_error or error
        except BaseException as error:  # noqa: BLE001 - caller preserves primary failures
            return error
        return None

    async def _complete_leased(
        self,
        capacity: CapacityController | CapacityPool,
        deployment: Deployment,
        lease: CapacityLease,
        request: ModelRequest,
        *,
        deadline: float,
        failure_tracking: _GatewayFailureTracking | None = None,
    ) -> ModelResponse:
        primary_error: BaseException | None = None
        response: ModelResponse | None = None
        receipt: ModelResponse | RejectedOutputEvidence | None = None
        transport_started: float | None = None
        should_record = False
        status_code: int | None = None
        history = failure_tracking.history if failure_tracking is not None else None
        try:
            try:
                remaining_seconds = deadline - asyncio.get_running_loop().time()
                if remaining_seconds <= 0:
                    raise TimeoutError
                api_key = await asyncio.wait_for(
                    self._secret_resolver.resolve(deployment.secret_ref),
                    timeout=remaining_seconds,
                )
            except TimeoutError:
                if history is not None:
                    history.mark_incomplete(ScopeIncompletePhase.OUTER_DEADLINE,
                                            ScopeIncompleteReason.DEADLINE_EXHAUSTED)
                primary_error = ModelTransportError(
                    "model request deadline exhausted", status_code=408
                )
            except asyncio.CancelledError as error:
                if history is not None:
                    history.mark_incomplete(ScopeIncompletePhase.CANCELLATION,
                                            ScopeIncompleteReason.CANCELLED)
                primary_error = error
            except Exception:  # noqa: BLE001 - redact resolver details at the boundary
                if history is not None:
                    history.mark_incomplete(ScopeIncompletePhase.PRETRANSPORT_CREDENTIALS,
                                            ScopeIncompleteReason.CREDENTIAL_RESOLUTION_FAILED)
                primary_error = ModelGatewayError("model credential resolution failed")
            else:
                transport_started = self._monotonic()
                invocation = asyncio.create_task(
                    self._invoke_with_heartbeat(
                        capacity, deployment, request, api_key, lease,
                        failure_tracking=failure_tracking,
                    )
                )
                del api_key
                try:
                    remaining_seconds = deadline - asyncio.get_running_loop().time()
                    if remaining_seconds <= 0:
                        raise TimeoutError
                    outcome = await asyncio.wait_for(invocation, timeout=remaining_seconds)
                    receipt = _received_result(outcome)
                    if isinstance(outcome, _SafeTransportFailure):
                        primary_error = outcome.error
                        if isinstance(outcome.error, ModelTransportError):
                            status_code = outcome.error.status_code
                        should_record = True
                    else:
                        if (
                            outcome.text is not None
                            and not outcome.text.strip()
                            and not outcome.tool_calls
                        ):
                            primary_error = ModelGatewayError("model response text is empty")
                            if failure_tracking is not None:
                                failure_tracking.record("empty_response", 200, outcome.usage)
                        elif outcome.text is None and not outcome.tool_calls:
                            primary_error = ModelGatewayError("model response is empty")
                            if failure_tracking is not None:
                                failure_tracking.record("empty_response", 200, outcome.usage)
                        else:
                            response = outcome
                        status_code = 200
                        should_record = True
                except TimeoutError:
                    if history is not None:
                        history.mark_incomplete(ScopeIncompletePhase.OUTER_DEADLINE,
                                                ScopeIncompleteReason.DEADLINE_EXHAUSTED)
                    primary_error = ModelTransportError(
                        "model request deadline exhausted", status_code=408
                    )
                    should_record = True
                except asyncio.CancelledError as error:
                    if history is not None:
                        history.mark_incomplete(ScopeIncompletePhase.CANCELLATION,
                                                ScopeIncompleteReason.CANCELLED)
                    if isinstance(error, ModelResponseCancelled):
                        receipt = error.receipt
                    elif invocation.done() and not invocation.cancelled() and invocation.exception() is None:
                        receipt = _received_result(invocation.result())
                    primary_error = error
                except (CapacityBackendError, CapacityConfigurationError) as error:
                    if history is not None:
                        history.mark_incomplete(ScopeIncompletePhase.TRANSPORT,
                                                ScopeIncompleteReason.CAPACITY_BACKEND_FAILURE)
                    primary_error = error
                except Exception:  # noqa: BLE001 - redact arbitrary injected transport failures
                    if history is not None:
                        history.mark_incomplete(ScopeIncompletePhase.UNKNOWN_ADAPTER,
                                                ScopeIncompleteReason.UNKNOWN_FAILURE)
                    should_record = True
                    primary_error = ModelGatewayError("model client internal failure")
                finally:
                    del invocation

            if isinstance(primary_error, asyncio.CancelledError) and receipt is not None:
                should_record = True
                if isinstance(receipt, ModelResponse):
                    response = receipt
                    status_code = 200

            if should_record:
                if transport_started is None:  # pragma: no cover - invariant
                    raise ModelGatewayError("model transport timing unavailable")
                latency = max(0.0, self._monotonic() - transport_started)
                try:
                    await self._record_capacity_outcome(
                        capacity,
                        lease,
                        deadline=deadline,
                        status_code=status_code,
                        latency_seconds=latency,
                        succeeded=response is not None,
                    )
                except asyncio.CancelledError as error:
                    if history is not None:
                        history.mark_incomplete(ScopeIncompletePhase.RECORDER,
                                                ScopeIncompleteReason.CANCELLED)
                    if primary_error is None or isinstance(
                        primary_error, ModelResponseError | asyncio.CancelledError
                    ):
                        primary_error = error
                except Exception:  # noqa: BLE001 - preserve any primary model failure
                    if history is not None:
                        history.mark_incomplete(ScopeIncompletePhase.RECORDER,
                                                ScopeIncompleteReason.OUTCOME_RECORDING_FAILED)
                    if primary_error is None:
                        primary_error = ModelGatewayError("model outcome recording failed")
        finally:
            release_error = await self._release_cleanup(
                capacity, lease, deadline=deadline
            )
            if isinstance(release_error, asyncio.CancelledError) and isinstance(
                primary_error, ModelResponseError | asyncio.CancelledError
            ):
                primary_error = release_error
            if release_error is not None and history is not None:
                history.mark_incomplete(ScopeIncompletePhase.CLEANUP,
                                        ScopeIncompleteReason.CANCELLED
                                        if isinstance(release_error, asyncio.CancelledError)
                                        else ScopeIncompleteReason.RELEASE_FAILED)
            if release_error is not None and primary_error is None:
                if isinstance(release_error, asyncio.CancelledError):
                    primary_error = release_error
                else:
                    primary_error = ModelGatewayError("model capacity release failed")

        if isinstance(primary_error, asyncio.CancelledError) and receipt is not None:
            cancelled = ModelResponseCancelled(receipt=receipt)
            cancelled.args = primary_error.args
            raise cancelled from None
        if primary_error is not None:
            if failure_tracking is not None and not failure_tracking.recorded:
                failure_tracking.history.mark_incomplete(ScopeIncompletePhase.UNKNOWN_ADAPTER,
                                                         ScopeIncompleteReason.UNKNOWN_FAILURE)
            raise primary_error from None
        if response is None:  # pragma: no cover - defensive invariant
            raise ModelGatewayError("model gateway completed without a response")
        return response

    async def _invoke_with_heartbeat(
        self,
        capacity: CapacityController | CapacityPool,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
        lease: CapacityLease,
        *,
        failure_tracking: _GatewayFailureTracking | None = None,
    ) -> ModelResponse | _SafeTransportFailure:
        transport_task = asyncio.create_task(
            self._call_transport_safely(
                deployment, request, api_key, failure_tracking=failure_tracking,
            )
        )
        del api_key
        heartbeat_task = asyncio.create_task(self._heartbeat(capacity, lease))
        outcome: ModelResponse | _SafeTransportFailure | None = None
        primary_error: BaseException | None = None
        try:
            done, _pending = await asyncio.wait(
                {transport_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if transport_task in done:
                outcome = transport_task.result()
            else:
                await heartbeat_task
                raise CapacityBackendError("model capacity heartbeat stopped")
        except asyncio.CancelledError as error:
            primary_error = error
        except Exception as error:  # noqa: BLE001 - cleanup before propagating worker failure
            primary_error = error
        finally:
            for task in (transport_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            cleanup_error = await _settle_cleanup(
                asyncio.gather(transport_task, heartbeat_task, return_exceptions=True)
            )
            if cleanup_error is not None and (
                primary_error is None or isinstance(primary_error, asyncio.CancelledError)
            ):
                primary_error = cleanup_error
        if isinstance(primary_error, asyncio.CancelledError):
            # Cancellation may arrive after transport completion but before delivery.
            if outcome is None and not transport_task.cancelled():
                outcome = transport_task.result()
            receipt = _received_result(outcome)
            if receipt is not None:
                cancelled = ModelResponseCancelled(receipt=receipt)
                cancelled.args = primary_error.args
                raise cancelled from None
        if primary_error is not None:
            raise primary_error from None
        if outcome is None:  # pragma: no cover - completed transport invariant
            raise ModelGatewayError("model transport result unavailable")
        return outcome

    async def _call_transport_safely(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
        *,
        failure_tracking: _GatewayFailureTracking | None = None,
    ) -> ModelResponse | _SafeTransportFailure:
        outcome: ModelResponse | _SafeTransportFailure
        try:
            if failure_tracking is not None:
                failure_tracking.enter()
            outcome = await self._transport.complete(deployment, request, api_key)
        except ModelResponseCancelled as error:
            # A task-cancelled state/gather can discard a CancelledError subclass's receipt.
            if failure_tracking is not None:
                failure_tracking.history.mark_incomplete(ScopeIncompletePhase.CANCELLATION,
                                                         ScopeIncompleteReason.CANCELLED)
            outcome = _SafeTransportFailure(ModelResponseCancelled(receipt=error.receipt))
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            del error
        except asyncio.CancelledError:
            raise
        except ModelResponseError as error:
            _LOGGER.warning("model_response_rejected deployment_id=%s", deployment.id)
            if failure_tracking is not None:
                failure_tracking.history.mark_incomplete(ScopeIncompletePhase.TRANSPORT,
                                                         ScopeIncompleteReason.REJECTED_OUTPUT)
            outcome = _SafeTransportFailure(ModelResponseError(
                "model response rejected", status_code=error.status_code, evidence=error.evidence,
            ))
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            del error
        except ModelTransportError as error:
            if failure_tracking is not None:
                failure_tracking.record("transport_error", error.status_code)
            _LOGGER.warning(
                "model_transport_failed deployment_id=%s status_code=%s error_type=%s",
                deployment.id,
                error.status_code,
                type(error).__name__,
            )
            outcome = _SafeTransportFailure(
                ModelTransportError("model transport failed", status_code=error.status_code)
            )
            error.__traceback__ = None
            del error
        except Exception as error:  # noqa: BLE001 - consume and redact injected failures
            safe_error = safe_model_client_error(deployment.id, error, (api_key,))
            if isinstance(safe_error, ModelTransportError) and not isinstance(
                safe_error, ModelResponseError,
            ):
                safe_error = ModelTransportError(
                    "model transport failed", status_code=safe_error.status_code,
                )
            _LOGGER.error(
                "model_transport_unexpected_failure deployment_id=%s error_type=%s",
                deployment.id,
                type(error).__name__,
            )
            error.__traceback__ = None
            del error
            if isinstance(safe_error, ModelResponseError):
                if failure_tracking is not None:
                    failure_tracking.history.mark_incomplete(ScopeIncompletePhase.TRANSPORT,
                                                             ScopeIncompleteReason.REJECTED_OUTPUT)
                outcome = _SafeTransportFailure(safe_error)
            elif isinstance(safe_error, ModelTransportError):
                if failure_tracking is not None:
                    failure_tracking.record("transport_error", safe_error.status_code)
                outcome = _SafeTransportFailure(
                    ModelTransportError("model transport failed", status_code=safe_error.status_code)
                )
            else:
                if failure_tracking is not None:
                    failure_tracking.history.mark_incomplete(ScopeIncompletePhase.UNKNOWN_ADAPTER,
                                                             ScopeIncompleteReason.UNKNOWN_FAILURE)
                outcome = _SafeTransportFailure(ModelGatewayError("model client internal failure"))
        del api_key, request
        return outcome

    async def _heartbeat(
        self, capacity: CapacityController | CapacityPool, lease: CapacityLease
    ) -> ModelResponse:
        current = lease
        immediate_renewals = 0
        while True:
            delay = min(self._heartbeat_interval, current.renew_after_seconds)
            if delay == 0:
                immediate_renewals += 1
                if immediate_renewals > 3:
                    raise CapacityBackendError("model capacity renewal timing unavailable")
            else:
                immediate_renewals = 0
            await asyncio.sleep(delay)
            renewed = await capacity.renew(current)
            if renewed is None:
                raise CapacityBackendError("model capacity lease expired")
            current = renewed

    async def _release_cleanup(
        self,
        capacity: CapacityController | CapacityPool,
        lease: CapacityLease,
        *,
        deadline: float | None = None,
    ) -> BaseException | None:
        release_task = asyncio.create_task(capacity.release(lease))
        timeout = self._capacity_wait_timeout
        if deadline is not None:
            timeout = min(timeout, max(0.0, deadline - asyncio.get_running_loop().time()))
        try:
            await asyncio.wait_for(
                asyncio.shield(release_task),
                timeout=timeout,
            )
        except TimeoutError:
            release_task.cancel()

            def _consume_release_result(task: asyncio.Task[bool]) -> None:
                if not task.cancelled():
                    task.exception()

            release_task.add_done_callback(_consume_release_result)
            return CapacityBackendError("model capacity release failed")
        except asyncio.CancelledError as error:
            release_task.cancel()
            cleanup_error = await _settle_cleanup(release_task)
            if isinstance(cleanup_error, asyncio.CancelledError):
                return error
            return cleanup_error or error
        except BaseException as error:  # noqa: BLE001 - caller decides precedence
            return error
        return None
