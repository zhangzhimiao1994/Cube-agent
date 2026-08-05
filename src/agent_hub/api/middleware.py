"""Small security-focused ASGI middleware."""

import logging
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_hub.api.errors import error_payload

_AUTH_BODY_LIMIT = 16 * 1024
_CONFIG_BODY_LIMIT = 1024 * 1024
_LOGGER = logging.getLogger(__name__)


class SafeExceptionMiddleware:
    """Turn unexpected request failures into a safe response inside Starlette's boundary."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False
        response_completed = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started, response_completed
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_completed = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception as error:  # noqa: BLE001 -- this is the outer request safety boundary.
            error_id = str(uuid4())
            _LOGGER.error(
                "unhandled_request_error error_id=%s error_type=%s response_started=%s",
                error_id,
                type(error).__name__,
                response_started,
            )
            if response_started:
                if not response_completed:
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"",
                            "more_body": False,
                        }
                    )
                return
            response = JSONResponse(
                status_code=500,
                content=error_payload("internal_error", "internal server error"),
                headers={"X-Error-ID": error_id},
            )
            await response(scope, receive, send)


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = _body_limit(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return
        content_length = _content_length(scope)
        if content_length is not None and content_length > limit:
            await _too_large(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                await self.app(scope, _single_message_receive(message), send)
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                await _too_large(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        buffered: Message = {
            "type": "http.request",
            "body": bytes(body),
            "more_body": False,
        }
        await self.app(scope, _single_message_receive(buffered), send)


def _body_limit(scope: Scope) -> int | None:
    if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
        return None
    path = str(scope.get("path", ""))
    if path in {"/api/v1/setup", "/api/v1/auth/login"}:
        return _AUTH_BODY_LIMIT
    if path.startswith("/api/v1/config/"):
        return _CONFIG_BODY_LIMIT
    return None


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name.lower() != b"content-length":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _single_message_receive(message: Message) -> Receive:
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return message
        return {"type": "http.disconnect"}

    return receive


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=413,
        content=error_payload("request_too_large", "request body is too large"),
    )
    await response(scope, receive, send)
