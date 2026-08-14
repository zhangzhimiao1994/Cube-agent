from __future__ import annotations

import asyncio
import importlib
import json
import logging
import threading
from collections.abc import AsyncIterator
from typing import Any, cast

from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.channels.feishu.websocket import FeishuWebSocketClient

_LOGGER = logging.getLogger(__name__)


class LarkOAPIFeishuWebSocketClient:
    """Adapt the official Feishu SDK WebSocket client to Agent Hub's async stream."""

    def __init__(self, settings: FeishuSettings, *, startup_timeout_seconds: float = 10.0) -> None:
        self._settings = settings
        self._startup_timeout_seconds = startup_timeout_seconds
        self._sdk_client: Any | None = None
        self._sdk_thread: threading.Thread | None = None

    async def events(self) -> AsyncIterator[dict[str, object]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, object] | BaseException | None] = asyncio.Queue()
        ready = threading.Event()

        def publish_event(event: object) -> None:
            try:
                sdk = importlib.import_module("lark_oapi")
                payload = json.loads(str(sdk.JSON.marshal(event)))
                if not isinstance(payload, dict):
                    raise TypeError("Feishu event payload must be an object")
            except Exception as error:  # noqa: BLE001 - isolate SDK callback boundary.
                loop.call_soon_threadsafe(queue.put_nowait, error)
                return
            loop.call_soon_threadsafe(queue.put_nowait, cast(dict[str, object], payload))

        def run_sdk() -> None:
            try:
                sdk = importlib.import_module("lark_oapi")
                handler = (
                    sdk.EventDispatcherHandler.builder("", "")
                    .register_p2_im_message_receive_v1(publish_event)
                    .build()
                )
                log_level = getattr(
                    sdk.LogLevel, "WARN", getattr(sdk.LogLevel, "ERROR", sdk.LogLevel.INFO)
                )
                self._sdk_client = sdk.ws.Client(
                    self._settings.app_id,
                    self._settings.app_secret_value(),
                    event_handler=handler,
                    log_level=log_level,
                )
                ready.set()
                self._sdk_client.start()
            except BaseException as error:  # noqa: BLE001 - surface SDK startup/runtime failure.
                ready.set()
                loop.call_soon_threadsafe(queue.put_nowait, error)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        thread = threading.Thread(target=run_sdk, name="feishu-websocket-sdk", daemon=True)
        self._sdk_thread = thread
        thread.start()
        await asyncio.to_thread(ready.wait, self._startup_timeout_seconds)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            await self._disconnect_best_effort()

    async def _disconnect_best_effort(self) -> None:
        client = self._sdk_client
        if client is None or not hasattr(client, "_disconnect"):
            return
        try:
            sdk_client_module = importlib.import_module("lark_oapi.ws.client")
            sdk_loop = getattr(sdk_client_module, "loop", None)
            current_loop = asyncio.get_running_loop()
            if sdk_loop is not None and sdk_loop.is_running() and sdk_loop is not current_loop:
                future = asyncio.run_coroutine_threadsafe(client._disconnect(), sdk_loop)
                await asyncio.to_thread(future.result, 3)
                if hasattr(sdk_loop, "call_soon_threadsafe"):
                    sdk_loop.call_soon_threadsafe(sdk_loop.stop)
                    await self._join_sdk_thread_best_effort()
            else:
                await client._disconnect()
        except Exception:
            _LOGGER.warning("feishu_websocket_sdk_disconnect_failed", exc_info=True)

    async def _join_sdk_thread_best_effort(self) -> None:
        thread = self._sdk_thread
        if thread is None or thread is threading.current_thread():
            return
        await asyncio.to_thread(thread.join, 3)


async def create_lark_oapi_feishu_websocket_client(
    settings: FeishuSettings,
) -> FeishuWebSocketClient:
    return LarkOAPIFeishuWebSocketClient(settings)


__all__ = ["LarkOAPIFeishuWebSocketClient", "create_lark_oapi_feishu_websocket_client"]
