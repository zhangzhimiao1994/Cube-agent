from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete, drain_and_reap_process

JsonValue = object
_EXECUTION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
SandboxProfile = Literal["none", "read_only", "restricted", "workspace_write"]
SANDBOX_PROFILES = frozenset({"none", "read_only", "restricted", "workspace_write"})
_TERMINATION_GRACE_SECONDS = 10.0


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
    sandbox_profile: SandboxProfile = "workspace_write"
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


class ProcessLimits(Protocol):
    @property
    def input(self) -> Mapping[str, object]: ...

    @property
    def timeout_seconds(self) -> int | float: ...

    @property
    def output_limit_bytes(self) -> int: ...


async def run_subprocess_with_limits(
    argv: tuple[str, ...],
    invocation: ProcessLimits,
    *,
    stdin_payload: bytes | None = None,
    process_started: Callable[[asyncio.subprocess.Process], None] | None = None,
    on_forced_terminate: Callable[[], Awaitable[None]] | None = None,
    termination_grace_seconds: float = _TERMINATION_GRACE_SECONDS,
) -> SkillResult:
    try:
        valid_grace = (type(termination_grace_seconds) in {int, float}
                       and math.isfinite(termination_grace_seconds) and termination_grace_seconds > 0)
    except OverflowError:
        valid_grace = False
    if not valid_grace:
        raise ValueError("invalid backend cleanup grace")
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    termination_task: asyncio.Task[None] | None = None
    io_tasks: list[asyncio.Task[bytes] | asyncio.Task[int] | asyncio.Task[None]] = []
    stdout = bytearray()
    stderr = bytearray()

    async def terminate() -> None:
        try:
            if on_forced_terminate is not None:
                try:
                    await asyncio.wait_for(
                        on_forced_terminate(), timeout=termination_grace_seconds,
                    )
                except TimeoutError as error:
                    raise ProcessCleanupIncomplete(
                        "Sandbox backend termination could not be confirmed before deadline"
                    ) from error
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()

    async def force_terminate() -> None:
        nonlocal termination_task
        if termination_task is None:
            termination_task = asyncio.create_task(terminate())
        # Both readers share the actual outcome, including a failed backend cleanup.
        await asyncio.shield(termination_task)

    async def write_stdin() -> None:
        stdin = (
            stdin_payload
            if stdin_payload is not None
            else json.dumps(invocation.input, sort_keys=True, separators=(",", ":")).encode()
        )
        if process.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.write(stdin)
                await process.stdin.drain()
            process.stdin.close()

    async def cleanup() -> None:
        try:
            await force_terminate()
        finally:
            # Unix write-pipe abort is not idempotent after connection_lost.
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.transport.abort()
            for task in io_tasks:
                task.cancel()
            await asyncio.gather(*io_tasks, return_exceptions=True)
            await drain_and_reap_process(process)

    deadline = asyncio.timeout(invocation.timeout_seconds)
    timed_out = False
    error: BaseException | None = None
    try:
        async with deadline:
            if process_started is not None:
                process_started(process)
            io_tasks.append(
                asyncio.create_task(
                    _read_limited(
                        process.stdout, invocation.output_limit_bytes, force_terminate, stdout
                    )
                )
            )
            io_tasks.append(
                asyncio.create_task(
                    _read_limited(
                        process.stderr, invocation.output_limit_bytes, force_terminate, stderr
                    )
                )
            )
            io_tasks.append(asyncio.create_task(write_stdin()))
            io_tasks.append(asyncio.create_task(process.wait()))
            await asyncio.gather(*io_tasks)
    except TimeoutError as exc:
        if deadline.expired():
            timed_out = True
        else:
            error = exc
    except BaseException as exc:  # noqa: BLE001 - every failure must release owned resources
        error = exc

    if timed_out or error is not None:
        cleanup_task = asyncio.create_task(cleanup())
        # Repeated caller cancellation must not interrupt owned-resource recovery.
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                error = exc
            except BaseException:  # noqa: BLE001 - retrieve and propagate the task failure below
                break
        try:
            cleanup_task.result()
        except BaseException as cleanup_error:
            if error is not None and error is not cleanup_error:
                raise error from cleanup_error
            raise
        if error is not None:
            raise error

    return SkillResult(
        exit_code=None if timed_out else process.returncode,
        stdout=decode_bounded(bytes(stdout), invocation.output_limit_bytes),
        stderr=decode_bounded(bytes(stderr), invocation.output_limit_bytes),
        timed_out=timed_out,
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
    buffer: bytearray,
) -> bytes:
    if stream is None:
        return b""
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
