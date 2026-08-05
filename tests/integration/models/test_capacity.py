import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityPool,
    CapacityUnavailable,
    CredentialDescriptor,
    CredentialRegistry,
    safe_operational_limit,
)
from agent_hub.models.types import Deployment


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    try:
        await client.ping()
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def prefix() -> str:
    return f"agent-hub:test:capacity:{uuid4()}:"


def deployment(identifier: str, scope: str, **overrides: object) -> Deployment:
    values: dict[str, object] = {
        "id": identifier,
        "logical_model": "chat",
        "secret_ref": f"secret://{identifier}",
        "quota_scope_id": scope,
        "max_concurrency": 1,
        "target_utilization": 0.8,
    }
    values.update(overrides)
    return Deployment(**values)  # type: ignore[arg-type]


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def credentials(*identifiers: str) -> CredentialRegistry:
    return CredentialRegistry(
        CredentialDescriptor(f"secret://{identifier}", fingerprint(identifier))
        for identifier in identifiers
    )


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RegistrationRedis:
    def __init__(self, client: Redis) -> None:
        self.client = client
        self.calls = 0
        self.fail_next = False
        self.block_next = False
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()

    async def eval(self, script: str, key_count: int, *args: object) -> object:
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("redis://private-backend")
        if self.block_next:
            self.block_next = False
            self.started.set()
            await self.proceed.wait()
        redis_eval = cast(Callable[..., Awaitable[object]], self.client.eval)
        return await redis_eval(script, key_count, *args)


async def redis_key(client: Redis, pattern: str, *, member: str | None = None) -> bytes:
    async for key in client.scan_iter(match=pattern):
        if not isinstance(key, bytes):
            raise TypeError("test Redis keys must be bytes")
        if member is None or await client.zscore(key, member) is not None:
            return key
    raise AssertionError("expected Redis key was not found")


@pytest.mark.integration
async def test_serial_scope_times_out_and_release_recovers(
    redis_client: Redis, prefix: str
) -> None:
    serial = deployment("serial", "account")
    pool = CapacityPool(
        redis_client,
        deployments=[serial],
        credentials=credentials("serial"),
        key_prefix=prefix,
        poll_interval=0.005,
    )
    await pool.initialize()

    first = await pool.acquire([serial], wait_timeout=0.05, estimated_tokens=1)
    with pytest.raises(CapacityUnavailable, match="capacity queue timeout"):
        await pool.acquire([serial], wait_timeout=0.025, estimated_tokens=1)
    assert await pool.release(first) is True
    assert await pool.release(first) is False
    second = await pool.acquire([serial], wait_timeout=0.05, estimated_tokens=1)
    await pool.release(second)


@pytest.mark.integration
async def test_independent_scopes_add_slots_but_same_scope_keys_do_not(
    redis_client: Redis, prefix: str
) -> None:
    account_a = deployment("key-a", "account-a")
    account_b = deployment("key-b", "account-b")
    shared_a = deployment("shared-a", "shared")
    shared_b = deployment("shared-b", "shared")
    pool = CapacityPool(
        redis_client,
        deployments=[account_a, account_b, shared_a, shared_b],
        credentials=credentials("key-a", "key-b", "shared-a", "shared-b"),
        key_prefix=prefix,
        poll_interval=0.005,
    )
    await pool.initialize()
    first, second = await asyncio.gather(
        pool.acquire([account_a, account_b], wait_timeout=0.1, estimated_tokens=1),
        pool.acquire([account_a, account_b], wait_timeout=0.1, estimated_tokens=1),
    )
    assert {first.quota_scope_id, second.quota_scope_id} == {"account-a", "account-b"}
    await asyncio.gather(pool.release(first), pool.release(second))

    held = await pool.acquire([shared_a, shared_b], wait_timeout=0.05, estimated_tokens=1)
    with pytest.raises(CapacityUnavailable):
        await pool.acquire([shared_a, shared_b], wait_timeout=0.02, estimated_tokens=1)
    await pool.release(held)


