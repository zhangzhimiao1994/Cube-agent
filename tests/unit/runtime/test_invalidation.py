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
        self.stream_entries: list[tuple[str, dict[str, str], int | None, bool]] = []
        self.stream_replay_entries: list[tuple[str, dict[str, str]]] = []
        self.consumer_groups_created: list[tuple[str, str, str, bool]] = []
        self.stream_reads: list[tuple[str, str, dict[str, str], int | None]] = []
        self.acked: list[tuple[str, str, tuple[str, ...]]] = []
        self._messages = messages or []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1

    async def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        self.stream_entries.append((stream, fields, maxlen, approximate))
        return "1-0"

    async def xgroup_create(
        self,
        stream: str,
        groupname: str,
        id: str = "0-0",
        *,
        mkstream: bool = False,
    ) -> str:
        self.consumer_groups_created.append((stream, groupname, id, mkstream))
        return "OK"

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        *,
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        del block
        self.stream_reads.append((groupname, consumername, streams, count))
        return [("runtime:test:stream", self.stream_replay_entries[: count or None])]

    async def xack(self, stream: str, groupname: str, *ids: str) -> int:
        self.acked.append((stream, groupname, ids))
        return len(ids)

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


class FlakyRuntime(Runtime):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    async def reload(self, tenant_id: UUID | None = None) -> None:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("temporary reload failure")
        await super().reload(tenant_id)


@pytest.mark.asyncio
async def test_runtime_invalidation_bus_publishes_tenant_target_payload() -> None:
    redis = FakeRedis()
    bus = RuntimeConfigInvalidationBus(
        redis,
        channel="runtime:test",
        source_instance_id="api-1",
    )

    await bus.publish(TENANT_ID, RuntimeConfigInvalidationTarget.MCP)

    assert len(redis.published) == 1
    channel, payload = redis.published[0]
    assert channel == "runtime:test"
    decoded = json.loads(payload)
    assert decoded["tenant_id"] == str(TENANT_ID)
    assert decoded["target"] == "mcp"
    assert decoded["source_instance_id"] == "api-1"
    assert isinstance(decoded["event_id"], str)
    assert decoded["event_id"]


@pytest.mark.asyncio
async def test_runtime_invalidation_bus_records_durable_stream_entry() -> None:
    redis = FakeRedis()
    bus = RuntimeConfigInvalidationBus(
        redis,
        channel="runtime:test",
        stream="runtime:test:stream",
        source_instance_id="api-1",
    )

    await bus.publish(TENANT_ID, RuntimeConfigInvalidationTarget.PLUGIN)

    assert len(redis.stream_entries) == 1
    stream, fields, maxlen, approximate = redis.stream_entries[0]
    assert stream == "runtime:test:stream"
    assert maxlen == 4096
    assert approximate is True
    assert set(fields) == {"payload"}
    stream_payload = json.loads(fields["payload"])
    pubsub_payload = json.loads(redis.published[0][1])
    assert stream_payload == pubsub_payload
    assert stream_payload["target"] == "plugin"


@pytest.mark.asyncio
async def test_runtime_invalidation_listener_replays_stream_entries_before_pubsub() -> None:
    redis = FakeRedis()
    redis.stream_replay_entries = [
        (
            "1-0",
            {
                "payload": json.dumps(
                    {
                        "event_id": "event-1",
                        "source_instance_id": "api-2",
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "plugin",
                    }
                )
            },
        )
    ]
    bus = RuntimeConfigInvalidationBus(
        redis,
        channel="runtime:test",
        stream="runtime:test:stream",
        source_instance_id="worker-1",
    )
    plugin_runtime = Runtime()

    await bus.listen(
        plugin_runtime=plugin_runtime,
        stream_consumer_group="worker-runtime-1",
        stream_consumer_name="worker-1",
    )

    assert redis.consumer_groups_created == [
        ("runtime:test:stream", "worker-runtime-1", "0-0", True)
    ]
    assert redis.stream_reads == [
        ("worker-runtime-1", "worker-1", {"runtime:test:stream": ">"}, 128)
    ]
    assert plugin_runtime.reloaded == [OTHER_TENANT_ID]
    assert redis.acked == [("runtime:test:stream", "worker-runtime-1", ("1-0",))]


@pytest.mark.asyncio
async def test_runtime_invalidation_listener_keeps_legacy_messages_replayable() -> None:
    redis = FakeRedis(
        [
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "mcp",
                    }
                ),
            },
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "mcp",
                    }
                ),
            },
        ]
    )
    bus = RuntimeConfigInvalidationBus(redis, channel="runtime:test")
    mcp_runtime = Runtime()

    await bus.listen(mcp_runtime=mcp_runtime, max_messages=2)

    assert mcp_runtime.reloaded == [OTHER_TENANT_ID, OTHER_TENANT_ID]


