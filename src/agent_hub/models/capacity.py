"""Atomic, quota-scoped model concurrency, rate, and health controls."""

import asyncio
import hashlib
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from types import MappingProxyType
from typing import Protocol, cast
from uuid import UUID, uuid4

from agent_hub.models.types import Deployment, _require_safe_identifier

_SAFE_PREFIX = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_SAFE_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_STATE_TTL_MS = 3_600_000
_MAX_OWNER_RECORDS = 4096


class CapacityUnavailable(asyncio.TimeoutError):
    """Stable base class for ordinary capacity admission failures."""


class CapacityWaitTimeout(CapacityUnavailable):
    """The configured capacity wait deadline elapsed without a lease."""


class CapacityQueueFull(CapacityUnavailable):
    """The bounded local wait queue rejected admission immediately."""


class CapacityBackendError(RuntimeError):
    """Stable fail-closed error for unavailable or malformed capacity state."""


class CapacityConfigurationError(RuntimeError):
    """Stable error for incomplete or conflicting non-secret capacity metadata."""


class RedisCapacityClient(Protocol):
    async def eval(self, script: str, key_count: int, *args: object) -> object: ...


@dataclass(frozen=True, slots=True, repr=False)
class CapacityLease:
    id: str
    deployment_id: str
    quota_scope_id: str
    expires_at: datetime
    renew_after_seconds: float

    def __post_init__(self) -> None:
        try:
            parsed_id = UUID(self.id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("lease id must be a canonical UUID") from None
        if str(parsed_id) != self.id:
            raise ValueError("lease id must be a canonical UUID")
        _require_safe_identifier("deployment id", self.deployment_id)
        _require_safe_identifier("quota_scope_id", self.quota_scope_id)
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("lease expiry must be timezone-aware")
        try:
            timestamp = self.expires_at.timestamp()
        except (OverflowError, OSError, ValueError):
            raise ValueError("lease expiry must be finite") from None
        if not math.isfinite(timestamp):
            raise ValueError("lease expiry must be finite")
        if (
            isinstance(self.renew_after_seconds, bool)
            or not isinstance(self.renew_after_seconds, int | float)
            or not math.isfinite(self.renew_after_seconds)
            or self.renew_after_seconds < 0
        ):
            raise ValueError("lease renewal delay must be finite and nonnegative")

    def __repr__(self) -> str:
        return "CapacityLease(<redacted>)"

    __str__ = __repr__


CapacityLease.__hash__ = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True, repr=False)
class CredentialDescriptor:
    secret_ref: str = field(repr=False)
    fingerprint: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.secret_ref or self.secret_ref != self.secret_ref.strip():
            raise ValueError("secret_ref must be nonblank and unpadded")
        if _SAFE_FINGERPRINT.fullmatch(self.fingerprint) is None:
            raise ValueError("credential fingerprint must be a lowercase SHA-256 digest")

    def __repr__(self) -> str:
        return "CredentialDescriptor(<redacted>)"


class CredentialRegistry:
    """Non-secret lookup used to deduplicate capacity before secret resolution."""

    def __init__(self, descriptors: Iterable[CredentialDescriptor]) -> None:
        by_ref: dict[str, str] = {}
        for descriptor in descriptors:
            if not isinstance(descriptor, CredentialDescriptor):
                raise TypeError("credentials must contain CredentialDescriptor values")
            previous = by_ref.get(descriptor.secret_ref)
            if previous is not None and previous != descriptor.fingerprint:
                raise ValueError("secret reference has conflicting credential fingerprints")
            by_ref[descriptor.secret_ref] = descriptor.fingerprint
        self._by_ref = MappingProxyType(by_ref)

    def fingerprint_for(self, secret_ref: str) -> str | None:
        return self._by_ref.get(secret_ref)


@dataclass(frozen=True, slots=True)
class _ScopePolicy:
    base_limit: int
    rpm: int | None
    tpm: int | None


@dataclass(slots=True)
class _RegistrationState:
    initialized: bool = False
    valid_until: float = 0.0
    refresh_in_progress: bool = False
    next_metadata_refresh: float = 0.0


def safe_operational_limit(
    max_concurrency: int, target_utilization: float, reserved_slots: int
) -> int:
    if type(max_concurrency) is not int or max_concurrency <= 0:
        raise ValueError("max_concurrency must be a positive integer")
    if (
        isinstance(target_utilization, bool)
        or not isinstance(target_utilization, int | float)
        or not math.isfinite(target_utilization)
        or not 0 < target_utilization <= 1
    ):
        raise ValueError("target_utilization must be finite and between zero and one")
    if (
        type(reserved_slots) is not int
        or reserved_slots < 0
        or reserved_slots >= max_concurrency
    ):
        raise ValueError("reserved_slots must be nonnegative and below max_concurrency")
    return max(
        1,
        min(
            math.floor(max_concurrency * target_utilization),
            max_concurrency - reserved_slots,
        ),
    )