def test_safe_operational_limit_keeps_headroom_and_one_low_capacity_slot() -> None:
    assert safe_operational_limit(10, 0.8, 1) == 8
    assert safe_operational_limit(10, 0.9, 3) == 7
    assert safe_operational_limit(1, 0.5, 0) == 1


@pytest.mark.integration
async def test_conflicting_scope_policy_uses_most_restrictive_limit(
    redis_client: Redis, prefix: str
) -> None:
    strict = deployment("strict", "shared", max_concurrency=2, target_utilization=0.5)
    permissive = deployment("wide", "shared", max_concurrency=8, target_utilization=0.9)
    pool = CapacityPool(
        redis_client,
        deployments=[permissive, strict],
        credentials=credentials("wide", "strict"),
        key_prefix=prefix,
    )
    await pool.initialize()

    held = await pool.acquire([permissive], wait_timeout=0.05, estimated_tokens=1)
    with pytest.raises(CapacityUnavailable):
        await pool.acquire([permissive], wait_timeout=0.02, estimated_tokens=1)
    await pool.release(held)


@pytest.mark.integration
async def test_distributed_scope_policy_converges_downward_across_pool_instances(
    redis_client: Redis, redis_url: str, prefix: str
) -> None:
    other_client = Redis.from_url(redis_url)
    permissive = deployment(
        "permissive", "distributed", max_concurrency=10, reserved_slots=3
    )
    strict = deployment("strict", "distributed", max_concurrency=2, target_utilization=0.5)
    permissive_pool = CapacityPool(
        redis_client,
        deployments=[permissive],
        credentials=credentials("permissive"),
        key_prefix=prefix,
        poll_interval=0.002,
    )
    strict_pool = CapacityPool(
        other_client,
        deployments=[strict],
        credentials=credentials("strict"),
        key_prefix=prefix,
        poll_interval=0.002,
    )
    try:
        await permissive_pool.initialize()
        live = [
            await permissive_pool.acquire(
                [permissive], wait_timeout=0.05, estimated_tokens=1
            )
            for _ in range(3)
        ]
        await strict_pool.initialize()

        await permissive_pool.release(live.pop())
        with pytest.raises(CapacityUnavailable):
            await permissive_pool.acquire(
                [permissive], wait_timeout=0.015, estimated_tokens=1
            )
        await asyncio.gather(*(permissive_pool.release(item) for item in live))
        one = await permissive_pool.acquire(
            [permissive], wait_timeout=0.05, estimated_tokens=1
        )
        with pytest.raises(CapacityUnavailable):
            await strict_pool.acquire([strict], wait_timeout=0.015, estimated_tokens=1)
        await permissive_pool.release(one)
        assert await permissive_pool.effective_limit("distributed") == 1

        reverse_prefix = f"{prefix}reverse:"
        reverse_strict = CapacityPool(
            redis_client,
            deployments=[strict],
            credentials=credentials("strict"),
            key_prefix=reverse_prefix,
            poll_interval=0.002,
        )
        reverse_permissive = CapacityPool(
            other_client,
            deployments=[permissive],
            credentials=credentials("permissive"),
            key_prefix=reverse_prefix,
            poll_interval=0.002,
        )
        await reverse_strict.initialize()
        await reverse_permissive.initialize()
        reverse_one = await reverse_permissive.acquire(
            [permissive], wait_timeout=0.05, estimated_tokens=1
        )
        with pytest.raises(CapacityUnavailable):
            await reverse_permissive.acquire(
                [permissive], wait_timeout=0.015, estimated_tokens=1
            )
        await reverse_permissive.release(reverse_one)
    finally:
        await other_client.aclose()


