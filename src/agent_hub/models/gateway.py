"""The sole leased, redacted path from model requests to model transports."""

import asyncio
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from types import MappingProxyType
from typing import Protocol

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityLease,
    CapacityPool,
    CapacityUnavailable,
    CapacityWaitTimeout,
)
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, _require_safe_identifier


class ModelGatewayError(RuntimeError):
    """Stable, redacted failure at the model gateway boundary."""


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
    cost_usd: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not isinstance(self.response, ModelResponse):
            raise TypeError("response must be ModelResponse")
        _require_safe_identifier("deployment id", self.deployment_id)
        _require_safe_identifier("logical model", self.logical_model)
        _require_safe_identifier("provider id", self.provider_id)
        cost_exponent = self.cost_usd.as_tuple().exponent
        if (
            not self.provider_model
            or self.provider_model != self.provider_model.strip()
            or len(self.provider_model) > 512
        ):
            raise ValueError("provider_model must be bounded and unpadded")
        if self.provider_model.split("/", 1)[0] != self.provider_id:
            raise ValueError("provider provenance is inconsistent")
        if (
            type(self.cost_usd) is not Decimal
            or not self.cost_usd.is_finite()
            or self.cost_usd < 0
            or (isinstance(cost_exponent, int) and cost_exponent < -6)
            or self.cost_usd > Decimal(1000000)
        ):
            raise ValueError("gateway cost must be a bounded USD decimal")


