from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePath

_DENIED_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "dash",
    "fish",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "zsh",
}


@dataclass(frozen=True, slots=True)
class OpenClawCommandResult:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool


def openclaw_command_allowed(argv: list[str], allowed_commands: list[list[str]]) -> bool:
    if not argv:
        return False
    executable = PurePath(argv[0]).name.lower()
    if executable in _DENIED_EXECUTABLES:
        return False
    return any(argv == allowed for allowed in allowed_commands)


async def run_openclaw_command(
    argv: list[str],
    *,
    timeout_seconds: float = 15,
    output_limit: int = 16_384,
) -> OpenClawCommandResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        timed_out = True
        process.kill()
        stdout_bytes, stderr_bytes = await process.communicate()

    stdout, stdout_truncated = _decode_limited(stdout_bytes, output_limit)
    stderr, stderr_truncated = _decode_limited(stderr_bytes, output_limit)
    if timed_out:
        stderr = (stderr + "\n" if stderr else "") + "OpenClaw command timed out"
    return OpenClawCommandResult(
        exit_code=124 if timed_out else int(process.returncode or 0),
        stdout=stdout,
        stderr=stderr,
        truncated=stdout_truncated or stderr_truncated,
    )


def _decode_limited(value: bytes, limit: int) -> tuple[str, bool]:
    text = value.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text, False
    return text[:limit], True


__all__ = ["OpenClawCommandResult", "openclaw_command_allowed", "run_openclaw_command"]