@pytest.mark.integration
async def test_expiry_and_exact_renew_semantics(redis_client: Redis, prefix: str) -> None:
    serial = deployment("serial", "expiry")
    pool = CapacityPool(
        redis_client,
        deployments=[serial],
        credentials=credentials("serial"),
        key_prefix=prefix,
        lease_seconds=0.08,
        poll_interval=0.005,
    )
    await pool.initialize()
    expired = await pool.acquire([serial], wait_timeout=0.05, estimated_tokens=1)
    await asyncio.sleep(0.1)
    replacement = await pool.acquire([serial], wait_timeout=0.05, estimated_tokens=1)

    assert await pool.renew(expired) is None
    assert await pool.release(expired) is False
    renewed = await pool.renew(replacement)
    assert renewed is not None and renewed.expires_at > replacement.expires_at
    assert await pool.release(replacement) is True
    assert await pool.renew(replacement) is None


@pytest.mark.integration
async def test_renew_fails_without_extending_after_fingerprint_takeover(
    redis_client: Redis, redis_url: str, prefix: str
) -> None:
    other_client = Redis.from_url(redis_url)
    first = deployment("renew-a", "renew-scope-a")
    second = deployment("renew-b", "renew-scope-b")
    shared = fingerprint("renew-shared-credential")
    first_pool = CapacityPool(
        redis_client,
        deployments=[first],
        credentials=CredentialRegistry([CredentialDescriptor(first.secret_ref, shared)]),
        key_prefix=prefix,
        lease_seconds=0.5,
        metadata_ttl_seconds=2,
        metadata_refresh_interval=0.5,
    )
    second_pool = CapacityPool(
        other_client,
        deployments=[second],
        credentials=CredentialRegistry([CredentialDescriptor(second.secret_ref, shared)]),
        key_prefix=prefix,
        lease_seconds=0.5,
        metadata_ttl_seconds=2,
        metadata_refresh_interval=0.5,
    )
    try:
        await first_pool.initialize()
        first_lease = await first_pool.acquire(
            [first], wait_timeout=0.05, estimated_tokens=1
        )
        first_lease_key = await redis_key(
            redis_client, f"{prefix}*:leases", member=first_lease.id
        )
        old_score = await redis_client.zscore(first_lease_key, first_lease.id)
        claim_key = await redis_key(redis_client, f"{prefix}credential:*:scope")
        await redis_client.delete(claim_key)
        await second_pool.initialize()
        second_lease = await second_pool.acquire(
            [second], wait_timeout=0.05, estimated_tokens=1
        )

        with pytest.raises(CapacityConfigurationError, match="quota scope conflicts"):
            await first_pool.renew(first_lease)
        assert await redis_client.zscore(first_lease_key, first_lease.id) == old_score
        assert await first_pool.release(first_lease) is True
        assert await redis_client.zcard(first_lease_key) == 0
        second_lease_key = await redis_key(
            redis_client, f"{prefix}*:leases", member=second_lease.id
        )
        assert await redis_client.zcard(second_lease_key) == 1
        await second_pool.release(second_lease)
    finally:
        await other_client.aclose()


@pytest.mark.integration
async def test_successful_renew_refreshes_fingerprint_claim(
    redis_client: Redis, prefix: str
) -> None:
    serial = deployment("renew-normal", "renew-normal-scope")
    pool = CapacityPool(
        redis_client,
        deployments=[serial],
        credentials=credentials("renew-normal"),
        key_prefix=prefix,
        lease_seconds=0.5,
        metadata_ttl_seconds=2,
        metadata_refresh_interval=0.5,
    )
    await pool.initialize()
    active = await pool.acquire([serial], wait_timeout=0.05, estimated_tokens=1)
    claim_key = await redis_key(redis_client, f"{prefix}credential:*:scope")
    await redis_client.pexpire(claim_key, 800)
    before = await redis_client.pttl(claim_key)

    renewed = await pool.renew(active)

    assert renewed is not None
    assert await redis_client.pttl(claim_key) > before + 500
    await pool.release(active)


