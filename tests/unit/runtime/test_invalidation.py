import json
from uuid import UUID

import pytest

from agent_hub.runtime.invalidation import (
    RuntimeConfigInvalidationBus,
    RuntimeConfigInvalidationTarget,
)

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
OTHER_TENANT_ID = UUID("00000000-0000-4000-8000-000000000002")


class FakeRedis:
    def __init__(self, messages: list[dict[str, object]] | None = None) -> None:
        self.published: list[tuple[str, str]] = []
        self._messages = messages or []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1

    def pubsub(self) -> "FakePubSub":
        return FakePubSub(self._messages)


class FakePubSub:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = messages
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def listen(self) -> object:
        for message in self.messages:
            yield message

    async def aclose(self) -> None:
        self.closed = True


class Runtime:
    def __init__(self) -> None:
        self.reloaded: list[UUID | None] = []

    async def reload(self, tenant_id: UUID | None = None) -> None:
        self.reloaded.append(tenant_id)


@pytest.mark.asyncio
async def test_runtime_invalidation_bus_publishes_tenant_target_payload() -> None:
    redis = FakeRedis()
    bus = RuntimeConfigInvalidationBus(redis, channel="runtime:test")

    await bus.publish(TENANT_ID, RuntimeConfigInvalidationTarget.MCP)

    assert len(redis.published) == 1
    channel, payload = redis.published[0]
    assert channel == "runtime:test"
    assert json.loads(payload) == {
        "tenant_id": str(TENANT_ID),
        "target": "mcp",
    }


@pytest.mark.asyncio
async def test_runtime_invalidation_listener_reloads_target_tenant_runtime() -> None:
    redis = FakeRedis(
        [
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "mcp",
                    }
                ).encode(),
            },
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "plugin",
                    }
                ),
            },
        ]
    )
    bus = RuntimeConfigInvalidationBus(redis, channel="runtime:test")
    mcp_runtime = Runtime()
    plugin_runtime = Runtime()

    await bus.listen(
        mcp_runtime=mcp_runtime,
        plugin_runtime=plugin_runtime,
        max_messages=2,
    )

    assert mcp_runtime.reloaded == [OTHER_TENANT_ID]
    assert plugin_runtime.reloaded == [OTHER_TENANT_ID]


@pytest.mark.asyncio
async def test_runtime_invalidation_listener_reloads_all_targets() -> None:
    redis = FakeRedis(
        [
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "all",
                    }
                ),
            },
        ]
    )
    bus = RuntimeConfigInvalidationBus(redis, channel="runtime:test")
    mcp_runtime = Runtime()
    plugin_runtime = Runtime()

    await bus.listen(
        mcp_runtime=mcp_runtime,
        plugin_runtime=plugin_runtime,
        max_messages=1,
    )

    assert mcp_runtime.reloaded == [OTHER_TENANT_ID]
    assert plugin_runtime.reloaded == [OTHER_TENANT_ID]
