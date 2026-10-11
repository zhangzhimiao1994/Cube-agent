import asyncio
import hashlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest

from agent_hub.models.capacity import (
    CapacityBackendError,
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
        self.acquire_token_estimates: list[int] = []

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
            self.acquire_token_estimates.append(int(str(args[7])))
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


class RefreshBarrierRedis(InMemoryCapacityRedis):
    def __init__(self) -> None:
        super().__init__()
        self.block_registration = False
        self.fail_registration = False
        self.registration_started = asyncio.Event()
        self.allow_registration = asyncio.Event()
        self.block_acquire = False
        self.acquire_released = asyncio.Event()
        self.release_calls = 0
        self.lease_ids: set[str] = set()
        self.issued_lease_ids: list[str] = []
        self.released_lease_ids: list[str] = []

    async def eval(self, script: str, key_count: int, *args: object) -> object:
        if key_count == 5 and self.block_acquire:
            self.acquire_started.set()
            await self.acquire_released.wait()
        if key_count == 1 and len(args) == 3:
            self.release_calls += 1
            self.released_lease_ids.append(str(args[1]))
            self.lease_ids.discard(str(args[1]))
        if key_count == 2 and len(args) == 6:
            if self.block_registration:
                self.registration_started.set()
                await self.allow_registration.wait()
            if self.fail_registration:
                raise ConnectionError("registration unavailable")
        result = await super().eval(script, key_count, *args)
        if key_count == 5 and isinstance(result, list) and result[0] == 1:
            lease_id = str(args[5])
            self.lease_ids.add(lease_id)
            self.issued_lease_ids.append(lease_id)
        return result


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


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
@pytest.mark.parametrize("failed_refresh", [False, True], ids=["success", "failure"])
async def test_initialize_waits_for_inflight_refresh(
    scoped: bool, failed_refresh: bool,
) -> None:
    redis = RefreshBarrierRedis()
    selected = deployment("selected", "selected-scope")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    reader = root.scoped([selected]) if scoped else root
    await capacity.initialize()
    existing = await capacity.acquire([selected], wait_timeout=0, estimated_tokens=1)
    redis.block_registration = True
    redis.fail_registration = failed_refresh
    renewal = asyncio.create_task(capacity.renew(existing))
    initialization = None
    try:
        await asyncio.wait_for(redis.registration_started.wait(), timeout=1)
        initialization = asyncio.create_task(reader.initialize())
        done, _ = await asyncio.wait({initialization}, timeout=0.02)
        assert not done, "initialize returned before the in-flight refresh settled"
        assert redis.acquire_calls == 1
    finally:
        redis.block_registration = False
        redis.allow_registration.set()
        pending = [renewal] if initialization is None else [renewal, initialization]
        results = await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=1,
        )
        await capacity.release(existing)
    if failed_refresh:
        assert all(isinstance(result, CapacityBackendError) for result in results)
        with pytest.raises(CapacityConfigurationError):
            await reader.acquire([selected], wait_timeout=0, estimated_tokens=1)
        assert redis.acquire_calls == 1
    else:
        assert results[0] is not None and not isinstance(results[0], BaseException)
        assert results[1] is None
        acquired = await reader.acquire([selected], wait_timeout=0, estimated_tokens=1)
        assert acquired.deployment_id == selected.id
        await reader.release(acquired)
    assert root._waiters == 0


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
@pytest.mark.parametrize("stage", ["waiter", "rpc"])
@pytest.mark.parametrize("failure", ["expired", "refresh-failed"])
async def test_acquire_rechecks_registration_after_waiting(
    scoped: bool, stage: str, failure: str,
) -> None:
    redis = RefreshBarrierRedis()
    selected = deployment("selected", "selected-scope")
    now = 0.0
    root = CapacityPool(
        redis, deployments=[selected], fingerprint_resolver=fingerprint, monotonic=lambda: now,
    )
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()
    existing = await capacity.acquire([selected], wait_timeout=0, estimated_tokens=1)
    if stage == "waiter":
        await root._waiter_lock.acquire()
    else:
        redis.block_acquire = True
    pending = asyncio.create_task(capacity.acquire([selected], wait_timeout=1, estimated_tokens=1))
    try:
        if stage == "rpc":
            await asyncio.wait_for(redis.acquire_started.wait(), timeout=1)
        else:
            done, _ = await asyncio.wait({pending}, timeout=0.02)
            assert not done
        if failure == "expired":
            now = 3600.0
        else:
            redis.fail_registration = True
            with pytest.raises(CapacityBackendError):
                await capacity.renew(existing)
        before_release = redis.release_calls
        if stage == "waiter":
            root._waiter_lock.release()
        else:
            redis.acquire_released.set()
        with pytest.raises(CapacityConfigurationError):
            await asyncio.wait_for(pending, timeout=1)
        assert redis.acquire_calls == (2 if stage == "rpc" else 1)
        assert redis.release_calls == before_release + (1 if stage == "rpc" else 0)
        assert redis.lease_ids == {existing.id}
        if stage == "rpc":
            assert redis.released_lease_ids[-1] == redis.issued_lease_ids[-1]
            assert redis.released_lease_ids[-1] != existing.id
        assert root._waiters == 0
    finally:
        redis.acquire_released.set()
        if stage == "waiter" and root._waiter_lock.locked():
            root._waiter_lock.release()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await capacity.release(existing)


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
@pytest.mark.parametrize("ending", ["cancel", "deadline"])
async def test_initialize_waiting_for_refresh_has_bounded_cancellation(
    scoped: bool, ending: str,
) -> None:
    redis = RefreshBarrierRedis()
    selected = deployment("selected", "selected-scope")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()
    existing = await capacity.acquire([selected], wait_timeout=0, estimated_tokens=1)
    redis.block_registration = True
    renewal = asyncio.create_task(capacity.renew(existing))
    initialization = None
    try:
        await asyncio.wait_for(redis.registration_started.wait(), timeout=1)
        initialization = asyncio.create_task(capacity.initialize())
        done, _ = await asyncio.wait({initialization}, timeout=0.02)
        assert not done, "initialize bypassed the refresh before cancellation"
        if ending == "cancel":
            initialization.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(initialization, timeout=1)
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(initialization, timeout=0.02)
        assert not renewal.done()
        assert redis.acquire_calls == 1
    finally:
        if initialization is not None and not initialization.done():
            initialization.cancel()
        redis.block_registration = False
        redis.allow_registration.set()
        pending = [renewal] if initialization is None else [renewal, initialization]
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=1)
        await capacity.release(existing)
    await capacity.initialize()
    acquired = await capacity.acquire([selected], wait_timeout=0, estimated_tokens=1)
    await capacity.release(acquired)
    assert root._waiters == 0


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_acquire_rejects_refresh_started_after_initialize(scoped: bool) -> None:
    redis = RefreshBarrierRedis()
    selected = deployment("selected", "selected-scope")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    reader = root.scoped([selected]) if scoped else root
    await capacity.initialize()
    existing = await capacity.acquire([selected], wait_timeout=0, estimated_tokens=1)
    await reader.initialize()
    redis.block_registration = True
    renewal = asyncio.create_task(capacity.renew(existing))
    try:
        await asyncio.wait_for(redis.registration_started.wait(), timeout=1)
        with pytest.raises(CapacityConfigurationError):
            await reader.acquire([selected], wait_timeout=0, estimated_tokens=1)
        assert redis.acquire_calls == 1
    finally:
        redis.block_registration = False
        redis.allow_registration.set()
        await asyncio.wait_for(renewal, timeout=1)
        await capacity.release(existing)
    acquired = await reader.acquire([selected], wait_timeout=0, estimated_tokens=1)
    await reader.release(acquired)
    assert root._waiters == 0


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
@pytest.mark.parametrize("state", ["uninitialized", "expired", "failed-refresh"])
async def test_acquire_rejects_invalid_registration(scoped: bool, state: str) -> None:
    redis = RefreshBarrierRedis()
    selected = deployment("selected", "selected-scope")
    now = 0.0
    root = CapacityPool(
        redis, deployments=[selected], fingerprint_resolver=fingerprint, monotonic=lambda: now,
    )
    capacity = root.scoped([selected]) if scoped else root
    if state != "uninitialized":
        await capacity.initialize()
    if state == "expired":
        now = 3600.0
    elif state == "failed-refresh":
        existing = await capacity.acquire([selected], wait_timeout=0, estimated_tokens=1)
        redis.fail_registration = True
        try:
            with pytest.raises(CapacityBackendError):
                await capacity.renew(existing)
        finally:
            await capacity.release(existing)
    prior_calls = redis.acquire_calls
    with pytest.raises(CapacityConfigurationError):
        await capacity.acquire([selected], wait_timeout=0, estimated_tokens=1)
    assert redis.acquire_calls == prior_calls
    assert root._waiters == 0


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


async def test_scoped_views_use_each_deployments_token_estimate() -> None:
    redis = InMemoryCapacityRedis()
    first = deployment("a", "shared-scope")
    second = deployment("b", "shared-scope")
    capacity = pool(redis, [first, second])
    view = capacity.scoped([first, second])
    await view.initialize()

    estimates = {"a": 11, "b": 22}
    for _ in range(2):
        acquired = await view.acquire(
            [first, second], wait_timeout=0.1, estimated_tokens=estimates
        )
        await view.release(acquired)

    assert redis.acquire_token_estimates == [11, 22]


@pytest.mark.parametrize("estimated", [None, True, 1.0])
async def test_capacity_rejects_non_integer_token_estimates_with_value_error(
    estimated: object,
) -> None:
    selected = deployment("selected", "selected-scope")
    capacity = CapacityPool(object())

    with pytest.raises(ValueError, match="estimated_tokens"):
        await capacity.acquire(
            [selected],
            wait_timeout=0,
            estimated_tokens=estimated,  # type: ignore[arg-type]
        )


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