def test_metadata_ttl_must_cover_three_lease_horizons() -> None:
    with pytest.raises(ValueError, match="metadata_ttl_seconds"):
        CapacityPool(
            object(),
            lease_seconds=1,
            metadata_ttl_seconds=2.9,
            metadata_refresh_interval=0.5,
        )


@pytest.mark.integration
async def test_rpm_tpm_are_scope_shared_and_full_capacity_spends_nothing(
    redis_client: Redis, prefix: str
) -> None:
    limited = deployment("limited", "rate", rpm=2, tpm=10)
    pool = CapacityPool(
        redis_client,
        deployments=[limited],
        credentials=credentials("limited"),
        key_prefix=prefix,
        poll_interval=0.005,
    )
    await pool.initialize()
    held = await pool.acquire([limited], wait_timeout=0.05, estimated_tokens=4)
    with pytest.raises(CapacityUnavailable):
        await pool.acquire([limited], wait_timeout=0.015, estimated_tokens=6)
    await pool.release(held)

    # The failed full-concurrency attempt did not spend its six tokens.
    second = await pool.acquire([limited], wait_timeout=0.05, estimated_tokens=6)
    await pool.release(second)
    with pytest.raises(CapacityUnavailable):
        await pool.acquire([limited], wait_timeout=0.015, estimated_tokens=1)


@pytest.mark.integration
async def test_distributed_rpm_tpm_policy_is_shared_across_two_credentials(
    redis_client: Redis, redis_url: str, prefix: str
) -> None:
    other_client = Redis.from_url(redis_url)
    unlimited = deployment("rate-a", "shared-rate")
    limited = deployment("rate-b", "shared-rate", rpm=1, tpm=5)
    unlimited_pool = CapacityPool(
        redis_client,
        deployments=[unlimited],
        credentials=credentials("rate-a"),
        key_prefix=prefix,
        poll_interval=0.002,
    )
    limited_pool = CapacityPool(
        other_client,
        deployments=[limited],
        credentials=credentials("rate-b"),
        key_prefix=prefix,
        poll_interval=0.002,
    )
    try:
        await unlimited_pool.initialize()
        await limited_pool.initialize()
        first = await unlimited_pool.acquire(
            [unlimited], wait_timeout=0.05, estimated_tokens=4
        )
        await unlimited_pool.release(first)
        with pytest.raises(CapacityUnavailable):
            await limited_pool.acquire([limited], wait_timeout=0.015, estimated_tokens=2)

        policy_keys = [
            key async for key in redis_client.scan_iter(match=f"{prefix}*:policy")
        ]
        assert len(policy_keys) == 1
        policy = await cast(
            Awaitable[dict[bytes, bytes]], redis_client.hgetall(policy_keys[0])
        )
        assert policy[b"rpm"] == b"1"
        assert policy[b"tpm"] == b"5"
    finally:
        await other_client.aclose()


@pytest.mark.integration
async def test_capacity_state_keys_have_bounded_ttls(redis_client: Redis, prefix: str) -> None:
    limited = deployment(
        "ttl-limited", "ttl-scope", max_concurrency=4, rpm=10, tpm=100
    )
    pool = CapacityPool(
        redis_client,
        deployments=[limited],
        credentials=credentials("ttl-limited"),
        key_prefix=prefix,
        cooldown_seconds=0.02,
    )
    await pool.initialize()
    held = await pool.acquire([limited], wait_timeout=0.05, estimated_tokens=3)
    await pool.record_outcome(
        "ttl-scope", status_code=429, latency_seconds=0.001, succeeded=False
    )

    keys = [key async for key in redis_client.scan_iter(match=f"{prefix}*")]
    decoded = {key.decode("ascii") for key in keys}
    for suffix in (":leases", ":rpm", ":tpm", ":policy", ":health", ":latency", ":scope"):
        assert any(key.endswith(suffix) for key in decoded)
    ttls = [await redis_client.ttl(key) for key in keys]
    assert all(0 < ttl <= 3600 for ttl in ttls)
    await pool.release(held)


