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
    details: dict[str, str] | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """Build explicit per-operation error response metadata."""

    return {
        status_code: {"model": ErrorResponse, "description": "Error response"}
        for status_code in status_codes
    }


BASE_ERROR_RESPONSES = error_responses(405, 500)
_SAFE_HTTP_HEADERS = frozenset({"allow", "www-authenticate", "retry-after"})


def error_payload(
    code: str,
    message: str,
    details: dict[str, str] | None = None,
) -> dict[str, dict[str, object]]:
    body: dict[str, object] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return {"error": body}


class PublicAPIError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.public_message = message
        self.details = details
        self.headers = headers
        super().__init__(code)


async def public_error_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    assert isinstance(error, PublicAPIError)
    return JSONResponse(
        status_code=error.status_code,
        content=error_payload(error.code, error.public_message, error.details),
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
