import asyncio

import pytest

from agent_hub.models.capacity import CapacityBackendError, CapacityPool
from tests.unit.models.test_capacity import InMemoryCapacityRedis, deployment, fingerprint


class UnresponsiveRegistrationRedis(InMemoryCapacityRedis):
    def __init__(self, *, block_rollback: bool) -> None:
        super().__init__()
        self.block_rollback = block_rollback
        self.cancelled = asyncio.Event()
        self.proceed = asyncio.Event()

    async def eval(self, script: str, key_count: int, *args: object) -> object:
        is_rollback = (key_count, len(args)) in {(2, 3), (7, 8)}
        if is_rollback == self.block_rollback:
            try:
                await self.proceed.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if is_rollback:
            return 1
        if self.block_rollback or self.proceed.is_set():
            raise ConnectionError("private backend")
        return await super().eval(script, key_count, *args)


@pytest.mark.parametrize("block_rollback", [False, True])
async def test_registration_and_rollback_have_a_finite_backend_horizon(
    block_rollback: bool,
) -> None:
    redis = UnresponsiveRegistrationRedis(block_rollback=block_rollback)
    selected = deployment("selected", "selected")
    root = CapacityPool(
        redis, deployments=[selected], fingerprint_resolver=fingerprint,
        lease_seconds=0.05,
    )
    task = asyncio.create_task(root.initialize())
    done, _ = await asyncio.wait({task}, timeout=0.5)
    try:
        assert task in done, "registration or rollback held the request indefinitely"
        with pytest.raises(CapacityBackendError):
            task.result()
        assert redis.cancelled.is_set()
        assert not root._initialized
    finally:
        redis.proceed.set()
        await asyncio.gather(task, return_exceptions=True)