@pytest.mark.integration
async def test_repeat_initialize_refreshes_active_metadata_ttls(
    redis_client: Redis, prefix: str
) -> None:
    serial = deployment("refresh-active", "refresh-active-scope")
    clock = ManualClock()
    pool = CapacityPool(
        redis_client,
        deployments=[serial],
        credentials=credentials("refresh-active"),
        key_prefix=prefix,
        lease_seconds=0.05,
        metadata_ttl_seconds=0.2,
        metadata_refresh_interval=0.05,
        monotonic=clock,
    )
    await pool.initialize()
    metadata_keys = [
        key
        async for key in redis_client.scan_iter(match=f"{prefix}*")
        if key.decode("ascii").endswith((":policy", ":scope"))
    ]
    assert len(metadata_keys) == 2
    await asyncio.sleep(0.08)
    decayed = [await redis_client.pttl(key) for key in metadata_keys]
    clock.advance(0.06)

    await pool.initialize()

    refreshed = [await redis_client.pttl(key) for key in metadata_keys]
    assert all(after > before + 40 for before, after in zip(decayed, refreshed, strict=True))


@pytest.mark.integration
async def test_initialize_reconstructs_expired_metadata_before_acquire(
    redis_client: Redis, prefix: str
) -> None:
    serial = deployment("refresh-expired", "refresh-expired-scope")
    clock = ManualClock()
    pool = CapacityPool(
        redis_client,
        deployments=[serial],
        credentials=credentials("refresh-expired"),
        key_prefix=prefix,
        lease_seconds=0.02,
        metadata_ttl_seconds=0.08,
        metadata_refresh_interval=0.02,
        monotonic=clock,
    )
    await pool.initialize()
    await asyncio.sleep(0.1)
    assert not [key async for key in redis_client.scan_iter(match=f"{prefix}*")]
    clock.advance(0.03)

    await pool.initialize()
    recovered = await pool.acquire([serial], wait_timeout=0.05, estimated_tokens=1)

    assert recovered.quota_scope_id == "refresh-expired-scope"
    await pool.release(recovered)


@pytest.mark.integration
async def test_expired_claim_conflict_blocks_original_worker(
    redis_client: Redis, redis_url: str, prefix: str
) -> None:
    other_client = Redis.from_url(redis_url)
    first = deployment("expired-a", "expired-scope-a")
    second = deployment("expired-b", "expired-scope-b")
    shared = fingerprint("expired-shared-credential")
    first_clock = ManualClock()
    second_clock = ManualClock()
    first_pool = CapacityPool(
        redis_client,
        deployments=[first],
        credentials=CredentialRegistry([CredentialDescriptor(first.secret_ref, shared)]),
        key_prefix=prefix,
        lease_seconds=0.02,
        metadata_ttl_seconds=0.08,
        metadata_refresh_interval=0.02,
        monotonic=first_clock,
    )
    second_pool = CapacityPool(
        other_client,
        deployments=[second],
        credentials=CredentialRegistry([CredentialDescriptor(second.secret_ref, shared)]),
        key_prefix=prefix,
        lease_seconds=0.05,
        metadata_ttl_seconds=0.2,
        metadata_refresh_interval=0.05,
        monotonic=second_clock,
    )
    try:
        await first_pool.initialize()
        await asyncio.sleep(0.1)
        await second_pool.initialize()
        first_clock.advance(0.03)

        with pytest.raises(CapacityConfigurationError, match="quota scope conflicts"):
            await first_pool.initialize()
        with pytest.raises(CapacityConfigurationError, match="not initialized"):
            await first_pool.acquire([first], wait_timeout=0.01, estimated_tokens=1)
        second_lease = await second_pool.acquire(
            [second], wait_timeout=0.05, estimated_tokens=1
        )
        await second_pool.release(second_lease)
    finally:
        await other_client.aclose()