def _script(name: str) -> str:
    return files("agent_hub.models.redis").joinpath(name).read_text(encoding="utf-8")


_ACQUIRE_SCRIPT = _script("acquire_lease.lua")
_RELEASE_SCRIPT = _script("release_lease.lua")
_RENEW_SCRIPT = _script("renew_lease.lua")
_RECORD_OUTCOME_SCRIPT = _script("record_outcome.lua")
_REGISTER_SCOPE_SCRIPT = _script("register_scope_policy.lua")
_CLAIM_FINGERPRINT_SCRIPT = _script("claim_fingerprint.lua")
_ROLLBACK_FINGERPRINT_SCRIPT = _script("rollback_fingerprint.lua")
_ROLLBACK_SCOPE_SCRIPT = _script("rollback_scope_policy.lua")
_EFFECTIVE_LIMIT_SCRIPT = """
local base = tonumber(redis.call('HGET', KEYS[2], 'base'))
if base == nil or base < 1 then return redis.error_reply('model scope policy is unavailable') end
local value = tonumber(redis.call('HGET', KEYS[1], 'effective')) or base
value = math.max(1, math.min(value, base))
redis.call('HSET', KEYS[1], 'effective', value)
redis.call('PEXPIRE', KEYS[1], ARGV[1])
return value
"""


def _most_restrictive_optional(first: int | None, second: int | None) -> int | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


