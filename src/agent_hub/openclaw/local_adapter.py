from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_hub.openclaw.executor import openclaw_command_allowed, run_openclaw_command


@dataclass(frozen=True, slots=True)
class OpenClawLocalAdapterConfig:
    token: str
    platform: str
    allowed_commands: Sequence[Sequence[str]]
    command_timeout_seconds: float = 15.0
    allowed_file_roots: Sequence[Path] = ()
    file_read_limit_bytes: int = 64_000

    def __post_init__(self) -> None:
        if self.platform not in {"linux", "windows", "macos"}:
            raise ValueError("OpenClaw adapter platform is invalid")
        if not self.token or self.token != self.token.strip():
            raise ValueError("OpenClaw adapter token must be nonblank and unpadded")
        for command in self.allowed_commands:
            if not command or any(not item or item != item.strip() for item in command):
                raise ValueError("OpenClaw adapter allowed commands must be bounded argv arrays")
        if self.file_read_limit_bytes < 1 or self.file_read_limit_bytes > 1_000_000:
            raise ValueError("OpenClaw adapter file read limit must be bounded")
        for root in self.allowed_file_roots:
            if not root.is_absolute():
                raise ValueError("OpenClaw adapter file roots must be absolute paths")


class LocalOpenClawExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=128)
    platform: str = Field(pattern=r"^(linux|windows|macos)$")
    kind: str = Field(pattern=r"^(server_command|desktop_action|screen_read|file_read)$")
    target: str = Field(min_length=1, max_length=256)
    argv: list[str] = Field(default_factory=list, max_length=32)
    risk_level: str = Field(default="medium", pattern=r"^(low|medium|high)$")
    reason: str = Field(min_length=1, max_length=2_000)
    session_id: str | None = Field(default=None, max_length=128)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str) or not item or item != item.strip():
                raise ValueError("argv items must be nonblank and unpadded")
            if any(ord(character) < 32 or ord(character) == 127 for character in item):
                raise ValueError("argv items must be printable")
        return value


def create_local_adapter_app(config: OpenClawLocalAdapterConfig) -> FastAPI:
    app = FastAPI(title="OpenClaw local adapter", version="0.1.0")

    @app.get("/v1/openclaw/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "platform": config.platform}

    @app.post("/v1/openclaw/execute")
    async def execute(
        body: LocalOpenClawExecuteRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        if not _authorized(authorization, config.token):
            return _error(401, "openclaw_adapter_unauthorized", "OpenClaw adapter token is invalid")
        if body.platform != config.platform:
            return _error(409, "openclaw_adapter_platform_mismatch", "OpenClaw adapter platform does not match request")

        if body.kind == "file_read" and not body.argv:
            return _execute_file_read(body.target, config)

        allowed_commands = [list(command) for command in config.allowed_commands]
        if not openclaw_command_allowed(body.argv, allowed_commands):
            return _error(403, "openclaw_adapter_command_denied", "OpenClaw adapter command is not allowlisted")
        result = await run_openclaw_command(body.argv, timeout_seconds=config.command_timeout_seconds)
        return JSONResponse(
            {
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "truncated": result.truncated,
            }
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, error: Exception) -> JSONResponse:
        del request, error
        return _error(500, "openclaw_adapter_error", "OpenClaw adapter failed")

    return app


def load_local_adapter_config_from_env() -> OpenClawLocalAdapterConfig:
    token = os.environ.get("OPENCLAW_ADAPTER_TOKEN", "")
    platform = os.environ.get("OPENCLAW_ADAPTER_PLATFORM", _default_platform())
    allowed_commands = _allowed_commands_from_env(os.environ.get("OPENCLAW_ADAPTER_ALLOWED_COMMANDS_JSON", "[]"))
    timeout = float(os.environ.get("OPENCLAW_ADAPTER_COMMAND_TIMEOUT_SECONDS", "15"))
    allowed_file_roots = _file_roots_from_env(os.environ.get("OPENCLAW_ADAPTER_ALLOWED_FILE_ROOTS_JSON", "[]"))
    file_read_limit = int(os.environ.get("OPENCLAW_ADAPTER_FILE_READ_LIMIT_BYTES", "64000"))
    return OpenClawLocalAdapterConfig(
        token=token,
        platform=platform,
        allowed_commands=allowed_commands,
        command_timeout_seconds=timeout,
        allowed_file_roots=allowed_file_roots,
        file_read_limit_bytes=file_read_limit,
    )


def main() -> None:
    import uvicorn

    host = os.environ.get("OPENCLAW_ADAPTER_HOST", "127.0.0.1")
    port = int(os.environ.get("OPENCLAW_ADAPTER_PORT", "8765"))
    uvicorn.run(create_local_adapter_app(load_local_adapter_config_from_env()), host=host, port=port)


def _allowed_commands_from_env(value: str) -> list[list[str]]:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise TypeError("OPENCLAW_ADAPTER_ALLOWED_COMMANDS_JSON must be a JSON array")
    commands: list[list[str]] = []
    for command in parsed:
        if not isinstance(command, list) or any(not isinstance(item, str) for item in command):
            raise ValueError("OPENCLAW_ADAPTER_ALLOWED_COMMANDS_JSON must contain argv arrays")
        commands.append(command)
    return commands


def _file_roots_from_env(value: str) -> list[Path]:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise TypeError("OPENCLAW_ADAPTER_ALLOWED_FILE_ROOTS_JSON must be a JSON array")
    roots: list[Path] = []
    for root in parsed:
        if not isinstance(root, str) or not root or root != root.strip():
            raise ValueError("OPENCLAW_ADAPTER_ALLOWED_FILE_ROOTS_JSON must contain nonblank paths")
        roots.append(Path(root).expanduser().resolve())
    return roots


def _execute_file_read(target: str, config: OpenClawLocalAdapterConfig) -> JSONResponse:
    if not config.allowed_file_roots:
        return _error(409, "openclaw_adapter_file_read_unavailable", "OpenClaw adapter file roots are not configured")
    target_path = Path(target).expanduser().resolve()
    if not any(_path_within_root(target_path, root) for root in config.allowed_file_roots):
        return _error(403, "openclaw_adapter_file_denied", "OpenClaw adapter file path is outside allowed roots")
    if not target_path.is_file():
        return _error(404, "openclaw_adapter_file_not_found", "OpenClaw adapter file target was not found")

    data = target_path.read_bytes()[: config.file_read_limit_bytes + 1]
    truncated = len(data) > config.file_read_limit_bytes
    if truncated:
        data = data[: config.file_read_limit_bytes]
    return JSONResponse(
        {
            "exit_code": 0,
            "stdout": data.decode("utf-8", errors="replace"),
            "stderr": "",
            "truncated": truncated,
        }
    )


def _path_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _default_platform() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _authorized(authorization: str | None, token: str) -> bool:
    if authorization is None or not authorization.startswith("Bearer "):
        return False
    provided = authorization.removeprefix("Bearer ")
    return secrets.compare_digest(provided, token)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


if __name__ == "__main__":
    main()


__all__ = [
    "LocalOpenClawExecuteRequest",
    "OpenClawLocalAdapterConfig",
    "create_local_adapter_app",
    "load_local_adapter_config_from_env",
]
