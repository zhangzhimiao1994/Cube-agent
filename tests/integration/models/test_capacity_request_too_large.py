import asyncio
from collections.abc import AsyncIterator, Awaitable
from typing import cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from agent_hub.models.capacity import CapacityPool, CapacityRequestTooLarge
from agent_hub.models.gateway import ConservativeTokenEstimator, ModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment, ModelMessage, ModelRequest, ModelResponse
from tests.integration.models.test_capacity import credentials, deployment

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    try:
        await client.ping()
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def prefix(redis_client: Redis) -> AsyncIterator[str]:
    value = f"agent-hub:test:oversized:{uuid4()}:"
    yield value
    keys = [key async for key in redis_client.scan_iter(match=f"{value}*")]
    if keys:
        await redis_client.unlink(*keys)


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_redis_oversized_precedes_busy_without_spending(
    redis_client: Redis, prefix: str, scoped: bool,
) -> None:
    selected = deployment("selected", "scope", rpm=10, tpm=100)
    root = CapacityPool(redis_client, deployments=[selected],
                        credentials=credentials("selected"), key_prefix=prefix)
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()
    lease = await capacity.acquire([selected], wait_timeout=0, estimated_tokens=1)
    keys = root._keys(selected.quota_scope_id)
    before = {name: await cast(Awaitable[dict[bytes, bytes]], redis_client.hgetall(keys[name]))
              for name in ("rpm", "tpm")}
    leases_before = await redis_client.zrange(keys["leases"], 0, -1, withscores=True)
    try:
        with pytest.raises(CapacityRequestTooLarge):
            await capacity.acquire([selected], wait_timeout=0, estimated_tokens=101)
        assert {name: await cast(Awaitable[dict[bytes, bytes]], redis_client.hgetall(keys[name]))
                for name in ("rpm", "tpm")} == before
        assert await redis_client.zrange(keys["leases"], 0, -1,
                                        withscores=True) == leases_before
        assert root._waiters == 0
    finally:
        await capacity.release(lease)


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_redis_authoritative_tpm_overrides_candidate_limit(
    redis_client: Redis, prefix: str, scoped: bool,
) -> None:
    permissive = deployment("permissive", "shared", tpm=1000)
    strict = deployment("strict", "shared", tpm=100)
    root = CapacityPool(redis_client, deployments=[permissive],
                        credentials=credentials("permissive"), key_prefix=prefix)
    other = CapacityPool(redis_client, deployments=[strict],
                         credentials=credentials("strict"), key_prefix=prefix)
    capacity = root.scoped([permissive]) if scoped else root
    await capacity.initialize()
    await other.initialize()
    keys = root._keys("shared")
    assert await cast(Awaitable[bytes | None], redis_client.hget(keys["policy"], "tpm")) == b"100"
    assert root._scope_policies["shared"].tpm == 1000
    with pytest.raises(CapacityRequestTooLarge):
        await capacity.acquire([permissive], wait_timeout=0, estimated_tokens=101)
    assert await redis_client.zcard(keys["leases"]) == 0
    assert not await redis_client.exists(keys["rpm"], keys["tpm"])


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
async def test_same_scope_smaller_candidate_can_acquire(
    redis_client: Redis, prefix: str, scoped: bool,
) -> None:
    large = deployment("a-large", "shared", tpm=100, rpm=10)
    small = deployment("b-small", "shared", tpm=100, rpm=10)
    candidates = [large, small]
    root = CapacityPool(redis_client, deployments=candidates,
                        credentials=credentials("a-large", "b-small"), key_prefix=prefix)
    capacity = root.scoped(candidates) if scoped else root
    await capacity.initialize()
    lease = await capacity.acquire(candidates, wait_timeout=0,
                                   estimated_tokens={large.id: 101, small.id: 100})
    try:
        assert lease.deployment_id == small.id
        assert await redis_client.zcard(root._keys("shared")["leases"]) == 1
        assert await cast(Awaitable[bytes | None], redis_client.hget(
            root._keys("shared")["rpm"], "tokens")) == b"9"
        assert await cast(Awaitable[bytes | None], redis_client.hget(
            root._keys("shared")["tpm"], "tokens")) == b"0"
    finally:
        await capacity.release(lease)


@pytest.mark.parametrize("scoped", [False, True], ids=["root", "scope"])
@pytest.mark.parametrize("tpm", [None, 100], ids=["unlimited", "equal"])
async def test_exact_and_unlimited_tpm_can_acquire(
    redis_client: Redis, prefix: str, scoped: bool, tpm: int | None,
) -> None:
    selected = deployment("selected", "scope", tpm=tpm)
    root = CapacityPool(redis_client, deployments=[selected],
                        credentials=credentials("selected"), key_prefix=prefix)
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()
    lease = await capacity.acquire([selected], wait_timeout=0, estimated_tokens=100)
    try:
        assert lease.deployment_id == selected.id
        keys = root._keys("scope")
        if tpm is None:
            assert not await redis_client.exists(keys["tpm"])
        else:
            assert await cast(Awaitable[bytes | None],
                              redis_client.hget(keys["tpm"], "tokens")) == b"0"
    finally:
        await capacity.release(lease)


@pytest.mark.parametrize("streaming", [False, True], ids=["complete", "stream"])
async def test_gateway_no_fallback_oversized_rejects_without_polling(
    redis_client: Redis, prefix: str, streaming: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForbiddenModelIO:
        def __init__(self) -> None:
            self.secret_calls = 0
            self.complete_calls = 0
            self.stream_calls = 0

        async def resolve(self, secret_ref: str) -> str:
            self.secret_calls += 1
            raise AssertionError("oversized admission must not resolve credentials")

        async def complete(
            self, deployment: Deployment, request: ModelRequest, api_key: str,
        ) -> ModelResponse:
            self.complete_calls += 1
            raise AssertionError("oversized admission must not invoke a model")

        def stream_openai_compatible_chunks(
            self, deployment: Deployment, request: ModelRequest, api_key: str,
        ) -> AsyncIterator[object]:
            self.stream_calls += 1
            raise AssertionError("oversized admission must not invoke a model stream")

    selected = deployment("selected", "scope", tpm=100)
    model_request = ModelRequest(
        logical_model=selected.logical_model,
        messages=(ModelMessage(role="user", content="offline token reservation check"),),
        max_output_tokens=4096, timeout_seconds=5, allow_fallback=False,
    )
    estimator = ConservativeTokenEstimator()
    assert estimator.estimate(model_request) > 100
    root = CapacityPool(redis_client, deployments=[selected],
                        credentials=credentials("selected"), key_prefix=prefix)
    model_io = ForbiddenModelIO()
    gateway = ModelGateway(ModelRegistry([selected]), root, model_io, model_io,
                           token_estimator=estimator, capacity_wait_timeout=5)

    async def no_poll(deadline: float) -> None:
        raise AssertionError("an impossible request must not enter the polling loop")

    monkeypatch.setattr(root, "_wait_for_next_poll", no_poll)
    async with asyncio.timeout(2):
        with pytest.raises(CapacityRequestTooLarge):
            if streaming:
                _events = [event async for event in gateway.stream_openai_compatible_events(
                    model_request)]
            else:
                await gateway.complete_with_context(model_request)
    assert model_io.secret_calls == model_io.complete_calls == model_io.stream_calls == 0
    assert root._waiters == 0
    keys = root._keys(selected.quota_scope_id)
    assert await redis_client.zcard(keys["leases"]) == 0
    assert not await redis_client.exists(keys["rpm"], keys["tpm"])
