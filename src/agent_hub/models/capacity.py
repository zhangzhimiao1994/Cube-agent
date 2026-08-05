"""Atomic, quota-scoped model concurrency, rate, and health controls."""

import asyncio
import hashlib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from types import MappingProxyType
from typing import Protocol, cast
from uuid import uuid4

from agent_hub.models.types import Deployment

_SAFE_PREFIX = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_SAFE_FINGERPRINT = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_STATE_TTL_MS = 3_600_000


class CapacityUnavailable(asyncio.TimeoutError):
    """A bounded model-capacity wait could not obtain a lease."""


class CapacityBackendError(RuntimeError):
    """Stable fail-closed error for unavailable or malformed capacity state."""


class RedisCapacityClient(Protocol):
    async def eval(self, script: str, key_count: int, *args: object) -> object: ...


@dataclass(frozen=True, slots=True)
class CapacityLease:
    id: str
    deployment_id: str
    quota_scope_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("lease expiry must be timezone-aware")


@dataclass(frozen=True, slots=True, repr=False)
class CredentialDescriptor:
    secret_ref: str = field(repr=False)
    fingerprint: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.secret_ref or self.secret_ref != self.secret_ref.strip():
            raise ValueError("secret_ref must be nonblank and unpadded")
        if _SAFE_FINGERPRINT.fullmatch(self.fingerprint) is None:
            raise ValueError("credential fingerprint must be bounded safe metadata")

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
_EFFECTIVE_LIMIT_SCRIPT = """
local value = tonumber(redis.call('HGET', KEYS[1], 'effective')) or tonumber(ARGV[1])
value = math.max(1, math.min(value, tonumber(ARGV[1])))
redis.call('HSET', KEYS[1], 'effective', value)
redis.call('PEXPIRE', KEYS[1], ARGV[2])
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
        key_prefix: str = "agent-hub:model-capacity:",
        lease_seconds: float = 30,
        poll_interval: float = 0.05,
        max_waiters: int = 1024,
        cooldown_seconds: float = 30,
        latency_threshold_seconds: float = 30,
        health_window: int = 20,
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
        self._redis = cast(RedisCapacityClient, redis_client)
        self._credentials = credentials
        self._key_prefix = key_prefix
        self._lease_ms = math.ceil(lease_seconds * 1000)
        self._poll_interval = float(poll_interval)
        self._max_waiters = max_waiters
        self._cooldown_ms = math.ceil(cooldown_seconds * 1000)
        self._latency_threshold_ms = math.ceil(latency_threshold_seconds * 1000)
        self._health_window = health_window
        self._waiters = 0
        self._waiter_lock = asyncio.Lock()
        self._scope_policies: dict[str, _ScopePolicy] = {}
        self._fingerprint_scopes: dict[str, str] = {}
        self._observed_loads: dict[str, int] = {}
        self._weights: dict[str, int] = {}
        configured = tuple(deployments)
        self.validate_configuration(configured)

    def validate_configuration(self, deployments: Iterable[Deployment]) -> None:
        for deployment in deployments:
            if not isinstance(deployment, Deployment):
                raise TypeError("deployments must contain only Deployment values")
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

            fingerprint = None
            if self._credentials is not None:
                fingerprint = self._credentials.fingerprint_for(deployment.secret_ref)
            if fingerprint is None:
                fingerprint = hashlib.sha256(deployment.secret_ref.encode("utf-8")).hexdigest()
            previous_scope = self._fingerprint_scopes.get(fingerprint)
            if previous_scope is not None and previous_scope != deployment.quota_scope_id:
                raise ValueError("credential fingerprint is assigned to conflicting quota scopes")
            self._fingerprint_scopes[fingerprint] = deployment.quota_scope_id

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
        self.validate_configuration(ordered)
        async with self._waiter_lock:
            if self._waiters >= self._max_waiters:
                raise CapacityUnavailable("model capacity queue is full")
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
                    raise CapacityUnavailable("model capacity queue timeout")
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
        policy = self._scope_policies[deployment.quota_scope_id]
        lease_id = str(uuid4())
        keys = self._keys(deployment.quota_scope_id)
        try:
            result = await self._redis.eval(
                _ACQUIRE_SCRIPT,
                4,
                keys["leases"],
                keys["rpm"],
                keys["tpm"],
                keys["health"],
                lease_id,
                self._lease_ms,
                policy.base_limit,
                policy.rpm or 0,
                policy.tpm or 0,
                estimated_tokens,
                _STATE_TTL_MS,
            )
        except asyncio.CancelledError:
            await self._release_id_ignoring_errors(keys["leases"], lease_id)
            raise
        except Exception:  # noqa: BLE001 - capacity must fail closed and redact Redis details
            raise CapacityBackendError("model capacity backend unavailable") from None
        values = self._integer_result(result, 4)
        acquired, active, _effective, final_value = values
        self._observed_loads[deployment.quota_scope_id] = active
        if acquired == 0:
            if final_value not in {1, 2}:
                raise CapacityBackendError("model capacity backend returned invalid state")
            return None
        if acquired != 1 or final_value <= 0:
            raise CapacityBackendError("model capacity backend returned invalid state")
        return CapacityLease(
            id=lease_id,
            deployment_id=deployment.id,
            quota_scope_id=deployment.quota_scope_id,
            expires_at=datetime.fromtimestamp(final_value / 1000, tz=UTC),
        )

    async def release(self, lease: CapacityLease) -> bool:
        if not isinstance(lease, CapacityLease):
            raise TypeError("lease must be a CapacityLease")
        key = self._keys(lease.quota_scope_id)["leases"]
        try:
            result = await self._redis.eval(
                _RELEASE_SCRIPT, 1, key, lease.id, _STATE_TTL_MS
            )
            removed = int(cast(int | bytes | str, result))
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
        key = self._keys(lease.quota_scope_id)["leases"]
        try:
            result = await self._redis.eval(
                _RENEW_SCRIPT, 1, key, lease.id, self._lease_ms, _STATE_TTL_MS
            )
            expires_ms = int(cast(int | bytes | str, result))
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
        )

    async def record_outcome(
        self,
        quota_scope_id: str,
        *,
        status_code: int | None,
        latency_seconds: float,
    ) -> None:
        policy = self._scope_policies.get(quota_scope_id)
        if policy is None:
            raise ValueError("unknown quota scope")
        if status_code is not None and (
            type(status_code) is not int or not 100 <= status_code <= 599
        ):
            raise ValueError("status_code must be None or between 100 and 599")
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
                2,
                keys["health"],
                keys["latency"],
                policy.base_limit,
                status_code or 0,
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
                1,
                self._keys(quota_scope_id)["health"],
                policy.base_limit,
                _STATE_TTL_MS,
            )
            value = int(cast(int | bytes | str, result))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - capacity must fail closed and redact Redis details
            raise CapacityBackendError("model capacity backend unavailable") from None
        if not 1 <= value <= policy.base_limit:
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
        }

    @staticmethod
    def _integer_result(result: object, length: int) -> tuple[int, ...]:
        if not isinstance(result, list | tuple) or len(result) != length:
            raise CapacityBackendError("model capacity backend returned invalid state")
        try:
            return tuple(int(cast(int | bytes | str, value)) for value in result)
        except (TypeError, ValueError):
            raise CapacityBackendError("model capacity backend returned invalid state") from None
