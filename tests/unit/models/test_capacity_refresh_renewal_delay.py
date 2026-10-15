"""Registration coordination must not hand out a stale relative renewal delay."""

from __future__ import annotations

import asyncio

import pytest

from agent_hub.models.capacity import CapacityLease
from agent_hub.models.types import Deployment
from tests.unit.models.test_capacity import RefreshBarrierRedis, deployment, pool


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_post_rpc_refresh_wait_reduces_handed_off_renewal_delay(
    scoped: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = RefreshBarrierRedis()
    selected = deployment("selected", "selected-scope")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()
    existing = await capacity.acquire([selected], wait_timeout=0, estimated_tokens=1)
    issued: list[CapacityLease] = []
    issued_event = asyncio.Event()
    original = root._try_acquire

    async def capture(
        candidate: Deployment, estimated_tokens: int, *, timeout: float,
    ) -> CapacityLease | None:
        result = await original(candidate, estimated_tokens, timeout=timeout)
        if result is not None:
            issued.append(result)
            issued_event.set()
        return result

    monkeypatch.setattr(root, "_try_acquire", capture)
    redis.block_acquire = True
    pending = asyncio.create_task(capacity.acquire(
        [selected], wait_timeout=1, estimated_tokens=12288,
    ))
    renewal: asyncio.Task[CapacityLease | None] | None = None
    returned: CapacityLease | None = None
    try:
        await asyncio.wait_for(redis.acquire_started.wait(), timeout=1)
        redis.block_registration = True
        renewal = asyncio.create_task(capacity.renew(existing))
        await asyncio.wait_for(redis.registration_started.wait(), timeout=1)
        redis.acquire_released.set()
        await asyncio.wait_for(issued_event.wait(), timeout=1)
        await asyncio.sleep(0.03)
        assert not pending.done()
        redis.block_registration = False
        redis.allow_registration.set()
        returned = await asyncio.wait_for(pending, timeout=1)
        assert returned.id == issued[0].id
        assert returned.expires_at == issued[0].expires_at
        assert 0 <= returned.renew_after_seconds <= issued[0].renew_after_seconds - 0.02
        assert root._waiters == 0
    finally:
        redis.block_registration = False
        redis.allow_registration.set()
        redis.acquire_released.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, *([] if renewal is None else [renewal]), return_exceptions=True)
        for granted in (existing, returned or (issued[0] if issued else None)):
            if granted is not None:
                await capacity.release(granted)
        if root._lease_cleanups:
            await asyncio.gather(*root._lease_cleanups)
    assert not redis.lease_ids and root._waiters == 0
