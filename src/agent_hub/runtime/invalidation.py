from __future__ import annotations

import json
import logging
from collections import deque
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

RUNTIME_CONFIG_INVALIDATION_CHANNEL = "agent-hub:runtime-config-invalidations"
_LOGGER = logging.getLogger(__name__)


class RuntimeConfigInvalidationTarget(StrEnum):
    MCP = "mcp"
    PLUGIN = "plugin"
    ALL = "all"


class ReloadableRuntime(Protocol):
    async def reload(self, tenant_id: UUID | None = None) -> None: ...


class RedisInvalidationClient(Protocol):
    async def publish(self, channel: str, payload: str) -> object: ...

    def pubsub(self) -> Any: ...


class RuntimeConfigInvalidationBus:
    def __init__(
        self,
        redis_client: RedisInvalidationClient,
        *,
        channel: str = RUNTIME_CONFIG_INVALIDATION_CHANNEL,
        source_instance_id: str | None = None,
        max_seen_event_ids: int = 1024,
    ) -> None:
        if type(max_seen_event_ids) is not int or max_seen_event_ids < 1:
            raise ValueError("max_seen_event_ids must be a positive integer")
        if source_instance_id is not None and _optional_safe_identifier(source_instance_id) is None:
            raise ValueError(
                "source_instance_id must be 1-128 characters and contain only "
                "letters, numbers, dashes, underscores, or dots"
            )
        self._redis = redis_client
        self._channel = channel
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
        await self._redis.publish(self._channel, payload)

    async def listen(
        self,
        *,
        mcp_runtime: ReloadableRuntime | None = None,
        plugin_runtime: ReloadableRuntime | None = None,
        max_messages: int | None = None,
    ) -> None:
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
        return True

    async def _reload_target(
        self,
        tenant_id: UUID,
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


async def _reload_runtime(
    runtime: ReloadableRuntime | None,
    tenant_id: UUID,
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
