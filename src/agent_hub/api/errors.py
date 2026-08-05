"""Stable public API error envelopes."""

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


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
