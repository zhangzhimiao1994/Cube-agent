import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace

import pytest

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityLease,
    CapacityQueueFull,
    CapacityUnavailable,
    CapacityWaitTimeout,
)
from agent_hub.models.gateway import ModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment
from tests.unit.models.test_capacity import InMemoryCapacityRedis, pool
from tests.unit.models.test_gateway import (
    CapacityStub,
    SecretStub,
    StreamingTransportStub,
    deployment,
    lease,
    request,
)


async def complete(gateway: ModelGateway, *, streaming: bool, timeout: float = 1) -> None:
    model_request = replace(request(allow_fallback=False), timeout_seconds=timeout)
    if streaming:
        events = [event async for event in gateway.stream_openai_compatible_events(model_request)]
        assert events
    else:
        assert (await gateway.complete(model_request)).text == "ok"


def gateway_for(
    capacity: CapacityStub, *, window: float = 0.05,
) -> tuple[ModelGateway, SecretStub, StreamingTransportStub]:
    secrets = SecretStub(capacity.events)
    transport = StreamingTransportStub(
        capacity.events, [{"choices": [{"delta": {"content": "ok"}}]}],
    )
    gateway = ModelGateway(
        ModelRegistry([deployment("selected"), deployment("unused", "backup")]),
        capacity, secrets, transport,
        fallbacks={"primary": "backup"}, capacity_wait_timeout=window,
    )
    return gateway, secrets, transport


@pytest.mark.parametrize("streaming", [False, True])
async def test_queue_window_is_soft_without_authorized_fallback(streaming: bool) -> None:
    capacity = CapacityStub([CapacityWaitTimeout("busy"), lease("selected")])
    gateway, secrets, transport = gateway_for(capacity)

    await complete(gateway, streaming=streaming)

    attempts = [
        event for event in capacity.events if isinstance(event, tuple) and event[0] == "acquire"
    ]
    assert len(attempts) == 2
    assert all(event[1] == ("selected",) for event in attempts)
    assert attempts[0][3] == attempts[1][3]
    assert secrets.references == ["secret://selected"]
    assert len(transport.stream_calls if streaming else transport.calls) == 1
    assert len(capacity.records) == len(capacity.releases) == 1


class WaitingCapacity(CapacityStub):
    def __init__(self) -> None:
        super().__init__([CapacityWaitTimeout("busy")])
        self.waiting = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def acquire(
        self, candidates: Sequence[Deployment], wait_timeout: float, *,
        estimated_tokens: int | Mapping[str, int],
    ) -> CapacityLease:
        if self.outcomes:
            return await super().acquire(
                candidates, wait_timeout, estimated_tokens=estimated_tokens,
            )
        self.waiting.set()
        try:
            await asyncio.Future()
        finally:
            self.cancelled.set()
        raise AssertionError("unreachable")


@pytest.mark.parametrize("streaming", [False, True])
async def test_soft_queue_wait_keeps_cancellation_hard(streaming: bool) -> None:
    capacity = WaitingCapacity()
    gateway, secrets, transport = gateway_for(capacity)
    pending = asyncio.create_task(complete(gateway, streaming=streaming))
    waiting = asyncio.create_task(capacity.waiting.wait())
    try:
        await asyncio.wait({pending, waiting}, return_when=asyncio.FIRST_COMPLETED)
        assert capacity.waiting.is_set()
        assert not pending.done()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert capacity.cancelled.is_set()
        assert not secrets.references and not transport.calls and not transport.stream_calls
        assert not capacity.records and not capacity.releases
    finally:
        for task in (pending, waiting):
            task.cancel()
        await asyncio.gather(pending, waiting, return_exceptions=True)


@pytest.mark.parametrize("streaming", [False, True])
async def test_soft_queue_wait_keeps_original_request_deadline(streaming: bool) -> None:
    capacity = WaitingCapacity()
    gateway, secrets, transport = gateway_for(capacity)

    with pytest.raises(CapacityUnavailable):
        await complete(gateway, streaming=streaming, timeout=0.15)

    assert capacity.waiting.is_set() and capacity.cancelled.is_set()
    assert not secrets.references and not transport.calls and not transport.stream_calls
    assert not capacity.records and not capacity.releases


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("error", [CapacityQueueFull("full"), CapacityBackendError("backend")])
async def test_soft_wait_does_not_retry_queue_full_or_backend_failure(
    streaming: bool, error: Exception,
) -> None:
    capacity = CapacityStub([error, lease("selected")])
    gateway, secrets, transport = gateway_for(capacity)

    with pytest.raises((CapacityUnavailable, CapacityBackendError)):
        await complete(gateway, streaming=streaming)

    attempts = [
        event for event in capacity.events if isinstance(event, tuple) and event[0] == "acquire"
    ]
    assert len(attempts) == 1
    assert not secrets.references and not transport.calls and not transport.stream_calls


@pytest.mark.parametrize("scoped", [False, True])
async def test_hung_acquire_backend_is_not_a_soft_queue_timeout(scoped: bool) -> None:
    redis = InMemoryCapacityRedis(block_first_acquire=True)
    selected = deployment("selected")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()

    async with asyncio.timeout(0.5):
        with pytest.raises(CapacityBackendError, match="backend unavailable"):
            await capacity.acquire([selected], wait_timeout=0.01, estimated_tokens=1)

    assert root._waiters == 0
    assert redis.acquire_calls == 1
