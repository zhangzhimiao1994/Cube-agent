import asyncio
import hashlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest

from agent_hub.models.capacity import (
    CapacityConfigurationError,
    CapacityPool,
    CapacityQueueFull,
)
from agent_hub.models.types import Deployment


def deployment(identifier: str, scope: str, **overrides: object) -> Deployment:
    values: dict[str, object] = {
        "id": identifier,
        "logical_model": "chat",
        "secret_ref": f"secret://{identifier}",
        "quota_scope_id": scope,
        "max_concurrency": 1,
        "target_utilization": 0.8,
        "weight": 1,
    }
    values.update(overrides)
    return Deployment(**values)  # type: ignore[arg-type]


async def fingerprint(secret_ref: str) -> str:
    return hashlib.sha256(secret_ref.encode("utf-8")).hexdigest()


class InMemoryCapacityRedis:
    """Small stateful Redis script boundary used by scoped-pool unit tests."""

    def __init__(self, *, block_first_acquire: bool = False) -> None:
        self.fingerprint_owners: dict[str, set[str]] = {}
        self.policy_owners: dict[str, set[str]] = {}
        self.registered_policy_bases: list[int] = []
        self.block_first_acquire = block_first_acquire
        self.acquire_started = asyncio.Event()
        self.allow_acquire = asyncio.Event()
        self.acquire_calls = 0

    async def eval(self, script: str, key_count: int, *args: object) -> object:
        del script
        if key_count == 2 and len(args) == 6:
            fingerprint_key = str(args[0])
            owner = str(args[2])
            owners = self.fingerprint_owners.setdefault(fingerprint_key, set())
            created = int(owner not in owners)
            owners.add(owner)
            return [1, created, len(owners)]
        if key_count == 7 and len(args) == 13:
            policy_key = str(args[1])
            owner = str(args[7])
            base = int(str(args[8]))
            owners = self.policy_owners.setdefault(policy_key, set())
            created = int(owner not in owners)
            owners.add(owner)
            self.registered_policy_bases.append(base)
            return [base, int(str(args[9])), int(str(args[10])), created, len(owners)]
        if key_count == 5:
            self.acquire_calls += 1
            if self.block_first_acquire and self.acquire_calls == 1:
                self.acquire_started.set()
                await self.allow_acquire.wait()
            if self.block_first_acquire and self.acquire_calls > 1:
                return [0, 1, 1, 1]
            return [1, 1, 1, 1_800_000_000_000]
        if key_count == 1 and len(args) == 3:
            return 1
        if key_count == 1 and len(args) == 4:
            return 1_800_000_000_000
        if key_count == 3:
            return [1, 1, 1]
        if key_count == 2 and len(args) == 3:
            return 1
        if key_count == 7 and len(args) == 8:
            return 1
        raise AssertionError(f"unexpected Redis script call: {key_count=}, {len(args)=}")


def pool(
    redis: InMemoryCapacityRedis,
    deployments: list[Deployment],
    *,
    resolver: Callable[[str], Awaitable[str]] = fingerprint,
    max_waiters: int = 1024,
) -> CapacityPool:
    return CapacityPool(
        redis,
        deployments=deployments,
        fingerprint_resolver=resolver,
        key_prefix="agent-hub:test:scoped-capacity:",
        max_waiters=max_waiters,
    )


async def test_scoped_views_reuse_owner_and_registration_state() -> None:
    redis = InMemoryCapacityRedis()
    selected = deployment("selected", "selected-scope")
    resolutions: list[str] = []

    async def recording_fingerprint(secret_ref: str) -> str:
        resolutions.append(secret_ref)
        return await fingerprint(secret_ref)

    capacity = pool(redis, [selected], resolver=recording_fingerprint)

    await capacity.scoped([selected]).initialize()
    await capacity.scoped([selected]).initialize()

    assert resolutions == [selected.secret_ref]
    assert redis.registered_policy_bases == [1]
    assert [len(owners) for owners in redis.fingerprint_owners.values()] == [1]
    assert [len(owners) for owners in redis.policy_owners.values()] == [1]


async def test_scoped_views_share_the_bounded_wait_queue() -> None:
    redis = InMemoryCapacityRedis(block_first_acquire=True)
    first = deployment("first", "first-scope")
    second = deployment("second", "second-scope")
    capacity = pool(redis, [first, second], max_waiters=1)
    first_view = capacity.scoped([first])
    second_view = capacity.scoped([second])
    await first_view.initialize()
    await second_view.initialize()

    waiting = asyncio.create_task(
        first_view.acquire([first], wait_timeout=1, estimated_tokens=1)
    )
    await redis.acquire_started.wait()
    try:
        with pytest.raises(CapacityQueueFull, match="queue is full"):
            await second_view.acquire([second], wait_timeout=0, estimated_tokens=1)
    finally:
        redis.allow_acquire.set()
    acquired = await waiting
    await first_view.release(acquired)


async def test_scoped_views_preserve_weighted_routing_progress() -> None:
    redis = InMemoryCapacityRedis()
    first = deployment("a", "shared-scope")
    second = deployment("b", "shared-scope")
    capacity = pool(redis, [first, second])

    selected_ids: list[str] = []
    for _ in range(2):
        view = capacity.scoped([first, second])
        await view.initialize()
        acquired = await view.acquire(
            [first, second], wait_timeout=0.1, estimated_tokens=1
        )
        selected_ids.append(acquired.deployment_id)
        await view.release(acquired)

    assert selected_ids == ["a", "b"]


async def test_scoped_view_registers_global_most_restrictive_shared_scope_policy() -> None:
    redis = InMemoryCapacityRedis()
    permissive = deployment(
        "permissive",
        "shared-scope",
        max_concurrency=10,
        target_utilization=0.9,
    )
    strict = deployment("strict", "shared-scope")
    capacity = pool(redis, [permissive, strict])

    await capacity.scoped([permissive]).initialize()

    assert redis.registered_policy_bases == [1]


async def test_fingerprint_resolution_error_drops_secret_exception_context() -> None:
    redis = InMemoryCapacityRedis()
    selected = deployment("selected", "selected-scope")

    async def failing_fingerprint(secret_ref: str) -> str:
        del secret_ref
        raise RuntimeError("vault://user:password@private-backend")

    capacity = pool(redis, [selected], resolver=failing_fingerprint)

    with pytest.raises(
        CapacityConfigurationError,
        match="credential fingerprint resolution failed",
    ) as captured:
        await capacity.scoped([selected]).initialize()

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "private-backend" not in str(captured.value)
