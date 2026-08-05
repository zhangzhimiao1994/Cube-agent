"""Small security-focused ASGI middleware."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_hub.api.errors import error_payload

_AUTH_BODY_LIMIT = 16 * 1024
_CONFIG_BODY_LIMIT = 1024 * 1024


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
