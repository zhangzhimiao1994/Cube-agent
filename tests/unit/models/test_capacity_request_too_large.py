import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityRequestTooLarge,
    CapacityUnavailable,
    CapacityWaitTimeout,
)
from tests.unit.models.test_capacity import InMemoryCapacityRedis, deployment, pool


class AdmissionRedis(InMemoryCapacityRedis):
    """Script-boundary outcomes, not an emulation or proof of Redis Lua execution."""

    def __init__(self) -> None:
        super().__init__()
        self.tpm = 100
        self.busy_costs: set[int] = set()
        self.release_calls = 0
        self.after_oversized: Callable[[], Awaitable[None]] | None = None
        self.malformed: object | None = None

    async def eval(self, script: str, key_count: int, *args: object) -> object:
        if key_count == 5:
            cost = int(str(args[7]))
            self.acquire_calls += 1
            self.acquire_token_estimates.append(cost)
            if self.malformed is not None:
                return self.malformed
            if self.tpm > 0 and cost > self.tpm:
                callback, self.after_oversized = self.after_oversized, None
                if callback is not None:
                    await callback()
                return [0, 0, 1, 3]
            if cost in self.busy_costs:
                return [0, 0, 1, 2]
            return [1, 1, 1, 1_800_000_000_000]
        if key_count == 1 and len(args) == 3:
            self.release_calls += 1
        return await super().eval(script, key_count, *args)


def test_request_too_large_is_not_a_timeout() -> None:
    error = CapacityRequestTooLarge()
    assert isinstance(error, ValueError)
    assert not isinstance(error, TimeoutError | CapacityUnavailable)
    assert str(error) == "model request exceeds capacity token limit"


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_all_oversized_fails_without_polling(
    scoped: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = AdmissionRedis()
    candidates = [deployment("first", "shared"), deployment("second", "shared"),
                  deployment("third", "separate")]
    root = pool(redis, candidates)
    capacity = root.scoped(candidates) if scoped else root
    await capacity.initialize()

    async def no_poll(deadline: float) -> None:
        raise AssertionError("an impossible request must not wait")

    monkeypatch.setattr(root, "_wait_for_next_poll", no_poll)
    with pytest.raises(CapacityRequestTooLarge):
        await capacity.acquire(candidates, wait_timeout=1, estimated_tokens=101)
    assert redis.acquire_calls == len(candidates)
    assert redis.acquire_token_estimates == [101] * len(candidates)
    assert redis.release_calls == root._waiters == 0


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_duplicate_oversized_candidates_fail_without_polling(
    scoped: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = AdmissionRedis()
    selected = deployment("selected", "scope")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()

    async def no_poll(deadline: float) -> None:
        raise AssertionError("duplicate candidates must not hide an impossible request")

    monkeypatch.setattr(root, "_wait_for_next_poll", no_poll)
    with pytest.raises(CapacityRequestTooLarge):
        await capacity.acquire([selected, selected], wait_timeout=1, estimated_tokens=101)
    assert redis.acquire_token_estimates == [101]
    assert redis.release_calls == root._waiters == 0


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_duplicate_id_does_not_hide_catalog_mismatch(scoped: bool) -> None:
    redis = AdmissionRedis()
    selected = deployment("selected", "scope")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()
    with pytest.raises(CapacityConfigurationError, match="outside initialized catalog"):
        await capacity.acquire([selected, replace(selected, tpm=1)], wait_timeout=0,
                               estimated_tokens=101)
    assert redis.acquire_calls == root._waiters == 0


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_same_scope_smaller_candidate_can_acquire(scoped: bool) -> None:
    redis = AdmissionRedis()
    large = deployment("a-large", "shared")
    small = deployment("b-small", "shared")
    candidates = [large, small]
    root = pool(redis, candidates)
    capacity = root.scoped(candidates) if scoped else root
    await capacity.initialize()
    lease = await capacity.acquire(candidates, wait_timeout=0,
                                   estimated_tokens={large.id: 101, small.id: 100})
    assert lease.deployment_id == small.id
    assert redis.acquire_token_estimates == [101, 100]
    assert root._waiters == 0
    await capacity.release(lease)


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_oversized_and_temporary_busy_still_waits(
    scoped: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = AdmissionRedis()
    redis.busy_costs = {100}
    large = deployment("a-large", "shared")
    busy = deployment("b-busy", "shared")
    candidates = [large, busy]
    root = pool(redis, candidates)
    capacity = root.scoped(candidates) if scoped else root
    await capacity.initialize()
    polls = 0

    async def timeout_poll(deadline: float) -> None:
        nonlocal polls
        polls += 1
        raise CapacityWaitTimeout("still busy")

    monkeypatch.setattr(root, "_wait_for_next_poll", timeout_poll)
    with pytest.raises(CapacityWaitTimeout):
        await capacity.acquire(candidates, wait_timeout=1,
                               estimated_tokens={large.id: 101, busy.id: 100})
    assert redis.acquire_token_estimates == [101, 100]
    assert polls == 1 and root._waiters == 0
    assert redis.release_calls == 0


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_refresh_rechecks_oversized_candidate(scoped: bool) -> None:
    redis = AdmissionRedis()
    selected = deployment("selected", "scope")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()

    async def refresh() -> None:
        redis.tpm = 200
        await capacity._ensure_registration(force=True)

    redis.after_oversized = refresh
    lease = await capacity.acquire([selected], wait_timeout=1, estimated_tokens=101)
    assert redis.acquire_token_estimates == [101, 101]
    assert root._waiters == 0
    await capacity.release(lease)


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_oversized_exclusion_does_not_survive_an_acquire(scoped: bool) -> None:
    redis = AdmissionRedis()
    selected = deployment("selected", "scope")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()
    with pytest.raises(CapacityRequestTooLarge):
        await capacity.acquire([selected], wait_timeout=0, estimated_tokens=101)
    redis.tpm = 200
    lease = await capacity.acquire([selected], wait_timeout=0, estimated_tokens=101)
    assert redis.acquire_token_estimates == [101, 101]
    await capacity.release(lease)


@pytest.mark.parametrize("malformed", [[0, 0, 1, True], [0, 0, 1, 3.0],
                                      [0, 0, 1, 4], [2, 0, 1, 3], [0, 0, 1, 3, 0]])
async def test_invalid_oversized_state_is_a_backend_error(malformed: object) -> None:
    redis = AdmissionRedis()
    redis.malformed = malformed
    selected = deployment("selected", "scope")
    root = pool(redis, [selected])
    await root.initialize()
    with pytest.raises(CapacityBackendError):
        await root.acquire([selected], wait_timeout=0, estimated_tokens=101)
    assert redis.release_calls == 1 and root._waiters == 0


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_busy_poll_rechecks_authority(scoped: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    redis = AdmissionRedis()
    redis.busy_costs = {50}
    large, busy = deployment("large", "a"), deployment("busy", "b")
    candidates = [large, busy]
    root = pool(redis, candidates)
    capacity = root.scoped(candidates) if scoped else root
    await capacity.initialize()

    async def change_authority(deadline: float) -> None:
        redis.tpm = 200
        await asyncio.sleep(0)

    monkeypatch.setattr(root, "_wait_for_next_poll", change_authority)
    lease = await capacity.acquire(candidates, wait_timeout=1,
                                   estimated_tokens={large.id: 101, busy.id: 50})
    assert lease.deployment_id == large.id
    assert redis.acquire_token_estimates.count(101) == 2
    await capacity.release(lease)