@pytest.mark.integration
async def test_concurrent_due_initialize_performs_one_registration_sequence(
    redis_client: Redis, prefix: str
) -> None:
    serial = deployment("refresh-concurrent", "refresh-concurrent-scope")
    clock = ManualClock()
    registration_redis = RegistrationRedis(redis_client)
    pool = CapacityPool(
        registration_redis,
        deployments=[serial],
        credentials=credentials("refresh-concurrent"),
        key_prefix=prefix,
        lease_seconds=0.08,
        metadata_ttl_seconds=0.3,
        metadata_refresh_interval=0.05,
        monotonic=clock,
    )
    await pool.initialize()
    assert registration_redis.calls == 2
    registration_redis.calls = 0
    clock.advance(0.06)

    await asyncio.gather(*(pool.initialize() for _ in range(20)))

    assert registration_redis.calls == 2
    lease = await pool.acquire([serial], wait_timeout=0.05, estimated_tokens=1)
    await pool.release(lease)


@pytest.mark.integration
async def test_failed_or_cancelled_initialize_is_retryable(
    redis_client: Redis, prefix: str
) -> None:
    serial = deployment("refresh-retry", "refresh-retry-scope")
    registration_redis = RegistrationRedis(redis_client)
    pool = CapacityPool(
        registration_redis,
        deployments=[serial],
        credentials=credentials("refresh-retry"),
        key_prefix=prefix,
        lease_seconds=0.08,
        metadata_ttl_seconds=0.3,
        metadata_refresh_interval=0.05,
    )
    registration_redis.fail_next = True
    with pytest.raises(CapacityBackendError) as captured:
        await pool.initialize()
    assert "private-backend" not in str(captured.value)
    with pytest.raises(CapacityConfigurationError, match="not initialized"):
        await pool.acquire([serial], wait_timeout=0.01, estimated_tokens=1)

    registration_redis.block_next = True
    cancelled = asyncio.create_task(pool.initialize())
    await registration_redis.started.wait()
    cancelled.cancel("registration cancelled")
    with pytest.raises(asyncio.CancelledError) as captured_cancelled:
        await cancelled
    assert captured_cancelled.value.args == ("registration cancelled",)
    with pytest.raises(CapacityConfigurationError, match="not initialized"):
        await pool.acquire([serial], wait_timeout=0.01, estimated_tokens=1)

    await pool.initialize()
    lease = await pool.acquire([serial], wait_timeout=0.05, estimated_tokens=1)
    await pool.release(lease)


@pytest.mark.integration
async def test_atomic_stress_across_two_pool_instances_never_oversubscribes(
    redis_client: Redis, redis_url: str, prefix: str
) -> None:
    other_client = Redis.from_url(redis_url)
    serial = deployment("serial", "atomic")
    first_pool = CapacityPool(
        redis_client,
        deployments=[serial],
        credentials=credentials("serial"),
        key_prefix=prefix,
        poll_interval=0.001,
    )
    second_pool = CapacityPool(
        other_client,
        deployments=[serial],
        credentials=credentials("serial"),
        key_prefix=prefix,
        poll_interval=0.001,
    )
    active = 0
    maximum = 0
    lock = asyncio.Lock()

    async def worker(pool: CapacityPool) -> None:
        nonlocal active, maximum
        lease = await pool.acquire([serial], wait_timeout=1, estimated_tokens=1)
        async with lock:
            active += 1
            maximum = max(maximum, active)
        await asyncio.sleep(0.003)
        async with lock:
            active -= 1
        await pool.release(lease)

    try:
        await first_pool.initialize()
        await second_pool.initialize()
        await asyncio.gather(*(worker(first_pool if i % 2 else second_pool) for i in range(30)))
    finally:
        await other_client.aclose()
    assert maximum == 1