class CapacityPool:
    def __init__(
        self,
        redis_client: object,
        *,
        deployments: Iterable[Deployment] = (),
        credentials: CredentialRegistry | None = None,
        fingerprint_resolver: Callable[[str], Awaitable[str]] | None = None,
        key_prefix: str = "agent-hub:model-capacity:",
        lease_seconds: float = 30,
        poll_interval: float = 0.05,
        max_waiters: int = 1024,
        cooldown_seconds: float = 30,
        latency_threshold_seconds: float = 30,
        health_window: int = 20,
        metadata_ttl_seconds: float = 3600,
        metadata_refresh_interval: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        owner_id: str | None = None,
    ) -> None:
        if _SAFE_PREFIX.fullmatch(key_prefix) is None:
            raise ValueError("capacity key prefix is invalid")
        for name, value in (
            ("lease_seconds", lease_seconds),
            ("poll_interval", poll_interval),
            ("cooldown_seconds", cooldown_seconds),
            ("latency_threshold_seconds", latency_threshold_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive and finite")
        if type(max_waiters) is not int or max_waiters <= 0:
            raise ValueError("max_waiters must be a positive integer")
        if type(health_window) is not int or not 1 <= health_window <= 100:
            raise ValueError("health_window must be between 1 and 100")
        if (
            isinstance(metadata_ttl_seconds, bool)
            or not isinstance(metadata_ttl_seconds, int | float)
            or not math.isfinite(metadata_ttl_seconds)
            or metadata_ttl_seconds <= 0
        ):
            raise ValueError("metadata_ttl_seconds must be positive and finite")
        refresh_interval = (
            float(metadata_ttl_seconds) / 4
            if metadata_refresh_interval is None
            else metadata_refresh_interval
        )
        if (
            isinstance(refresh_interval, bool)
            or not isinstance(refresh_interval, int | float)
            or not math.isfinite(refresh_interval)
            or refresh_interval <= 0
            or refresh_interval >= float(metadata_ttl_seconds) / 3
        ):
            raise ValueError("metadata_refresh_interval must be positive and below TTL/3")
        lease_ms = math.ceil(float(lease_seconds) * 1000)
        metadata_ttl_ms = math.ceil(float(metadata_ttl_seconds) * 1000)
        if metadata_ttl_ms < 3 * lease_ms:
            raise ValueError("metadata_ttl_seconds must cover at least three lease horizons")
        configured_owner = str(uuid4()) if owner_id is None else owner_id
        try:
            parsed_owner = UUID(configured_owner)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("capacity owner_id must be a canonical UUID") from None
        if str(parsed_owner) != configured_owner:
            raise ValueError("capacity owner_id must be a canonical UUID")
        self._redis = cast(RedisCapacityClient, redis_client)
        self._credentials = credentials
        self._fingerprint_resolver = fingerprint_resolver
        self._key_prefix = key_prefix
        self._lease_ms = lease_ms
        self._poll_interval = float(poll_interval)
        self._max_waiters = max_waiters
        self._cooldown_ms = math.ceil(cooldown_seconds * 1000)
        self._latency_threshold_ms = math.ceil(latency_threshold_seconds * 1000)
        self._health_window = health_window
        self._metadata_ttl_ms = metadata_ttl_ms
        self._metadata_refresh_interval = float(refresh_interval)
        self._monotonic = monotonic
        self._owner_id = configured_owner
        self._waiters = 0
        self._waiter_lock = asyncio.Lock()
        self._registration_lock = asyncio.Lock()
        self._scope_policies: dict[str, _ScopePolicy] = {}
        self._catalog_by_id: dict[str, Deployment] = {}
        self._initialized = False
        self._catalog_frozen = False
        self._registration_valid_until = 0.0
        self._refresh_in_progress = False
        self._next_metadata_refresh = 0.0
        self._observed_loads: dict[str, int] = {}
        self._weights: dict[str, int] = {}
        self._fingerprint_cache: dict[str, str] = {}
        self._scoped_registration_states: dict[tuple[str, ...], _RegistrationState] = {}
        configured = tuple(deployments)
        self.validate_configuration(configured)

    def scoped(self, deployments: Sequence[Deployment]) -> "_CapacityScopeView":
        """Create a catalog-scoped view that shares runtime capacity state."""
        configured = tuple(deployments)
        if not configured:
            raise CapacityConfigurationError("model capacity catalog is empty")
        for deployment in configured:
            if self._catalog_by_id.get(deployment.id) != deployment:
                raise CapacityConfigurationError("deployment is outside capacity catalog")
        return _CapacityScopeView(self, configured)

    def _registration_state_for(
        self, catalog_by_id: Mapping[str, Deployment]
    ) -> _RegistrationState:
        key = tuple(sorted(catalog_by_id))
        state = self._scoped_registration_states.get(key)
        if state is None:
            state = _RegistrationState()
            self._scoped_registration_states[key] = state
        return state

    def validate_configuration(self, deployments: Iterable[Deployment]) -> None:
        for deployment in deployments:
            if not isinstance(deployment, Deployment):
                raise TypeError("deployments must contain only Deployment values")
            existing_deployment = self._catalog_by_id.get(deployment.id)
            if existing_deployment is not None and existing_deployment != deployment:
                raise CapacityConfigurationError("deployment capacity configuration conflicts")
            if self._catalog_frozen and existing_deployment is None:
                raise CapacityConfigurationError("capacity catalog is already initialized")
            self._catalog_by_id[deployment.id] = deployment
            policy = _ScopePolicy(
                safe_operational_limit(
                    deployment.max_concurrency,
                    deployment.target_utilization,
                    deployment.reserved_slots,
                ),
                deployment.rpm,
                deployment.tpm,
            )
            existing = self._scope_policies.get(deployment.quota_scope_id)
            if existing is not None:
                policy = _ScopePolicy(
                    min(existing.base_limit, policy.base_limit),
                    _most_restrictive_optional(existing.rpm, policy.rpm),
                    _most_restrictive_optional(existing.tpm, policy.tpm),
                )
            self._scope_policies[deployment.quota_scope_id] = policy

    async def initialize(self) -> None:
        """Atomically register a complete published catalog before serving requests.

        Every worker must receive the same full catalog. Redis only converges values
        downward, so a permissive or stale worker can never raise a scope policy.
        """
        await self._ensure_registration(force=False)

    async def _ensure_registration(self, *, force: bool) -> None:
        if not self._catalog_by_id:
            raise CapacityConfigurationError("model capacity catalog is empty")
        if self._credentials is None and self._fingerprint_resolver is None:
            raise CapacityConfigurationError("credential fingerprints are required")
        now = self._clock_now()
        if (
            not force
            and self._initialized
            and now < self._next_metadata_refresh
            and now < self._registration_valid_until
        ):
            return
        async with self._registration_lock:
            now = self._clock_now()
            if (
                not force
                and self._initialized
                and now < self._next_metadata_refresh
                and now < self._registration_valid_until
            ):
                return
            prior_registration_current = (
                self._initialized and now < self._registration_valid_until
            )
            self._refresh_in_progress = True
            try:
                valid_until = await self._register_metadata(
                    catalog_by_id=self._catalog_by_id,
                    scope_policies=self._scope_policies,
                    prior_registration_current=prior_registration_current
                )
            except BaseException:
                self._initialized = False
                if not prior_registration_current:
                    self._registration_valid_until = 0.0
                raise
            else:
                self._initialized = True
                self._catalog_frozen = True
                self._registration_valid_until = valid_until
                self._next_metadata_refresh = (
                    self._clock_now() + self._metadata_refresh_interval
                )
            finally:
                self._refresh_in_progress = False

    async def _register_metadata(
        self,
        *,
        catalog_by_id: Mapping[str, Deployment],
        scope_policies: Mapping[str, _ScopePolicy],
        prior_registration_current: bool,
    ) -> float:
        registration_started = self._clock_now()
        credentials = await self._resolved_credentials(catalog_by_id.values())
        fingerprint_scopes: dict[str, str] = {}
        deployment_fingerprints: list[tuple[Deployment, str]] = []
        for deployment in catalog_by_id.values():
            fingerprint = credentials.fingerprint_for(deployment.secret_ref)
            if fingerprint is None:
                raise CapacityConfigurationError("credential fingerprint metadata is incomplete")
            previous_scope = fingerprint_scopes.get(fingerprint)
            if previous_scope is not None and previous_scope != deployment.quota_scope_id:
                raise CapacityConfigurationError("credential fingerprint quota scope conflicts")
            fingerprint_scopes[fingerprint] = deployment.quota_scope_id
            deployment_fingerprints.append((deployment, fingerprint))
        owner = self._owner_id
        planned_fingerprint_keys = list(
            dict.fromkeys(
                self._fingerprint_keys(fingerprint)
                for _deployment, fingerprint in deployment_fingerprints
            )
        )
        planned_policy_keys = [
            self._keys(quota_scope_id) for quota_scope_id in scope_policies
        ]
        fingerprint_keys: list[tuple[str, str]] = []
        policy_keys: list[Mapping[str, str]] = []
        try:
            for deployment, fingerprint in deployment_fingerprints:
                claim_keys = self._fingerprint_keys(fingerprint)
                claimed, cancellation = await self._registration_eval(
                    _CLAIM_FINGERPRINT_SCRIPT,
                    2,
                    *claim_keys,
                    owner,
                    self._scope_digest(deployment.quota_scope_id),
                    self._metadata_ttl_ms,
                    _MAX_OWNER_RECORDS,
                )
                accepted, created, owner_count = self._integer_result(claimed, 3)
                if not 0 <= owner_count <= _MAX_OWNER_RECORDS:
                    raise CapacityBackendError(
                        "model capacity backend returned invalid state"
                    )
                if accepted != 1:
                    raise CapacityConfigurationError(
                        "credential fingerprint quota scope conflicts"
                    )
                if created not in {0, 1}:
                    raise CapacityBackendError(
                        "model capacity backend returned invalid state"
                    )
                if created:
                    fingerprint_keys.append(claim_keys)
                if cancellation is not None:
                    raise cancellation
            for quota_scope_id, policy in scope_policies.items():
                keys = self._keys(quota_scope_id)
                result, cancellation = await self._registration_eval(
                    _REGISTER_SCOPE_SCRIPT,
                    7,
                    *self._policy_registration_keys(keys),
                    owner,
                    policy.base_limit,
                    policy.rpm or 0,
                    policy.tpm or 0,
                    self._metadata_ttl_ms,
                    _MAX_OWNER_RECORDS,
                )
                _base, _rpm, _tpm, created, owner_count = self._integer_result(result, 5)
                if created not in {0, 1} or not 0 <= owner_count <= _MAX_OWNER_RECORDS:
                    raise CapacityBackendError(
                        "model capacity backend returned invalid state"
                    )
                if created:
                    policy_keys.append(keys)
                if cancellation is not None:
                    raise cancellation
            valid_until = self._registration_deadline(registration_started)
        except BaseException as error:
            await self._rollback_registration(
                owner,
                fingerprint_keys if prior_registration_current else planned_fingerprint_keys,
                policy_keys if prior_registration_current else planned_policy_keys,
            )
            if isinstance(error, asyncio.CancelledError | CapacityConfigurationError):
                raise
            if isinstance(error, CapacityBackendError):
                raise
            raise CapacityBackendError("model capacity backend unavailable") from None
        return valid_until

    async def _resolved_credentials(
        self, deployments: Iterable[Deployment]
    ) -> CredentialRegistry:
        if self._credentials is not None:
            return self._credentials
        resolver = self._fingerprint_resolver
        if resolver is None:  # pragma: no cover - initialize validates before entry
            raise CapacityConfigurationError("credential fingerprints are required")
        resolution_failed = False
        try:
            secret_refs = tuple(
                dict.fromkeys(deployment.secret_ref for deployment in deployments)
            )
            descriptors = [
                CredentialDescriptor(secret_ref, self._fingerprint_cache[secret_ref])
                for secret_ref in secret_refs
                if secret_ref in self._fingerprint_cache
            ]
            for secret_ref in secret_refs:
                if secret_ref in self._fingerprint_cache:
                    continue
                fingerprint = await resolver(secret_ref)
                self._fingerprint_cache[secret_ref] = fingerprint
                descriptors.append(CredentialDescriptor(secret_ref, fingerprint))
            return CredentialRegistry(descriptors)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - redact secret backend details at the boundary
            resolution_failed = True
        if resolution_failed:
            raise CapacityConfigurationError("credential fingerprint resolution failed")
        raise CapacityConfigurationError("credential fingerprint resolution failed")

    def _registration_deadline(self, started: float) -> float:
        ttl_seconds = self._metadata_ttl_ms / 1000
        safety_margin = max(0.001, ttl_seconds * 0.1)
        deadline = started + max(0.0, ttl_seconds - safety_margin)
        if not math.isfinite(deadline):
            raise CapacityConfigurationError("capacity registration deadline is invalid")
        return deadline

    async def _registration_eval(
        self, script: str, key_count: int, *args: object
    ) -> tuple[object, asyncio.CancelledError | None]:
        task = asyncio.create_task(self._redis.eval(script, key_count, *args))
        try:
            return await asyncio.shield(task), None
        except asyncio.CancelledError as cancellation:
            try:
                result = await asyncio.shield(task)
            except BaseException as inner_error:  # noqa: BLE001 - preserve cancellation
                del inner_error
                raise cancellation from None
            return result, cancellation

    async def _rollback_registration(
        self,
        owner: str,
        fingerprints: Sequence[tuple[str, str]],
        policies: Sequence[Mapping[str, str]],
    ) -> None:
        async def cleanup() -> None:
            for claim_keys in reversed(fingerprints):
                try:
                    await self._redis.eval(
                        _ROLLBACK_FINGERPRINT_SCRIPT, 2, *claim_keys, owner
                    )
                except BaseException as cleanup_error:  # noqa: BLE001 - best effort rollback
                    del cleanup_error
            for keys in reversed(policies):
                try:
                    await self._redis.eval(
                        _ROLLBACK_SCOPE_SCRIPT,
                        7,
                        *self._policy_registration_keys(keys),
                        owner,
                    )
                except BaseException as cleanup_error:  # noqa: BLE001 - best effort rollback
                    del cleanup_error

        task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve primary outcome
                del cleanup_error

    def _clock_now(self) -> float:
        value = self._monotonic()
        if not math.isfinite(value):
            raise CapacityConfigurationError("capacity monotonic clock is invalid")
        return value

    def _require_initialized(self) -> None:
        if (
            not self._initialized
            or self._refresh_in_progress
            or self._clock_now() >= self._registration_valid_until
        ):
            self._initialized = False
            raise CapacityConfigurationError("model capacity pool is not initialized")

    async def acquire(
        self,
        candidates: Sequence[Deployment],
        wait_timeout: float,
        *,
        estimated_tokens: int,
    ) -> CapacityLease:
        ordered = tuple(candidates)
        if not ordered:
            raise ValueError("capacity candidates must not be empty")
        if not all(isinstance(candidate, Deployment) for candidate in ordered):
            raise TypeError("capacity candidates must contain only Deployment values")
        if (
            isinstance(wait_timeout, bool)
            or not isinstance(wait_timeout, int | float)
            or not math.isfinite(wait_timeout)
            or wait_timeout < 0
        ):
            raise ValueError("wait_timeout must be nonnegative and finite")
        if type(estimated_tokens) is not int or estimated_tokens <= 0:
            raise ValueError("estimated_tokens must be a strict positive integer")
        self._require_initialized()
        for candidate in ordered:
            if self._catalog_by_id.get(candidate.id) != candidate:
                raise CapacityConfigurationError("candidate is outside initialized catalog")
        async with self._waiter_lock:
            if self._waiters >= self._max_waiters:
                raise CapacityQueueFull("model capacity queue is full")
            self._waiters += 1
        try:
            deadline = asyncio.get_running_loop().time() + float(wait_timeout)
            while True:
                for deployment in self._ordered_candidates(ordered):
                    lease = await self._try_acquire(deployment, estimated_tokens)
                    if lease is not None:
                        return lease
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise CapacityWaitTimeout("model capacity queue timeout")
                await asyncio.sleep(min(self._poll_interval, remaining))
        finally:
            async with self._waiter_lock:
                self._waiters -= 1

    def _ordered_candidates(self, candidates: Sequence[Deployment]) -> tuple[Deployment, ...]:
        by_scope: dict[str, list[Deployment]] = {}
        for candidate in candidates:
            by_scope.setdefault(candidate.quota_scope_id, []).append(candidate)
        representatives = [self._weighted_choice(items) for items in by_scope.values()]
        return tuple(
            sorted(
                representatives,
                key=lambda item: (
                    self._observed_loads.get(item.quota_scope_id, 0)
                    / self._scope_policies[item.quota_scope_id].base_limit,
                    item.quota_scope_id,
                    item.id,
                ),
            )
        )

    def _weighted_choice(self, deployments: Sequence[Deployment]) -> Deployment:
        ordered = sorted(deployments, key=lambda item: item.id)
        total = 0
        for deployment in ordered:
            total += deployment.weight
            self._weights[deployment.id] = self._weights.get(deployment.id, 0) + deployment.weight
        selected = min(ordered, key=lambda item: (-self._weights[item.id], item.id))
        self._weights[selected.id] -= total
        return selected

    async def _try_acquire(
        self, deployment: Deployment, estimated_tokens: int
    ) -> CapacityLease | None:
        lease_id = str(uuid4())
        keys = self._keys(deployment.quota_scope_id)
        started = self._clock_now()
        try:
            result = await self._redis.eval(
                _ACQUIRE_SCRIPT,
                5,
                keys["leases"],
                keys["rpm"],
                keys["tpm"],
                keys["health"],
                keys["policy"],
                lease_id,
                self._lease_ms,
                estimated_tokens,
                _STATE_TTL_MS,
            )
        except asyncio.CancelledError:
            await self._release_id_ignoring_errors(keys["leases"], lease_id)
            raise
        except Exception:  # noqa: BLE001 - capacity must fail closed and redact Redis details
            raise CapacityBackendError("model capacity backend unavailable") from None
        try:
            acquired, active, _effective, final_value = self._integer_result(result, 4)
            self._observed_loads[deployment.quota_scope_id] = active
            if acquired == 0:
                if final_value not in {1, 2}:
                    raise CapacityBackendError(
                        "model capacity backend returned invalid state"
                    )
                return None
            if acquired != 1 or final_value <= 0:
                raise CapacityBackendError("model capacity backend returned invalid state")
        except CapacityBackendError:
            await self._release_id_ignoring_errors(keys["leases"], lease_id)
            raise
        try:
            renewal_delay = self._safe_renewal_delay(started)
        except CapacityConfigurationError:
            await self._release_id_ignoring_errors(keys["leases"], lease_id)
            raise
        return CapacityLease(
            id=lease_id,
            deployment_id=deployment.id,
            quota_scope_id=deployment.quota_scope_id,
            expires_at=datetime.fromtimestamp(final_value / 1000, tz=UTC),
            renew_after_seconds=renewal_delay,
        )

    async def release(self, lease: CapacityLease) -> bool:
        if not isinstance(lease, CapacityLease):
            raise TypeError("lease must be a CapacityLease")
        key = self._keys(lease.quota_scope_id)["leases"]
        try:
            result = await self._redis.eval(
                _RELEASE_SCRIPT, 1, key, lease.id, _STATE_TTL_MS
            )
            removed = self._strict_redis_int(result)
        except asyncio.CancelledError:
            await self._release_id_ignoring_errors(key, lease.id)
            raise
        except Exception:  # noqa: BLE001 - capacity must fail closed and redact Redis details
            raise CapacityBackendError("model capacity backend unavailable") from None
        if removed not in {0, 1}:
            raise CapacityBackendError("model capacity backend returned invalid state")
        if removed:
            self._observed_loads[lease.quota_scope_id] = max(
                0, self._observed_loads.get(lease.quota_scope_id, 1) - 1
            )
        return bool(removed)

    async def _release_id_ignoring_errors(self, key: str, lease_id: str) -> None:
        cleanup = asyncio.create_task(
            self._redis.eval(_RELEASE_SCRIPT, 1, key, lease_id, _STATE_TTL_MS)
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            try:
                await cleanup
            except Exception as cleanup_error:  # noqa: BLE001 - best effort cleanup
                del cleanup_error
        except Exception as cleanup_error:  # noqa: BLE001 - preserve primary outcome
            del cleanup_error

    async def renew(self, lease: CapacityLease) -> CapacityLease | None:
        if not isinstance(lease, CapacityLease):
            raise TypeError("lease must be a CapacityLease")
        deployment = self._catalog_by_id.get(lease.deployment_id)
        if deployment is None or deployment.quota_scope_id != lease.quota_scope_id:
            raise CapacityConfigurationError("lease is outside initialized catalog")
        # Fingerprint claims and leases occupy different Redis Cluster hash slots,
        # so they cannot share one Lua transaction. Forced claim refresh plus the
        # 3x metadata/lease TTL invariant fences natural expiry before ZADD.
        await self._ensure_registration(force=True)
        key = self._keys(lease.quota_scope_id)["leases"]
        started = self._clock_now()
        try:
            result = await self._redis.eval(
                _RENEW_SCRIPT, 1, key, lease.id, self._lease_ms, _STATE_TTL_MS
            )
            expires_ms = self._strict_redis_int(result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - capacity must fail closed and redact Redis details
            raise CapacityBackendError("model capacity backend unavailable") from None
        if expires_ms == 0:
            return None
        if expires_ms < 0:
            raise CapacityBackendError("model capacity backend returned invalid state")
        return CapacityLease(
            id=lease.id,
            deployment_id=lease.deployment_id,
            quota_scope_id=lease.quota_scope_id,
            expires_at=datetime.fromtimestamp(expires_ms / 1000, tz=UTC),
            renew_after_seconds=self._safe_renewal_delay(started),
        )

    def _safe_renewal_delay(self, started: float) -> float:
        elapsed = self._clock_now() - started
        if elapsed < 0:
            raise CapacityConfigurationError("capacity monotonic clock moved backwards")
        remaining = max(0.0, self._lease_ms / 1000 - elapsed)
        return remaining / 2

    async def record_outcome(
        self,
        quota_scope_id: str,
        *,
        status_code: int | None,
        latency_seconds: float,
        succeeded: bool,
    ) -> None:
        if quota_scope_id not in self._scope_policies:
            raise ValueError("unknown quota scope")
        if status_code is not None and (
            type(status_code) is not int or not 100 <= status_code <= 599
        ):
            raise ValueError("status_code must be None or between 100 and 599")
        if type(succeeded) is not bool:
            raise ValueError("succeeded must be a boolean")
        if (
            isinstance(latency_seconds, bool)
            or not isinstance(latency_seconds, int | float)
            or not math.isfinite(latency_seconds)
            or latency_seconds < 0
        ):
            raise ValueError("latency_seconds must be nonnegative and finite")
        keys = self._keys(quota_scope_id)
        try:
            result = await self._redis.eval(
                _RECORD_OUTCOME_SCRIPT,
                3,
                keys["health"],
                keys["latency"],
                keys["policy"],
                status_code or 0,
                int(succeeded),
                math.ceil(latency_seconds * 1000),
                self._cooldown_ms,
                self._latency_threshold_ms,
                self._health_window,
                _STATE_TTL_MS,
            )
            self._integer_result(result, 3)
        except asyncio.CancelledError:
            raise
        except CapacityBackendError:
            raise
        except Exception:  # noqa: BLE001 - capacity must fail closed and redact Redis details
            raise CapacityBackendError("model capacity backend unavailable") from None

    async def effective_limit(self, quota_scope_id: str) -> int:
        policy = self._scope_policies.get(quota_scope_id)
        if policy is None:
            raise ValueError("unknown quota scope")
        try:
            result = await self._redis.eval(
                _EFFECTIVE_LIMIT_SCRIPT,
                2,
                self._keys(quota_scope_id)["health"],
                self._keys(quota_scope_id)["policy"],
                _STATE_TTL_MS,
            )
            value = self._strict_redis_int(result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - capacity must fail closed and redact Redis details
            raise CapacityBackendError("model capacity backend unavailable") from None
        if value < 1:
            raise CapacityBackendError("model capacity backend returned invalid state")
        return value

    def _keys(self, quota_scope_id: str) -> Mapping[str, str]:
        digest = hashlib.sha256(quota_scope_id.encode("utf-8")).hexdigest()
        tag = "{" + digest + "}"
        root = f"{self._key_prefix}{tag}:"
        return {
            "leases": root + "leases",
            "rpm": root + "rpm",
            "tpm": root + "tpm",
            "health": root + "health",
            "latency": root + "latency",
            "policy": root + "policy",
            "policy_expires": root + "policy-expires",
            "policy_base": root + "policy-base",
            "policy_rpm": root + "policy-rpm",
            "policy_tpm": root + "policy-tpm",
            "policy_owners": root + "policy-owners",
        }

    @staticmethod
    def _scope_digest(quota_scope_id: str) -> str:
        return hashlib.sha256(quota_scope_id.encode("utf-8")).hexdigest()

    def _fingerprint_keys(self, fingerprint: str) -> tuple[str, str]:
        digest = hashlib.sha256(fingerprint.encode("ascii")).hexdigest()
        root = f"{self._key_prefix}credential:{{{digest}}}:"
        return root + "expires", root + "scopes"

    @staticmethod
    def _policy_registration_keys(keys: Mapping[str, str]) -> tuple[str, ...]:
        return (
            keys["policy"],
            keys["policy_expires"],
            keys["policy_base"],
            keys["policy_rpm"],
            keys["policy_tpm"],
            keys["policy_owners"],
            keys["health"],
        )

    @staticmethod
    def _strict_redis_int(value: object) -> int:
        if type(value) is int:
            return value
        if isinstance(value, bytes):
            try:
                text = value.decode("ascii")
            except UnicodeDecodeError:
                raise CapacityBackendError(
                    "model capacity backend returned invalid state"
                ) from None
        elif isinstance(value, str):
            text = value
        else:
            raise CapacityBackendError("model capacity backend returned invalid state")
        if re.fullmatch(r"(?:0|-?[1-9][0-9]*)", text) is None:
            raise CapacityBackendError("model capacity backend returned invalid state")
        return int(text)

    @staticmethod
    def _integer_result(result: object, length: int) -> tuple[int, ...]:
        if not isinstance(result, list | tuple) or len(result) != length:
            raise CapacityBackendError("model capacity backend returned invalid state")
        return tuple(CapacityPool._strict_redis_int(value) for value in result)


class _CapacityScopeView:
    """Scoped capacity controller that reuses a root pool's mutable runtime state."""

    def __init__(self, root: CapacityPool, deployments: Sequence[Deployment]) -> None:
        self._root = root
        self._catalog_by_id = {deployment.id: deployment for deployment in deployments}
        self._scope_policies = {
            quota_scope_id: root._scope_policies[quota_scope_id]
            for quota_scope_id in dict.fromkeys(
                deployment.quota_scope_id for deployment in deployments
            )
        }
        self._registration_state = root._registration_state_for(self._catalog_by_id)

    def validate_configuration(self, deployments: Iterable[Deployment]) -> None:
        for deployment in deployments:
            if not isinstance(deployment, Deployment):
                raise TypeError("deployments must contain only Deployment values")
            if self._catalog_by_id.get(deployment.id) != deployment:
                raise CapacityConfigurationError("deployment is outside capacity catalog")

    async def initialize(self) -> None:
        await self._ensure_registration(force=False)

    async def _ensure_registration(self, *, force: bool) -> None:
        root = self._root
        if not self._catalog_by_id:
            raise CapacityConfigurationError("model capacity catalog is empty")
        if root._credentials is None and root._fingerprint_resolver is None:
            raise CapacityConfigurationError("credential fingerprints are required")
        now = root._clock_now()
        state = self._registration_state
        if (
            not force
            and state.initialized
            and now < state.next_metadata_refresh
            and now < state.valid_until
        ):
            return
        async with root._registration_lock:
            now = root._clock_now()
            if (
                not force
                and state.initialized
                and now < state.next_metadata_refresh
                and now < state.valid_until
            ):
                return
            prior_registration_current = (
                state.initialized and now < state.valid_until
            )
            state.refresh_in_progress = True
            try:
                valid_until = await root._register_metadata(
                    catalog_by_id=self._catalog_by_id,
                    scope_policies=self._scope_policies,
                    prior_registration_current=prior_registration_current,
                )
            except BaseException:
                state.initialized = False
                if not prior_registration_current:
                    state.valid_until = 0.0
                raise
            else:
                state.initialized = True
                root._catalog_frozen = True
                state.valid_until = valid_until
                state.next_metadata_refresh = (
                    root._clock_now() + root._metadata_refresh_interval
                )
            finally:
                state.refresh_in_progress = False

    def _require_initialized(self) -> None:
        root = self._root
        state = self._registration_state
        if (
            not state.initialized
            or state.refresh_in_progress
            or root._clock_now() >= state.valid_until
        ):
            state.initialized = False
            raise CapacityConfigurationError("model capacity pool is not initialized")

    async def acquire(
        self,
        candidates: Sequence[Deployment],
        wait_timeout: float,
        *,
        estimated_tokens: int,
    ) -> CapacityLease:
        ordered = tuple(candidates)
        if not ordered:
            raise ValueError("capacity candidates must not be empty")
        if not all(isinstance(candidate, Deployment) for candidate in ordered):
            raise TypeError("capacity candidates must contain only Deployment values")
        if (
            isinstance(wait_timeout, bool)
            or not isinstance(wait_timeout, int | float)
            or not math.isfinite(wait_timeout)
            or wait_timeout < 0
        ):
            raise ValueError("wait_timeout must be nonnegative and finite")
        if type(estimated_tokens) is not int or estimated_tokens <= 0:
            raise ValueError("estimated_tokens must be a strict positive integer")
        self._require_initialized()
        for candidate in ordered:
            if self._catalog_by_id.get(candidate.id) != candidate:
                raise CapacityConfigurationError("candidate is outside initialized catalog")
        root = self._root
        async with root._waiter_lock:
            if root._waiters >= root._max_waiters:
                raise CapacityQueueFull("model capacity queue is full")
            root._waiters += 1
        try:
            deadline = asyncio.get_running_loop().time() + float(wait_timeout)
            while True:
                for deployment in root._ordered_candidates(ordered):
                    lease = await root._try_acquire(deployment, estimated_tokens)
                    if lease is not None:
                        return lease
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise CapacityWaitTimeout("model capacity queue timeout")
                await asyncio.sleep(min(root._poll_interval, remaining))
        finally:
            async with root._waiter_lock:
                root._waiters -= 1

    async def release(self, lease: CapacityLease) -> bool:
        return await self._root.release(lease)

    async def renew(self, lease: CapacityLease) -> CapacityLease | None:
        if not isinstance(lease, CapacityLease):
            raise TypeError("lease must be a CapacityLease")
        deployment = self._catalog_by_id.get(lease.deployment_id)
        if deployment is None or deployment.quota_scope_id != lease.quota_scope_id:
            raise CapacityConfigurationError("lease is outside initialized catalog")
        await self._ensure_registration(force=True)
        root = self._root
        key = root._keys(lease.quota_scope_id)["leases"]
        started = root._clock_now()
        try:
            result = await root._redis.eval(
                _RENEW_SCRIPT, 1, key, lease.id, root._lease_ms, _STATE_TTL_MS
            )
            expires_ms = root._strict_redis_int(result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - capacity must fail closed and redact Redis details
            raise CapacityBackendError("model capacity backend unavailable") from None
        if expires_ms == 0:
            return None
        if expires_ms < 0:
            raise CapacityBackendError("model capacity backend returned invalid state")
        return CapacityLease(
            id=lease.id,
            deployment_id=lease.deployment_id,
            quota_scope_id=lease.quota_scope_id,
            expires_at=datetime.fromtimestamp(expires_ms / 1000, tz=UTC),
            renew_after_seconds=root._safe_renewal_delay(started),
        )

    async def record_outcome(
        self,
        quota_scope_id: str,
        *,
        status_code: int | None,
        latency_seconds: float,
        succeeded: bool,
    ) -> None:
        if quota_scope_id not in self._scope_policies:
            raise ValueError("unknown quota scope")
        await self._root.record_outcome(
            quota_scope_id,
            status_code=status_code,
            latency_seconds=latency_seconds,
            succeeded=succeeded,
        )

    async def effective_limit(self, quota_scope_id: str) -> int:
        if quota_scope_id not in self._scope_policies:
            raise ValueError("unknown quota scope")
        return await self._root.effective_limit(quota_scope_id)
