from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from agent_hub.channels.base import InboundMessage
from agent_hub.channels.directives import (
    apply_channel_command_aliases,
    is_channel_help_request,
    parse_command_aliases,
)
from agent_hub.channels.feishu.normalize import (
    UnsupportedFeishuEvent,
    normalize_feishu_event,
)
from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.channels.feishu.verify import FeishuVerifier
from agent_hub.channels.feishu.webhook import ChannelGatewayProtocol


class FeishuWebSocketClient(Protocol):
    def events(self) -> AsyncIterator[dict[str, object]]: ...


FeishuWebSocketClientFactory = Callable[[], Awaitable[FeishuWebSocketClient]]
FeishuWebSocketSubmissionHandler = Callable[[InboundMessage, object], Awaitable[None]]
FeishuWebSocketHelpHandler = Callable[[InboundMessage], Awaitable[None]]
CredentialRefresh = Callable[[], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class FeishuWebSocketMetrics:
    connection_attempts: int = 0
    reconnects: int = 0
    received_events: int = 0
    submitted_messages: int = 0
    ignored_events: int = 0
    failures: int = 0
    last_error_type: str | None = None
    last_error_message: str | None = None


class FeishuWebSocketReceiver:
    def __init__(
        self,
        *,
        verifier: FeishuVerifier,
        gateway: ChannelGatewayProtocol | None,
        bot_open_id: str | None,
        submission_handler: FeishuWebSocketSubmissionHandler | None = None,
        command_aliases: dict[str, str] | None = None,
        help_handler: FeishuWebSocketHelpHandler | None = None,
    ) -> None:
        self._verifier = verifier
        self._gateway = gateway
        self._bot_open_id = bot_open_id
        self._submission_handler = submission_handler
        self._command_aliases = command_aliases or {}
        self._help_handler = help_handler

    async def receive(self, payload: dict[str, object]) -> InboundMessage | None:
        verified = self._verifier.verify_payload(payload)
        if verified.challenge is not None:
            return None
        try:
            message = normalize_feishu_event(verified.payload, bot_open_id=self._bot_open_id)
        except UnsupportedFeishuEvent:
            return None
        normalized_text = apply_channel_command_aliases(message.text, self._command_aliases)
        if normalized_text != message.text:
            message = message.model_copy(update={"text": normalized_text})
        if is_channel_help_request(message.text, self._command_aliases):
            if self._help_handler is not None:
                await self._help_handler(message)
            return message
        if self._gateway is not None:
            submission = await self._gateway.handle(message)
            if self._submission_handler is not None:
                await self._submission_handler(message, submission)
        return message


class FeishuWebSocketConnector:
    def __init__(
        self,
        *,
        receiver: FeishuWebSocketReceiver,
        client_factory: FeishuWebSocketClientFactory,
        credential_refresh: CredentialRefresh | None = None,
        reconnect_min_seconds: float = 0.1,
        reconnect_max_seconds: float = 5.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._receiver = receiver
        self._client_factory = client_factory
        self._credential_refresh = credential_refresh
        self._reconnect_min_seconds = reconnect_min_seconds
        self._reconnect_max_seconds = reconnect_max_seconds
        self._sleep = sleep
        self.metrics = FeishuWebSocketMetrics()
        self._stop = asyncio.Event()
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def request_shutdown(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            self.metrics.connection_attempts += 1
            if self._credential_refresh is not None:
                await self._credential_refresh()
            try:
                client = await self._client_factory()
                self._ready = True
                async for payload in client.events():
                    if self._stop.is_set():
                        break
                    self.metrics.received_events += 1
                    message = await self._receiver.receive(payload)
                    if message is None:
                        self.metrics.ignored_events += 1
                    else:
                        self.metrics.submitted_messages += 1
            except asyncio.CancelledError:
                self._ready = False
                raise
            except Exception as error:  # noqa: BLE001 -- connector isolates transient network failures.
                self.metrics.failures += 1
                self.metrics.last_error_type = type(error).__name__
                self.metrics.last_error_message = _bounded_error_message(error)
            finally:
                self._ready = False
            if not self._stop.is_set():
                self.metrics.reconnects += 1
                await self._sleep(self._next_delay())

    def _next_delay(self) -> float:
        if self._reconnect_max_seconds <= self._reconnect_min_seconds:
            return self._reconnect_min_seconds
        return random.uniform(self._reconnect_min_seconds, self._reconnect_max_seconds)


def _bounded_error_message(error: Exception) -> str:
    message = str(error).strip() or type(error).__name__
    if len(message) <= 256:
        return message
    return message[:253] + "..."


def build_feishu_websocket_receiver(
    settings: FeishuSettings,
    *,
    gateway: ChannelGatewayProtocol | None,
    submission_handler: FeishuWebSocketSubmissionHandler | None = None,
    help_handler: FeishuWebSocketHelpHandler | None = None,
    clock: Callable[[], float] | None = None,
) -> FeishuWebSocketReceiver:
    verifier = FeishuVerifier(
        app_id=settings.app_id,
        verification_token=settings.verification_token_value(),
        encrypt_key=settings.encrypt_key_value(),
        allowed_tenant_keys=settings.allowed_tenant_keys,
        require_verification_token=False,
        timestamp_tolerance_seconds=settings.timestamp_tolerance_seconds,
        clock=clock,
    )
    return FeishuWebSocketReceiver(
        verifier=verifier,
        gateway=gateway,
        bot_open_id=settings.bot_open_id,
        submission_handler=submission_handler,
        command_aliases=parse_command_aliases(settings.command_aliases),
        help_handler=help_handler,
    )


__all__ = [
    "CredentialRefresh",
    "FeishuWebSocketClient",
    "FeishuWebSocketClientFactory",
    "FeishuWebSocketConnector",
    "FeishuWebSocketHelpHandler",
    "FeishuWebSocketMetrics",
    "FeishuWebSocketReceiver",
    "FeishuWebSocketSubmissionHandler",
    "build_feishu_websocket_receiver",
]
