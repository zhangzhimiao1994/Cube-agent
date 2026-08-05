"""Stable public API error envelopes."""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


ERROR_STATUS_CODES = (400, 401, 403, 404, 409, 413, 422, 429, 500, 503)
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status_code: {"model": ErrorResponse, "description": "Error response"}
    for status_code in ERROR_STATUS_CODES
}
_SAFE_HTTP_HEADERS = frozenset({"allow", "www-authenticate", "retry-after"})


def error_payload(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


class PublicAPIError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.public_message = message
        self.headers = headers
        super().__init__(code)


async def public_error_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    assert isinstance(error, PublicAPIError)
    return JSONResponse(
        status_code=error.status_code,
        content=error_payload(error.code, error.public_message),
        headers=error.headers,
    )


async def http_exception_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    assert isinstance(error, StarletteHTTPException)
    if error.status_code == 404:
        code, message = "not_found", "resource not found"
    elif error.status_code == 405:
        code, message = "method_not_allowed", "method not allowed"
    else:
        code, message = "http_error", "request failed"
    headers = {
        name: value
        for name, value in (error.headers or {}).items()
        if name.lower() in _SAFE_HTTP_HEADERS
    }
    return JSONResponse(
        status_code=error.status_code,
        content=error_payload(code, message),
        headers=headers,
    )


async def unhandled_exception_handler(
    request: Request, error: Exception
) -> JSONResponse:
    del request, error
    return JSONResponse(
        status_code=500,
        content=error_payload("internal_error", "internal server error"),
    )