@pytest.mark.integration
async def test_health_reduces_scope_and_restores_gradually_after_cooldown(
    redis_client: Redis, prefix: str
) -> None:
    wide = deployment("wide", "health", max_concurrency=5, target_utilization=0.8)
    pool = CapacityPool(
        redis_client,
        deployments=[wide],
        credentials=credentials("wide"),
        key_prefix=prefix,
        cooldown_seconds=0.04,
        latency_threshold_seconds=0.2,
    )
    await pool.initialize()
    assert await pool.effective_limit("health") == 4
    await pool.record_outcome(
        "health", status_code=429, latency_seconds=0.01, succeeded=False
    )
    assert await pool.effective_limit("health") == 2
    await pool.record_outcome(
        "health", status_code=200, latency_seconds=0.01, succeeded=True
    )
    assert await pool.effective_limit("health") == 2
    await asyncio.sleep(0.05)
    await pool.record_outcome(
        "health", status_code=200, latency_seconds=0.01, succeeded=True
    )
    assert await pool.effective_limit("health") == 3
    await pool.record_outcome(
        "health", status_code=200, latency_seconds=0.01, succeeded=True
    )
    assert await pool.effective_limit("health") == 4
    await pool.record_outcome(
        "health", status_code=200, latency_seconds=0.01, succeeded=True
    )
    assert await pool.effective_limit("health") == 4


@pytest.mark.integration
async def test_health_restores_only_after_explicit_success(
    redis_client: Redis, prefix: str
) -> None:
    wide = deployment("wide-explicit", "explicit-health", max_concurrency=5)
    pool = CapacityPool(
        redis_client,
        deployments=[wide],
        credentials=credentials("wide-explicit"),
        key_prefix=prefix,
        cooldown_seconds=0.02,
        latency_threshold_seconds=1,
    )
    await pool.initialize()
    await pool.record_outcome(
        "explicit-health", status_code=429, latency_seconds=0.001, succeeded=False
    )
    assert await pool.effective_limit("explicit-health") == 2
    await asyncio.sleep(0.03)

    await pool.record_outcome(
        "explicit-health", status_code=500, latency_seconds=0.001, succeeded=False
    )
    assert await pool.effective_limit("explicit-health") == 2
    await pool.record_outcome(
        "explicit-health", status_code=None, latency_seconds=0.001, succeeded=False
    )
    assert await pool.effective_limit("explicit-health") == 2
    await pool.record_outcome(
        "explicit-health", status_code=200, latency_seconds=0.001, succeeded=True
    )
    assert await pool.effective_limit("explicit-health") == 3


@pytest.mark.integration
async def test_p95_and_503_share_health_and_keys_have_ttls(
    redis_client: Redis, prefix: str
) -> None:
    wide = deployment("wide", "shared-health", max_concurrency=5, target_utilization=0.8)
    pool = CapacityPool(
        redis_client,
        deployments=[wide],
        credentials=credentials("wide"),
        key_prefix=prefix,
        cooldown_seconds=0.03,
        latency_threshold_seconds=0.02,
        health_window=4,
    )
    await pool.initialize()
    await pool.record_outcome(
        "shared-health", status_code=200, latency_seconds=0.1, succeeded=True
    )
    assert await pool.effective_limit("shared-health") == 2
    await pool.record_outcome(
        "shared-health", status_code=503, latency_seconds=0.001, succeeded=False
    )
    assert await pool.effective_limit("shared-health") == 1
    keys = [key async for key in redis_client.scan_iter(match=f"{prefix}*")]
    assert keys
    ttls = [await redis_client.ttl(key) for key in keys]
    assert all(0 < ttl <= 3600 for ttl in ttls)


@pytest.mark.integration
async def test_duplicate_fingerprint_cannot_claim_independent_scopes(
    redis_client: Redis, prefix: str
) -> None:
    duplicate_credentials = CredentialRegistry(
        [
            CredentialDescriptor("secret://one", fingerprint("same-fingerprint")),
            CredentialDescriptor("secret://two", fingerprint("same-fingerprint")),
        ]
    )
    pool = CapacityPool(
            redis_client,
            deployments=[deployment("one", "scope-a"), deployment("two", "scope-b")],
            credentials=duplicate_credentials,
            key_prefix=prefix,
    )
    with pytest.raises(CapacityConfigurationError, match="fingerprint quota scope"):
        await pool.initialize()


