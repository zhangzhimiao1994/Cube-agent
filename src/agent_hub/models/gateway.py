"""The sole leased, redacted path from model requests to model transports."""

import asyncio
import math
import time
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Protocol

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityLease,
    CapacityPool,
    CapacityUnavailable,
)
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, _require_safe_identifier


class ModelGatewayError(RuntimeError):
    """Stable, redacted failure at the model gateway boundary."""


class SecretResolver(Protocol):
    async def resolve(self, secret_ref: str) -> str: ...


class ModelTransport(Protocol):
    async def complete(
        self, deployment: Deployment, request: ModelRequest, api_key: str
    ) -> ModelResponse: ...


class TokenEstimator(Protocol):
    def estimate(self, request: ModelRequest) -> int: ...


class CapacityController(Protocol):
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
    ) -> None: ...


class ConservativeTokenEstimator:
    """Deterministic local upper estimate with fixed response headroom."""

    def estimate(self, request: ModelRequest) -> int:
        characters = 0
        for message in request.messages:
            if isinstance(message.content, str):
                characters += len(message.content)
                continue
            for part in message.content:
                characters += self._string_size(part)
        if request.response_schema is not None:
            characters += self._string_size(request.response_schema.schema)
        return 513 + max(1, math.ceil(characters / 2))

    def _string_size(self, value: object) -> int:
        if isinstance(value, str):
            return len(value)
        if isinstance(value, Mapping):
            return sum(len(key) + self._string_size(item) for key, item in value.items())
        if isinstance(value, tuple | list):
            return sum(self._string_size(item) for item in value)
        return 0


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
        token_estimator: TokenEstimator | None = None,
        monotonic: Callable[[], float] = time.monotonic,
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
        configured_fallbacks = dict(fallbacks or {})
        self._validate_fallbacks(registry, configured_fallbacks)
        capacity_pool.validate_configuration(registry.deployments)
        self._registry = registry
        self._capacity = capacity_pool
        self._secret_resolver = secret_resolver
        self._transport = transport
        self._fallbacks = MappingProxyType(configured_fallbacks)
        self._capacity_wait_timeout = float(capacity_wait_timeout)
        self._heartbeat_interval = float(heartbeat_interval)
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
        estimated_tokens = self._token_estimator.estimate(request)
        if type(estimated_tokens) is not int or estimated_tokens <= 0:
            raise ValueError("token estimator must return a strict positive integer")
        models = self._fallback_chain(request.logical_model, request.allow_fallback)
        lease: CapacityLease | None = None
        selected: Deployment | None = None
        for logical_model in models:
            try:
                candidates = self._registry.candidates(
                    logical_model, request.required_capabilities
                )
            except NoCapableDeployment:
                if logical_model == request.logical_model:
                    raise
                break
            try:
                lease = await self._capacity.acquire(
                    candidates,
                    self._capacity_wait_timeout,
                    estimated_tokens=estimated_tokens,
                )
            except CapacityUnavailable:
                continue
            selected = next(
                (item for item in candidates if item.id == lease.deployment_id), None
            )
            if selected is None or selected.quota_scope_id != lease.quota_scope_id:
                await self._release_cleanup(lease)
                raise CapacityBackendError("model capacity returned an unknown deployment")
            break
        if lease is None or selected is None:
            raise CapacityUnavailable("model capacity unavailable") from None
        return await self._complete_leased(selected, lease, request)

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
        start = self._monotonic()
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
                invocation = asyncio.create_task(
                    self._invoke_with_heartbeat(deployment, request, api_key, lease)
                )
                del api_key
                try:
                    response = await invocation
                    status_code = 200
                    should_record = True
                except asyncio.CancelledError as error:
                    primary_error = error
                except ModelTransportError as error:
                    status_code = error.status_code
                    should_record = True
                    primary_error = error.with_traceback(None)
                except CapacityBackendError as error:
                    primary_error = error
                except Exception:  # noqa: BLE001 - redact arbitrary injected transport failures
                    should_record = True
                    primary_error = ModelGatewayError("model transport failed")

            if should_record:
                latency = max(0.0, self._monotonic() - start)
                try:
                    await self._capacity.record_outcome(
                        lease.quota_scope_id,
                        status_code=status_code,
                        latency_seconds=latency,
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
    ) -> ModelResponse:
        transport_task = asyncio.create_task(
            self._transport.complete(deployment, request, api_key)
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

    async def _heartbeat(self, lease: CapacityLease) -> ModelResponse:
        current = lease
        while True:
            await asyncio.sleep(self._heartbeat_interval)
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