@pytest.mark.asyncio
async def test_runtime_invalidation_listener_skips_self_originated_events() -> None:
    redis = FakeRedis(
        [
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "event_id": "event-1",
                        "source_instance_id": "api-1",
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "plugin",
                    }
                ),
            }
        ]
    )
    bus = RuntimeConfigInvalidationBus(
        redis,
        channel="runtime:test",
        source_instance_id="api-1",
    )
    plugin_runtime = Runtime()

    await bus.listen(plugin_runtime=plugin_runtime, max_messages=1)

    assert plugin_runtime.reloaded == []


@pytest.mark.asyncio
async def test_runtime_invalidation_listener_deduplicates_successful_event_ids() -> None:
    redis = FakeRedis(
        [
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "event_id": "event-1",
                        "source_instance_id": "api-2",
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "mcp",
                    }
                ),
            },
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "event_id": "event-1",
                        "source_instance_id": "api-2",
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "mcp",
                    }
                ),
            },
        ]
    )
    bus = RuntimeConfigInvalidationBus(
        redis,
        channel="runtime:test",
        source_instance_id="worker-1",
    )
    mcp_runtime = Runtime()

    await bus.listen(mcp_runtime=mcp_runtime, max_messages=2)

    assert mcp_runtime.reloaded == [OTHER_TENANT_ID]


@pytest.mark.asyncio
async def test_runtime_invalidation_listener_eviction_allows_old_event_id_again() -> None:
    redis = FakeRedis(
        [
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "event_id": "event-1",
                        "source_instance_id": "api-2",
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "mcp",
                    }
                ),
            },
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "event_id": "event-2",
                        "source_instance_id": "api-2",
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "mcp",
                    }
                ),
            },
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "event_id": "event-1",
                        "source_instance_id": "api-2",
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "mcp",
                    }
                ),
            },
        ]
    )
    bus = RuntimeConfigInvalidationBus(
        redis,
        channel="runtime:test",
        source_instance_id="worker-1",
        max_seen_event_ids=1,
    )
    mcp_runtime = Runtime()

    await bus.listen(mcp_runtime=mcp_runtime, max_messages=3)

    assert mcp_runtime.reloaded == [OTHER_TENANT_ID, OTHER_TENANT_ID, OTHER_TENANT_ID]


@pytest.mark.asyncio
async def test_runtime_invalidation_listener_retries_duplicate_event_after_reload_failure() -> None:
    redis = FakeRedis(
        [
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "event_id": "event-1",
                        "source_instance_id": "api-2",
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "plugin",
                    }
                ),
            },
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "event_id": "event-1",
                        "source_instance_id": "api-2",
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "plugin",
                    }
                ),
            },
        ]
    )
    bus = RuntimeConfigInvalidationBus(
        redis,
        channel="runtime:test",
        source_instance_id="worker-1",
    )
    plugin_runtime = FlakyRuntime()

    await bus.listen(plugin_runtime=plugin_runtime, max_messages=2)

    assert plugin_runtime.reloaded == [OTHER_TENANT_ID]


@pytest.mark.asyncio
async def test_runtime_invalidation_listener_keeps_distinct_event_ids_for_same_target() -> None:
    redis = FakeRedis(
        [
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "event_id": "event-1",
                        "source_instance_id": "api-2",
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "mcp",
                    }
                ),
            },
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "event_id": "event-2",
                        "source_instance_id": "api-2",
                        "tenant_id": str(OTHER_TENANT_ID),
                        "target": "mcp",
                    }
                ),
            },
        ]
    )
    bus = RuntimeConfigInvalidationBus(
        redis,
        channel="runtime:test",
        source_instance_id="worker-1",
    )
    mcp_runtime = Runtime()

    await bus.listen(mcp_runtime=mcp_runtime, max_messages=2)

    assert mcp_runtime.reloaded == [OTHER_TENANT_ID, OTHER_TENANT_ID]


@pytest.mark.asyncio
async def test_runtime_invalidation_bus_rejects_invalid_seen_event_capacity() -> None:
    with pytest.raises(ValueError, match="max_seen_event_ids"):
        RuntimeConfigInvalidationBus(FakeRedis(), max_seen_event_ids=0)


@pytest.mark.asyncio
async def test_runtime_invalidation_bus_rejects_invalid_source_instance_id() -> None:
    with pytest.raises(ValueError, match="source_instance_id"):
        RuntimeConfigInvalidationBus(FakeRedis(), source_instance_id="api:1")


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
