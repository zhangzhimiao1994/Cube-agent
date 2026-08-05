import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from agent_hub.models.capacity import (
    CapacityBackendError,
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


@pytest.mark.integration
async def test_serial_scope_times_out_and_release_recovers(
    redis_client: Redis, prefix: str
) -> None:
    pool = CapacityPool(redis_client, key_prefix=prefix, poll_interval=0.005)
    serial = deployment("serial", "account")

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
    pool = CapacityPool(redis_client, key_prefix=prefix, poll_interval=0.005)
    account_a = deployment("key-a", "account-a")
    account_b = deployment("key-b", "account-b")
    first, second = await asyncio.gather(
        pool.acquire([account_a, account_b], wait_timeout=0.1, estimated_tokens=1),
        pool.acquire([account_a, account_b], wait_timeout=0.1, estimated_tokens=1),
    )
    assert {first.quota_scope_id, second.quota_scope_id} == {"account-a", "account-b"}
    await asyncio.gather(pool.release(first), pool.release(second))

    shared_a = deployment("shared-a", "shared")
    shared_b = deployment("shared-b", "shared")
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
    pool = CapacityPool(redis_client, deployments=[permissive, strict], key_prefix=prefix)

    held = await pool.acquire([permissive], wait_timeout=0.05, estimated_tokens=1)
    with pytest.raises(CapacityUnavailable):
        await pool.acquire([permissive], wait_timeout=0.02, estimated_tokens=1)
    await pool.release(held)


@pytest.mark.integration
async def test_expiry_and_exact_renew_semantics(redis_client: Redis, prefix: str) -> None:
    pool = CapacityPool(
        redis_client, key_prefix=prefix, lease_seconds=0.08, poll_interval=0.005
    )
    serial = deployment("serial", "expiry")
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
async def test_rpm_tpm_are_scope_shared_and_full_capacity_spends_nothing(
    redis_client: Redis, prefix: str
) -> None:
    pool = CapacityPool(redis_client, key_prefix=prefix, poll_interval=0.005)
    limited = deployment("limited", "rate", rpm=2, tpm=10)
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
async def test_atomic_stress_across_two_pool_instances_never_oversubscribes(
    redis_client: Redis, redis_url: str, prefix: str
) -> None:
    other_client = Redis.from_url(redis_url)
    first_pool = CapacityPool(redis_client, key_prefix=prefix, poll_interval=0.001)
    second_pool = CapacityPool(other_client, key_prefix=prefix, poll_interval=0.001)
    serial = deployment("serial", "atomic")
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
        key_prefix=prefix,
        cooldown_seconds=0.04,
        latency_threshold_seconds=0.2,
    )
    assert await pool.effective_limit("health") == 4
    await pool.record_outcome("health", status_code=429, latency_seconds=0.01)
    assert await pool.effective_limit("health") == 2
    await pool.record_outcome("health", status_code=200, latency_seconds=0.01)
    assert await pool.effective_limit("health") == 2
    await asyncio.sleep(0.05)
    await pool.record_outcome("health", status_code=200, latency_seconds=0.01)
    assert await pool.effective_limit("health") == 3
    await pool.record_outcome("health", status_code=200, latency_seconds=0.01)
    assert await pool.effective_limit("health") == 4
    await pool.record_outcome("health", status_code=200, latency_seconds=0.01)
    assert await pool.effective_limit("health") == 4


@pytest.mark.integration
async def test_p95_and_503_share_health_and_keys_have_ttls(
    redis_client: Redis, prefix: str
) -> None:
    wide = deployment("wide", "shared-health", max_concurrency=5, target_utilization=0.8)
    pool = CapacityPool(
        redis_client,
        deployments=[wide],
        key_prefix=prefix,
        cooldown_seconds=0.03,
        latency_threshold_seconds=0.02,
        health_window=4,
    )
    await pool.record_outcome("shared-health", status_code=200, latency_seconds=0.1)
    assert await pool.effective_limit("shared-health") == 2
    await pool.record_outcome("shared-health", status_code=503, latency_seconds=0.001)
    assert await pool.effective_limit("shared-health") == 1
    keys = [key async for key in redis_client.scan_iter(match=f"{prefix}*")]
    assert keys
    ttls = [await redis_client.ttl(key) for key in keys]
    assert all(0 < ttl <= 3600 for ttl in ttls)


def test_duplicate_fingerprint_cannot_claim_independent_scopes() -> None:
    credentials = CredentialRegistry(
        [
            CredentialDescriptor("secret://one", "same-fingerprint"),
            CredentialDescriptor("secret://two", "same-fingerprint"),
        ]
    )
    with pytest.raises(ValueError, match="credential fingerprint.*quota scope"):
        CapacityPool(
            object(),
            deployments=[deployment("one", "scope-a"), deployment("two", "scope-b")],
            credentials=credentials,
        )


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
        pool = CapacityPool(unavailable, key_prefix=prefix)
        with pytest.raises(CapacityBackendError) as captured:
            await pool.acquire(
                [deployment("serial", "failure")], wait_timeout=0.1, estimated_tokens=1
            )
    finally:
        await unavailable.aclose()
    assert "127.0.0.1" not in str(captured.value)