def test_credential_fingerprint_must_be_irreversible_digest() -> None:
    with pytest.raises(ValueError, match="credential fingerprint"):
        CredentialDescriptor("secret://one", "same-fingerprint")


@pytest.mark.integration
async def test_missing_fingerprint_fails_before_lease(
    redis_client: Redis, prefix: str
) -> None:
    serial = deployment("missing-fingerprint", "missing")
    pool = CapacityPool(
        redis_client,
        deployments=[serial],
        credentials=CredentialRegistry([]),
        key_prefix=prefix,
    )
    with pytest.raises(CapacityConfigurationError, match="fingerprint metadata"):
        await pool.initialize()
    with pytest.raises(CapacityConfigurationError, match="not initialized"):
        await pool.acquire([serial], wait_timeout=0.01, estimated_tokens=1)


@pytest.mark.integration
async def test_fingerprint_scope_claim_conflicts_across_workers(
    redis_client: Redis, redis_url: str, prefix: str
) -> None:
    other_client = Redis.from_url(redis_url)
    first = deployment("claim-a", "claim-scope-a")
    second = deployment("claim-b", "claim-scope-b")
    shared_fingerprint = fingerprint("same-credential")
    first_pool = CapacityPool(
        redis_client,
        deployments=[first],
        credentials=CredentialRegistry(
            [CredentialDescriptor(first.secret_ref, shared_fingerprint)]
        ),
        key_prefix=prefix,
    )
    second_pool = CapacityPool(
        other_client,
        deployments=[second],
        credentials=CredentialRegistry(
            [CredentialDescriptor(second.secret_ref, shared_fingerprint)]
        ),
        key_prefix=prefix,
    )
    try:
        await first_pool.initialize()
        with pytest.raises(CapacityConfigurationError, match="quota scope conflicts"):
            await second_pool.initialize()
    finally:
        await other_client.aclose()


@pytest.mark.integration
async def test_distinct_fingerprints_and_scopes_initialize_independent_slots(
    redis_client: Redis, prefix: str
) -> None:
    first = deployment("distinct-a", "distinct-scope-a")
    second = deployment("distinct-b", "distinct-scope-b")
    pool = CapacityPool(
        redis_client,
        deployments=[first, second],
        credentials=credentials("distinct-a", "distinct-b"),
        key_prefix=prefix,
    )
    await pool.initialize()
    one, two = await asyncio.gather(
        pool.acquire([first, second], wait_timeout=0.05, estimated_tokens=1),
        pool.acquire([first, second], wait_timeout=0.05, estimated_tokens=1),
    )
    assert one.quota_scope_id != two.quota_scope_id
    await asyncio.gather(pool.release(one), pool.release(two))


@pytest.mark.parametrize("estimated", [0, -1, 1.0, True, None])
@pytest.mark.integration
async def test_estimated_tokens_must_be_strict_positive_integer(
    redis_client: Redis, prefix: str, estimated: object
) -> None:
    pool = CapacityPool(redis_client, key_prefix=prefix)
    with pytest.raises(ValueError, match="estimated_tokens"):
        await pool.acquire(
            [deployment("serial", "validation")],
            wait_timeout=0.1,
            estimated_tokens=estimated,  # type: ignore[arg-type]
        )


@pytest.mark.integration
async def test_backend_failure_fails_closed_without_backend_details(prefix: str) -> None:
    unavailable = Redis.from_url("redis://127.0.0.1:1/15", socket_connect_timeout=0.01)
    try:
        serial = deployment("serial", "failure")
        pool = CapacityPool(
            unavailable,
            deployments=[serial],
            credentials=credentials("serial"),
            key_prefix=prefix,
        )
        with pytest.raises(CapacityBackendError) as captured:
            await pool.initialize()
    finally:
        await unavailable.aclose()
    assert "127.0.0.1" not in str(captured.value)
