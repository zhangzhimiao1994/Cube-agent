from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

from agent_hub.skills.sandbox.base import (
    SkillInvocation,
    SkillResult,
    run_subprocess_with_limits,
    validate_execution_id,
)


@dataclass(frozen=True, slots=True)
class SystemdSandboxSettings:
    python: str = "/opt/agent-hub/current/.venv/bin/python"
    source_path: Path = Path("/opt/agent-hub/current/src")


class SystemdSkillSandbox:
    def __init__(self, settings: SystemdSandboxSettings | None = None) -> None:
        self._settings = settings or SystemdSandboxSettings()
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def run(self, invocation: SkillInvocation) -> SkillResult:
        argv = build_systemd_run_command(invocation, self._settings)
        try:
            return await run_subprocess_with_limits(
                argv,
                invocation,
                process_started=lambda process: self._processes.__setitem__(
                    invocation.execution_id, process
                ),
                on_forced_terminate=lambda: self._terminate_backend(invocation.execution_id),
            )
        finally:
            self._processes.pop(invocation.execution_id, None)

    async def terminate(self, execution_id: str) -> None:
        process = self._processes.get(execution_id)
        if process is not None and process.returncode is None:
            process.kill()
        with contextlib.suppress(FileNotFoundError, TimeoutError):
            await self._terminate_backend(execution_id)

    async def _terminate_backend(self, execution_id: str) -> None:
        killer = await asyncio.create_subprocess_exec(
            *build_systemd_terminate_command(execution_id),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=5)


def build_systemd_run_command(
    invocation: SkillInvocation,
    settings: SystemdSandboxSettings | None = None,
) -> tuple[str, ...]:
    settings = settings or SystemdSandboxSettings()
    command: list[str] = [
        "systemd-run",
        "--wait",
        "--collect",
        "--pipe",
        "--quiet",
        "--unit",
        f"agent-hub-skill-{invocation.execution_id}",
        "-p",
        "DynamicUser=yes",
        "-p",
        "NoNewPrivileges=yes",
        "-p",
        "ProtectSystem=strict",
        "-p",
        "PrivateTmp=yes",
        "-p",
        "PrivateDevices=yes",
        "-p",
        "RestrictSUIDSGID=yes",
        "-p",
        "PrivateNetwork=yes",
        "-p",
        "IPAddressDeny=any",
        "-p",
        f"MemoryMax={invocation.memory_limit_bytes}",
        "-p",
        f"CPUQuota={invocation.cpu_quota_percent}%",
        "-p",
        f"RuntimeMaxSec={invocation.timeout_seconds}s",
        "-p",
        f"ReadOnlyPaths={_path_arg(invocation.package_path)}",
        "-p",
        f"ReadWritePaths={_path_arg(invocation.writable_tmp_path)}",
        "-p",
        f"WorkingDirectory={_path_arg(invocation.writable_tmp_path)}",
        "-E",
        f"PYTHONPATH={_path_arg(settings.source_path)}",
        "-E",
        f"AGENT_HUB_EXECUTION_ID={invocation.execution_id}",
        "-E",
        f"AGENT_HUB_PACKAGE_PATH={_path_arg(invocation.package_path)}",
        "-E",
        f"AGENT_HUB_PACKAGE_SHA256={invocation.package_sha256}",
        "-E",
        f"AGENT_HUB_OUTPUT_LIMIT_BYTES={invocation.output_limit_bytes}",
        "-E",
        f"AGENT_HUB_WORKDIR={_path_arg(invocation.writable_tmp_path)}",
        "-E",
        f"AGENT_HUB_TIMEOUT_SECONDS={invocation.timeout_seconds}",
    ]
    for input_path in invocation.read_only_inputs:
        command.extend(("-p", f"ReadOnlyPaths={_path_arg(input_path)}"))
    command.extend((settings.python, "-m", "agent_hub.skills.runner"))
    return tuple(command)


def build_systemd_terminate_command(execution_id: str) -> tuple[str, ...]:
    validate_execution_id(execution_id)
    return ("systemctl", "kill", "--kill-who=all", f"agent-hub-skill-{execution_id}.service")


def _path_arg(value: Path) -> str:
    text = value.as_posix()
    if not text or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("systemd path values must not contain control characters")
    return text
