from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

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
    ) -> None:
        self._redis = redis_client
        self._channel = channel

    async def publish(
        self,
        tenant_id: UUID,
        target: RuntimeConfigInvalidationTarget,
    ) -> None:
        payload = json.dumps(
            {"tenant_id": str(tenant_id), "target": target.value},
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
        try:
            await pubsub.subscribe(self._channel)
            async for message in pubsub.listen():
                if not await self._handle_message(
                    message,
                    mcp_runtime=mcp_runtime,
                    plugin_runtime=plugin_runtime,
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
    ) -> bool:
        if not isinstance(message, dict) or message.get("type") != "message":
            return False
        try:
            payload = _decode_payload(message.get("data"))
            tenant_id = UUID(str(payload["tenant_id"]))
            target = RuntimeConfigInvalidationTarget(str(payload["target"]))
        except Exception as error:  # noqa: BLE001 - bad invalidation messages are ignored.
            _LOGGER.warning(
                "runtime_config_invalidation_message_invalid error_type=%s",
                type(error).__name__,
            )
            return False
        await self._reload_target(
            tenant_id,
            target,
            mcp_runtime=mcp_runtime,
            plugin_runtime=plugin_runtime,
        )
        return True

    async def _reload_target(
        self,
        tenant_id: UUID,
        target: RuntimeConfigInvalidationTarget,
        *,
        mcp_runtime: ReloadableRuntime | None,
        plugin_runtime: ReloadableRuntime | None,
    ) -> None:
        if target in {RuntimeConfigInvalidationTarget.MCP, RuntimeConfigInvalidationTarget.ALL}:
            await _reload_runtime(mcp_runtime, tenant_id, "mcp")
        if target in {RuntimeConfigInvalidationTarget.PLUGIN, RuntimeConfigInvalidationTarget.ALL}:
            await _reload_runtime(plugin_runtime, tenant_id, "plugin")


def _decode_payload(data: object) -> dict[str, object]:
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    if not isinstance(data, str):
        raise TypeError("runtime invalidation payload must be text")
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise TypeError("runtime invalidation payload must be an object")
    return payload


async def _reload_runtime(
    runtime: ReloadableRuntime | None,
    tenant_id: UUID,
    runtime_name: str,
) -> None:
    if runtime is None:
        return
    try:
        await runtime.reload(tenant_id)
    except Exception as error:  # noqa: BLE001 - invalidation listeners must keep running.
        _LOGGER.warning(
            "runtime_config_invalidation_reload_failed runtime=%s tenant_id=%s error_type=%s",
            runtime_name,
            tenant_id,
            type(error).__name__,
        )