@dataclass(frozen=True, slots=True)
class _SafeTransportFailure:
    error: ModelTransportError | ModelGatewayError


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
        estimated_tokens: int,
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
    """Deterministic local upper estimate using the request's output budget."""

    def estimate(self, request: ModelRequest) -> int:
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
        ).encode("utf-8")
        return len(normalized) + request.max_output_tokens

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

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        estimated_tokens = self._token_estimator.estimate(request)
        if type(estimated_tokens) is not int or estimated_tokens <= 0:
            raise ValueError("token estimator must return a strict positive integer")
        models = self._fallback_chain(request.logical_model, request.allow_fallback)
        lease: CapacityLease | None = None
        selected: Deployment | None = None
        for logical_model in models:
            try:
                candidates = self._registry.candidates(logical_model, request.required_capabilities)
            except NoCapableDeployment:
                if logical_model == request.logical_model:
                    raise
                break
            try:
                await self._capacity.initialize()
                lease = await self._capacity.acquire(
                    candidates,
                    self._capacity_wait_timeout,
                    estimated_tokens=estimated_tokens,
                )
            except CapacityWaitTimeout:
                continue
            selected = next((item for item in candidates if item.id == lease.deployment_id), None)
            if selected is None or selected.quota_scope_id != lease.quota_scope_id:
                cleanup_error = await self._release_cleanup(lease)
                if isinstance(cleanup_error, asyncio.CancelledError):
                    raise cleanup_error
                if cleanup_error is not None:
                    raise CapacityBackendError("model capacity release failed") from None
                raise CapacityBackendError("model capacity returned an unknown deployment")
            break
        if lease is None or selected is None:
            raise CapacityUnavailable("model capacity unavailable") from None
        response = await self._complete_leased(selected, lease, request)
        return GatewayCompletion(
            response=response,
            deployment_id=selected.id,
            logical_model=selected.logical_model,
            provider_id=selected.provider_model.split("/", 1)[0],
            provider_model=selected.provider_model,
            cost_usd=self._cost_usd(selected, response),
        )

    def _cost_usd(self, deployment: Deployment, response: ModelResponse) -> Decimal:
        pricing = self._pricing.get(deployment.id)
        usage = response.usage
        if pricing is None or usage is None:
            return Decimal(0)
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

    async def _complete_leased(
        self, deployment: Deployment, lease: CapacityLease, request: ModelRequest
    ) -> ModelResponse:
        primary_error: BaseException | None = None
        response: ModelResponse | None = None
        transport_started: float | None = None
        should_record = False
        status_code: int | None = None
        try:
            try:
                api_key = await self._secret_resolver.resolve(deployment.secret_ref)
            except asyncio.CancelledError as error:
                primary_error = error
            except Exception:  # noqa: BLE001 - redact resolver details at the boundary
                primary_error = ModelGatewayError("model credential resolution failed")
            else:
                transport_started = self._monotonic()
                invocation = asyncio.create_task(
                    self._invoke_with_heartbeat(deployment, request, api_key, lease)
                )
                del api_key
                try:
                    try:
                        outcome = await invocation
                    finally:
                        del invocation
                    if isinstance(outcome, _SafeTransportFailure):
                        primary_error = outcome.error
                        if isinstance(outcome.error, ModelTransportError):
                            status_code = outcome.error.status_code
                        should_record = True
                    else:
                        response = outcome
                        status_code = 200
                        should_record = True
                except asyncio.CancelledError as error:
                    primary_error = error
                except (CapacityBackendError, CapacityConfigurationError) as error:
                    primary_error = error
                except Exception:  # noqa: BLE001 - redact arbitrary injected transport failures
                    should_record = True
                    primary_error = ModelGatewayError("model transport failed")

            if should_record:
                if transport_started is None:  # pragma: no cover - invariant
                    raise ModelGatewayError("model transport timing unavailable")
                latency = max(0.0, self._monotonic() - transport_started)
                try:
                    await self._capacity.record_outcome(
                        lease.quota_scope_id,
                        status_code=status_code,
                        latency_seconds=latency,
                        succeeded=response is not None,
                    )
                except asyncio.CancelledError as error:
                    if primary_error is None:
                        primary_error = error
                except Exception:  # noqa: BLE001 - preserve any primary model failure
                    if primary_error is None:
                        primary_error = ModelGatewayError("model outcome recording failed")
        finally:
            release_error = await self._release_cleanup(lease)
            if release_error is not None and primary_error is None:
                if isinstance(release_error, asyncio.CancelledError):
                    primary_error = release_error
                else:
                    primary_error = ModelGatewayError("model capacity release failed")

        if primary_error is not None:
            raise primary_error from None
        if response is None:  # pragma: no cover - defensive invariant
            raise ModelGatewayError("model gateway completed without a response")
        return response

    async def _invoke_with_heartbeat(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
        lease: CapacityLease,
    ) -> ModelResponse | _SafeTransportFailure:
        transport_task = asyncio.create_task(
            self._call_transport_safely(deployment, request, api_key)
        )
        del api_key
        heartbeat_task = asyncio.create_task(self._heartbeat(lease))
        try:
            done, _pending = await asyncio.wait(
                {transport_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if transport_task in done:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
                return transport_task.result()
            transport_task.cancel()
            await asyncio.gather(transport_task, return_exceptions=True)
            return await heartbeat_task
        finally:
            for task in (transport_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(transport_task, heartbeat_task, return_exceptions=True)

    async def _call_transport_safely(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse | _SafeTransportFailure:
        outcome: ModelResponse | _SafeTransportFailure
        try:
            outcome = await self._transport.complete(deployment, request, api_key)
        except asyncio.CancelledError:
            raise
        except ModelTransportError as error:
            outcome = _SafeTransportFailure(
                ModelTransportError("model transport failed", status_code=error.status_code)
            )
            error.__traceback__ = None
            del error
        except Exception as error:  # noqa: BLE001 - consume and redact injected failures
            error.__traceback__ = None
            del error
            outcome = _SafeTransportFailure(ModelGatewayError("model transport failed"))
        del api_key, request
        return outcome

    async def _heartbeat(self, lease: CapacityLease) -> ModelResponse:
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
            renewed = await self._capacity.renew(current)
            if renewed is None:
                raise CapacityBackendError("model capacity lease expired")
            current = renewed

    async def _release_cleanup(self, lease: CapacityLease) -> BaseException | None:
        release_task = asyncio.create_task(self._capacity.release(lease))
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError as error:
            try:
                await release_task
            except Exception as cleanup_error:  # noqa: BLE001 - preserve cancellation
                del cleanup_error
            return error
        except Exception as error:  # noqa: BLE001 - caller decides primary precedence
            return error
        return None
