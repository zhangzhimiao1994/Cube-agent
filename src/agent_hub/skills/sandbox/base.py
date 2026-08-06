from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

JsonValue = object
_EXECUTION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


class SkillInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    execution_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    package_path: Path
    package_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    input: dict[str, object] = Field(default_factory=dict)
    timeout_seconds: int = Field(ge=1, le=3600)
    output_limit_bytes: int = Field(ge=1, le=10_000_000)
    memory_limit_bytes: int = Field(ge=16 * 1024 * 1024, le=16 * 1024 * 1024 * 1024)
    cpu_quota_percent: int = Field(ge=1, le=1000)
    network_allowlist: tuple[str, ...] = Field(default_factory=tuple)
    read_only_inputs: tuple[Path, ...] = Field(default_factory=tuple)
    writable_tmp_path: Path
    selected_secret_refs: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("input", mode="before")
    @classmethod
    def validate_input_json(cls, value: object) -> object:
        return _validate_json(value)

    @field_validator("network_allowlist", "read_only_inputs", "selected_secret_refs", mode="before")
    @classmethod
    def coerce_tuple_fields(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("network_allowlist", "selected_secret_refs")
    @classmethod
    def validate_printable_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for item in value:
            if item != item.strip() or not item or any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
                raise ValueError("values must be non-empty unpadded printable text")
            if item in seen:
                raise ValueError("values must be unique")
            seen.add(item)
        return value


class SkillResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


class SkillSandbox(Protocol):
    async def run(self, invocation: SkillInvocation) -> SkillResult: ...

    async def terminate(self, execution_id: str) -> None: ...


async def run_subprocess_with_limits(
    argv: tuple[str, ...],
    invocation: SkillInvocation,
    *,
    process_started: Callable[[asyncio.subprocess.Process], None] | None = None,
    on_forced_terminate: Callable[[], Awaitable[None]] | None = None,
) -> SkillResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if process_started is not None:
        process_started(process)
    cleanup_lock = asyncio.Lock()
    cleanup_called = False

    async def force_terminate() -> None:
        nonlocal cleanup_called
        async with cleanup_lock:
            if cleanup_called:
                return
            cleanup_called = True
            if on_forced_terminate is not None:
                await on_forced_terminate()
            if process.returncode is None:
                process.kill()

    stdin = json.dumps(invocation.input, sort_keys=True, separators=(",", ":")).encode()
    if process.stdin is not None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            process.stdin.write(stdin)
            await process.stdin.drain()
        process.stdin.close()
    stdout_task = asyncio.create_task(
        _read_limited(process.stdout, invocation.output_limit_bytes, force_terminate)
    )
    stderr_task = asyncio.create_task(
        _read_limited(process.stderr, invocation.output_limit_bytes, force_terminate)
    )
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=invocation.timeout_seconds)
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        return SkillResult(
            exit_code=returncode,
            stdout=decode_bounded(stdout, invocation.output_limit_bytes),
            stderr=decode_bounded(stderr, invocation.output_limit_bytes),
            timed_out=False,
        )
    except TimeoutError:
        await force_terminate()
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        return SkillResult(
            exit_code=None,
            stdout=decode_bounded(stdout, invocation.output_limit_bytes),
            stderr=decode_bounded(stderr, invocation.output_limit_bytes),
            timed_out=True,
        )


def decode_bounded(value: bytes, limit: int) -> str:
    clipped = value[:limit]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError as exc:
            clipped = clipped[: exc.start]
    return ""


async def _read_limited(
    stream: asyncio.StreamReader | None,
    limit: int,
    force_terminate: Callable[[], Awaitable[None]],
) -> bytes:
    if stream is None:
        return b""
    buffer = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        remaining = limit - len(buffer)
        if remaining > 0:
            buffer.extend(chunk[:remaining])
        if len(buffer) >= limit:
            await force_terminate()
    return bytes(buffer)


def validate_execution_id(value: str) -> str:
    if _EXECUTION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("execution_id must be a safe lowercase identifier")
    return value


def _validate_json(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, list):
        return [_validate_json(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            result[key] = _validate_json(item)
        return result
    raise ValueError("input must be JSON-compatible")
