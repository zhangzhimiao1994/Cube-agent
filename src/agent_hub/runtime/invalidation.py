from __future__ import annotations

import json
import logging
from collections import deque
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

RUNTIME_CONFIG_INVALIDATION_CHANNEL = "agent-hub:runtime-config-invalidations"
RUNTIME_CONFIG_INVALIDATION_STREAM = "agent-hub:runtime-config-invalidations:stream"
_LOGGER = logging.getLogger(__name__)


class RuntimeConfigInvalidationTarget(StrEnum):
    MCP = "mcp"
    PLUGIN = "plugin"
    ALL = "all"


class ReloadableRuntime(Protocol):
    async def reload(self, tenant_id: UUID | None = None) -> None: ...


class RedisInvalidationClient(Protocol):
    async def xgroup_create(
        self,
        stream: str,
        groupname: str,
        id: str = "0-0",
        *,
        mkstream: bool = False,
    ) -> object: ...

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        *,
        count: int | None = None,
        block: int | None = None,
    ) -> object: ...

    async def xack(self, stream: str, groupname: str, *ids: str) -> object: ...

    async def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> object: ...

    async def publish(self, channel: str, payload: str) -> object: ...

    def pubsub(self) -> Any: ...


class RuntimeConfigInvalidationBus:
    def __init__(
        self,
        redis_client: RedisInvalidationClient,
        *,
        channel: str = RUNTIME_CONFIG_INVALIDATION_CHANNEL,
        stream: str | None = RUNTIME_CONFIG_INVALIDATION_STREAM,
        stream_maxlen: int = 4096,
        source_instance_id: str | None = None,
        max_seen_event_ids: int = 1024,
    ) -> None:
        if type(max_seen_event_ids) is not int or max_seen_event_ids < 1:
            raise ValueError("max_seen_event_ids must be a positive integer")
        if stream is not None and _optional_safe_stream_name(stream) is None:
            raise ValueError("stream must be a safe Redis stream name or None")
        if type(stream_maxlen) is not int or stream_maxlen < 1:
            raise ValueError("stream_maxlen must be a positive integer")
        if source_instance_id is not None and _optional_safe_identifier(source_instance_id) is None:
            raise ValueError(
                "source_instance_id must be 1-128 characters and contain only "
                "letters, numbers, dashes, underscores, or dots"
            )
        self._redis = redis_client
        self._channel = channel
        self._stream = stream
        self._stream_maxlen = stream_maxlen
        self._source_instance_id = source_instance_id or str(uuid4())
        self._max_seen_event_ids = max_seen_event_ids

    async def publish(
        self,
        tenant_id: UUID,
        target: RuntimeConfigInvalidationTarget,
    ) -> None:
        payload = json.dumps(
            {
                "event_id": str(uuid4()),
                "source_instance_id": self._source_instance_id,
                "target": target.value,
                "tenant_id": str(tenant_id),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        if self._stream is not None:
            await self._redis.xadd(
                self._stream,
                {"payload": payload},
                maxlen=self._stream_maxlen,
                approximate=True,
            )
        await self._redis.publish(self._channel, payload)

    async def listen(
        self,
        *,
        mcp_runtime: ReloadableRuntime | None = None,
        plugin_runtime: ReloadableRuntime | None = None,
        max_messages: int | None = None,
        stream_consumer_group: str | None = None,
        stream_consumer_name: str | None = None,
        max_stream_replay_messages: int = 128,
        reload_on_empty_stream_replay: bool = False,
    ) -> None:
        if stream_consumer_group is not None:
            replayed = await self._replay_stream(
                mcp_runtime=mcp_runtime,
                plugin_runtime=plugin_runtime,
                stream_consumer_group=stream_consumer_group,
                stream_consumer_name=stream_consumer_name,
                max_stream_replay_messages=max_stream_replay_messages,
            )
            if not replayed and reload_on_empty_stream_replay:
                await self._reload_target(
                    None,
                    RuntimeConfigInvalidationTarget.ALL,
                    mcp_runtime=mcp_runtime,
                    plugin_runtime=plugin_runtime,
                )
        pubsub = self._redis.pubsub()
        handled = 0
        seen_event_ids: set[str] = set()
        seen_event_order: deque[str] = deque()
        try:
            await pubsub.subscribe(self._channel)
            async for message in pubsub.listen():
                if not await self._handle_message(
                    message,
                    mcp_runtime=mcp_runtime,
                    plugin_runtime=plugin_runtime,
                    seen_event_ids=seen_event_ids,
                    seen_event_order=seen_event_order,
                ):
                    continue
                handled += 1
                if max_messages is not None and handled >= max_messages:
                    return
        finally:
            close = getattr(pubsub, "aclose", None)
            if callable(close):
                await close()

    async def _replay_stream(
        self,
        *,
        mcp_runtime: ReloadableRuntime | None,
        plugin_runtime: ReloadableRuntime | None,
        stream_consumer_group: str,
        stream_consumer_name: str | None,
        max_stream_replay_messages: int,
    ) -> bool:
        if self._stream is None:
            return False
        if _optional_safe_identifier(stream_consumer_group) is None:
            raise ValueError("stream_consumer_group must be a safe identifier")
        consumer_name = stream_consumer_name or self._source_instance_id
        if _optional_safe_identifier(consumer_name) is None:
            raise ValueError("stream_consumer_name must be a safe identifier")
        if type(max_stream_replay_messages) is not int or max_stream_replay_messages < 1:
            raise ValueError("max_stream_replay_messages must be a positive integer")
        try:
            await self._redis.xgroup_create(
                self._stream,
                stream_consumer_group,
                "0-0",
                mkstream=True,
            )
        except Exception as error:  # noqa: BLE001 - Redis reports existing groups by exception.
            _LOGGER.debug(
                "runtime_config_invalidation_stream_group_create_skipped error_type=%s",
                type(error).__name__,
            )
        seen_event_ids: set[str] = set()
        seen_event_order: deque[str] = deque()
        replayed = False
        failed_replay = False
        while True:
            raw_entries = await self._redis.xreadgroup(
                stream_consumer_group,
                consumer_name,
                {self._stream: ">"},
                count=max_stream_replay_messages,
                block=0,
            )
            entries_read, handled, failed = await self._handle_stream_replay_entries(
                raw_entries,
                mcp_runtime=mcp_runtime,
                plugin_runtime=plugin_runtime,
                stream_consumer_group=stream_consumer_group,
                seen_event_ids=seen_event_ids,
                seen_event_order=seen_event_order,
            )
            replayed = replayed or handled
            failed_replay = failed_replay or failed
            if entries_read < max_stream_replay_messages:
                break
        if failed_replay:
            raw_pending_entries = await self._redis.xreadgroup(
                stream_consumer_group,
                consumer_name,
                {self._stream: "0"},
                count=max_stream_replay_messages,
                block=0,
            )
            entries_read, handled, failed = await self._handle_stream_replay_entries(
                raw_pending_entries,
                mcp_runtime=mcp_runtime,
                plugin_runtime=plugin_runtime,
                stream_consumer_group=stream_consumer_group,
                seen_event_ids=seen_event_ids,
                seen_event_order=seen_event_order,
            )
            replayed = replayed or handled
            failed_replay = failed or entries_read == 0
        return replayed and not failed_replay

    async def _handle_stream_replay_entries(
        self,
        raw_entries: object,
        *,
        mcp_runtime: ReloadableRuntime | None,
        plugin_runtime: ReloadableRuntime | None,
        stream_consumer_group: str,
        seen_event_ids: set[str],
        seen_event_order: deque[str],
    ) -> tuple[int, bool, bool]:
        entries_read = 0
        handled_any = False
        failed_any = False
        for stream, entries in _stream_read_entries(raw_entries):
            entries_read += len(entries)
            acked: list[str] = []
            for entry_id, fields in entries:
                payload = fields.get("payload")
                if payload is None:
                    failed_any = True
                    continue
                handled = await self._handle_message(
                    {"type": "message", "data": payload},
                    mcp_runtime=mcp_runtime,
                    plugin_runtime=plugin_runtime,
                    seen_event_ids=seen_event_ids,
                    seen_event_order=seen_event_order,
                )
                if handled:
                    handled_any = True
                    acked.append(entry_id)
                else:
                    failed_any = True
            if acked:
                await self._redis.xack(stream, stream_consumer_group, *acked)
        return entries_read, handled_any, failed_any

    async def _handle_message(
        self,
        message: object,
        *,
        mcp_runtime: ReloadableRuntime | None,
        plugin_runtime: ReloadableRuntime | None,
        seen_event_ids: set[str],
        seen_event_order: deque[str],
    ) -> bool:
        if not isinstance(message, dict) or message.get("type") != "message":
            return False
        try:
            payload = _decode_payload(message.get("data"))
            tenant_id = UUID(str(payload["tenant_id"]))
            target = RuntimeConfigInvalidationTarget(str(payload["target"]))
            event_id = _optional_safe_identifier(payload.get("event_id"))
            source_instance_id = _optional_safe_identifier(payload.get("source_instance_id"))
        except Exception as error:  # noqa: BLE001 - bad invalidation messages are ignored.
            _LOGGER.warning(
                "runtime_config_invalidation_message_invalid error_type=%s",
                type(error).__name__,
            )
            return False
        if source_instance_id == self._source_instance_id:
            return True
        if event_id is not None and event_id in seen_event_ids:
            return True
        reloaded = await self._reload_target(
            tenant_id,
            target,
            mcp_runtime=mcp_runtime,
            plugin_runtime=plugin_runtime,
        )
        if event_id is not None and reloaded:
            _remember_seen_event_id(
                event_id,
                seen_event_ids,
                seen_event_order,
                max_seen_event_ids=self._max_seen_event_ids,
            )
        return reloaded

    async def _reload_target(
        self,
        tenant_id: UUID | None,
        target: RuntimeConfigInvalidationTarget,
        *,
        mcp_runtime: ReloadableRuntime | None,
        plugin_runtime: ReloadableRuntime | None,
    ) -> bool:
        reloaded = True
        if target in {RuntimeConfigInvalidationTarget.MCP, RuntimeConfigInvalidationTarget.ALL}:
            reloaded = await _reload_runtime(mcp_runtime, tenant_id, "mcp") and reloaded
        if target in {RuntimeConfigInvalidationTarget.PLUGIN, RuntimeConfigInvalidationTarget.ALL}:
            reloaded = await _reload_runtime(plugin_runtime, tenant_id, "plugin") and reloaded
        return reloaded


def _decode_payload(data: object) -> dict[str, object]:
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    if not isinstance(data, str):
        raise TypeError("runtime invalidation payload must be text")
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise TypeError("runtime invalidation payload must be an object")
    return payload


def _optional_safe_identifier(value: object) -> str | None:
    if type(value) is not str:
        return None
    if 1 <= len(value) <= 128 and all(
        character.isalnum() or character in {"-", "_", "."} for character in value
    ):
        return value
    return None


def _optional_safe_stream_name(value: object) -> str | None:
    if type(value) is not str:
        return None
    if not 1 <= len(value) <= 256:
        return None
    if any(character.isspace() or ord(character) < 33 for character in value):
        return None
    return value


def _stream_read_entries(
    raw_entries: object,
) -> tuple[tuple[str, tuple[tuple[str, dict[str, str]], ...]], ...]:
    if not isinstance(raw_entries, list | tuple):
        return ()
    streams: list[tuple[str, tuple[tuple[str, dict[str, str]], ...]]] = []
    for raw_stream in raw_entries:
        if not isinstance(raw_stream, list | tuple) or len(raw_stream) != 2:
            continue
        stream_name, raw_messages = raw_stream
        if isinstance(stream_name, bytes):
            stream_name = stream_name.decode("utf-8", "replace")
        if not isinstance(stream_name, str) or not isinstance(raw_messages, list | tuple):
            continue
        messages: list[tuple[str, dict[str, str]]] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, list | tuple) or len(raw_message) != 2:
                continue
            entry_id, raw_fields = raw_message
            if isinstance(entry_id, bytes):
                entry_id = entry_id.decode("utf-8", "replace")
            if not isinstance(entry_id, str) or not isinstance(raw_fields, dict):
                continue
            fields: dict[str, str] = {}
            for key, value in raw_fields.items():
                if isinstance(key, bytes):
                    key = key.decode("utf-8", "replace")
                if isinstance(value, bytes):
                    value = value.decode("utf-8", "replace")
                if isinstance(key, str) and isinstance(value, str):
                    fields[key] = value
            messages.append((entry_id, fields))
        streams.append((stream_name, tuple(messages)))
    return tuple(streams)


async def _reload_runtime(
    runtime: ReloadableRuntime | None,
    tenant_id: UUID | None,
    runtime_name: str,
) -> bool:
    if runtime is None:
        return True
    try:
        await runtime.reload(tenant_id)
        return True
    except Exception as error:  # noqa: BLE001 - invalidation listeners must keep running.
        _LOGGER.warning(
            "runtime_config_invalidation_reload_failed runtime=%s tenant_id=%s error_type=%s",
            runtime_name,
            tenant_id,
            type(error).__name__,
        )
        return False


def _remember_seen_event_id(
    event_id: str,
    seen_event_ids: set[str],
    seen_event_order: deque[str],
    *,
    max_seen_event_ids: int,
) -> None:
    seen_event_ids.add(event_id)
    seen_event_order.append(event_id)
    while len(seen_event_order) > max_seen_event_ids:
        expired = seen_event_order.popleft()
        seen_event_ids.discard(expired)
